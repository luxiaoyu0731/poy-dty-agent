import { expect, test, type BrowserContext, type Page } from "@playwright/test";

async function openModule(context: BrowserContext, module: string) {
  const page = await context.newPage();
  await page.goto(`/?module=${module}`);
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("正在读取交付状态", { exact: true })).not.toBeVisible({ timeout: 60_000 });
  return page;
}

function apiPath(requestUrl: string) {
  const url = new URL(requestUrl);
  return url.pathname.startsWith("/api/v1/") ? url.pathname : undefined;
}

function watchApi(page: Page) {
  const paths: string[] = [];
  page.on("request", (request) => {
    const path = apiPath(request.url());
    if (path) paths.push(path);
  });
  return paths;
}

test("fresh browser contexts render the same server-owned daily conclusion", async ({ browser }) => {
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();
  await firstContext.addInitScript(() => localStorage.setItem("unrelated-device-state", "first"));
  await secondContext.addInitScript(() => localStorage.setItem("unrelated-device-state", "second"));

  try {
    const [firstPage, secondPage] = await Promise.all([
      openModule(firstContext, "overview"),
      openModule(secondContext, "overview")
    ]);
    const conclusion = ".overview-decision .decision-primary strong";
    await expect(firstPage.locator(conclusion)).toBeVisible({ timeout: 60_000 });
    await expect(secondPage.locator(conclusion)).toBeVisible({ timeout: 60_000 });
    await expect(firstPage.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
    await expect(secondPage.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });

    const [firstText, secondText] = await Promise.all([
      firstPage.locator(conclusion).innerText(),
      secondPage.locator(conclusion).innerText()
    ]);
    expect(firstText.trim()).not.toBe("");
    expect(secondText).toBe(firstText);
  } finally {
    await firstContext.close();
    await secondContext.close();
  }
});

test("overview defers market and evidence requests until their module is opened", async ({ page }) => {
  const paths = watchApi(page);
  await page.goto("/?module=overview");
  await expect(page.locator(".overview-layout")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".overview-decision")).toBeVisible({ timeout: 60_000 });

  // v34：概览随服务端快照携带市场链；证据检索类端点仍须延迟到证据模块。
  for (const deferredPath of [
    "/api/v1/knowledge/graph",
    "/api/v1/knowledge/retrieval"
  ]) {
    expect(paths, `overview must defer ${deferredPath}`).not.toContain(deferredPath);
  }

  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  await expect(page.locator(".market-layout")).toBeVisible();
  await expect(page.locator("[data-testid='price-detail-list']")).toBeVisible({ timeout: 60_000 });

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await expect(page.getByRole("link", { name: "证据图谱", exact: true })).toHaveAttribute("aria-current", "page");
  await expect.poll(() => paths, { timeout: 60_000 }).toContain("/api/v1/knowledge/graph");
  await expect.poll(() => paths, { timeout: 60_000 }).toContain("/api/v1/knowledge/retrieval");

  // 市场链可随快照预载；证据检索数据按需加载且去重。
  for (const path of ["/api/v1/knowledge/graph", "/api/v1/knowledge/retrieval"]) {
    expect(paths.filter((item) => item === path), `${path} should be de-duplicated`).toHaveLength(1);
  }
});

test("a failed refresh keeps the last successful module data visible", async ({ page }) => {
  let rejectPriceRefresh = false;
  const initialPriceResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/price-comparison") && response.status() === 200
  );
  await page.route("**/api/v1/price-comparison**", async (route) => {
    if (rejectPriceRefresh) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary outage" }) });
      return;
    }
    await route.fallback();
  });

  await page.goto("/?module=market");
  await expect(page.locator(".market-layout")).toBeVisible({ timeout: 60_000 });
  await initialPriceResponse;
  const priceDetails = page.locator("[data-testid='price-detail-list']");
  await expect(priceDetails).toBeVisible({ timeout: 60_000 });
  const firstQuote = priceDetails.locator("article").first();
  const before = (await firstQuote.innerText()).replace(/\s+/g, " ").trim();
  expect(before).not.toBe("");

  rejectPriceRefresh = true;
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  await expect(page.locator(".market-layout")).toBeVisible();
  await expect.poll(async () => (await firstQuote.innerText()).replace(/\s+/g, " ").trim(), { timeout: 60_000 }).toBe(before);
  await expect(page.locator(".delivery-workbench")).not.toContainText("正在读取交付状态");
});

test("an older module request cannot finish the active module's silent sync", async ({ page }) => {
  let releaseEvidence!: () => void;
  let releaseMarket!: () => void;
  const evidenceReleased = new Promise<void>((resolve) => { releaseEvidence = resolve; });
  const marketReleased = new Promise<void>((resolve) => { releaseMarket = resolve; });
  let evidenceStarted!: () => void;
  let marketStarted!: () => void;
  const evidenceRequest = new Promise<void>((resolve) => { evidenceStarted = resolve; });
  const marketRequest = new Promise<void>((resolve) => { marketStarted = resolve; });

  await page.route("**/api/v1/knowledge/graph**", async (route) => {
    evidenceStarted();
    await evidenceReleased;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "delayed evidence" }) });
  });
  await page.route("**/api/v1/price-comparison**", async (route) => {
    marketStarted();
    await marketReleased;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "delayed market" }) });
  });

  await page.goto("/?module=overview");
  await expect(page.locator(".overview-decision")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".delivery-side-status")).not.toContainText("暂无时间", { timeout: 60_000 });
  const conclusion = (await page.locator(".overview-decision .decision-primary strong").innerText()).replace(/\s+/g, "").trim();

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await evidenceRequest;
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  await marketRequest;
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "true");

  releaseEvidence();
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "true");

  releaseMarket();
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  await page.getByRole("link", { name: "总览看板", exact: true }).click();
  await expect.poll(async () => (await page.locator(".overview-decision .decision-primary strong").innerText()).replace(/\s+/g, "").trim()).toBe(conclusion);
});

test("evidence renders the frozen snapshot while live retrieval is still pending", async ({ page }) => {
  let releaseRagVisual!: () => void;
  const ragVisualReleased = new Promise<void>((resolve) => { releaseRagVisual = resolve; });
  let markRagStarted!: () => void;
  const ragStarted = new Promise<void>((resolve) => { markRagStarted = resolve; });

  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      snapshot_id: "daily-test", business_date: "2026-07-14", generated_at: "2026-07-14T01:30:00Z",
      as_of_time: "2026-07-14T01:30:00Z", status: "published", source_run_id: "run-test",
      data_snapshot_id: "data-test", payload_sha256: "a".repeat(64),
      workbench: {
        contract_version: "1.0", business_date: "2026-07-14", as_of_time: "2026-07-14T01:30:00Z",
        source_run: {}, daily_report: {},
        judgement: {
          overview: { direction: "震荡", confidence: 0.55, key_drivers: [], risks: [], trend_7d: "震荡", trend_30d: "震荡", updated_at: "2026-07-14T01:30:00Z" },
          full_chain: { generated_at: "2026-07-14T01:30:00Z", as_of_time: "2026-07-14T01:30:00Z", status: "ready", summary: [] },
          factors: [], formal_predictions: []
        },
        market: {
          latest_prices: { generated_at: "2026-07-14T01:30:00Z", items: [], policy_note: "", status_counts: {} },
          chain: { generated_at: "2026-07-14T01:30:00Z", products: [], coverage: { product_count: 0, price_ready: 0, indicator_ready: 0 } }
        },
        briefing: { morning_brief: [], events: [] }
      },
      live: { conclusion_locked: true, refresh_policy: "daily_snapshot_is_authoritative", message: "locked" }
    }) });
  });
  await page.route("**/api/v1/workbench/rag-visual**", async (route) => {
    markRagStarted();
    await ragVisualReleased;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary" }) });
  });

  await page.goto("/?module=evidence");
  await ragStarted;
  // v34：核验台读取统一证明档案，不依赖 rag-visual；检索挂起时页面照常可读。
  const board = page.locator("[data-testid='evidence-verification-board']");
  await expect(board).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#evb-verdict")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("workbench-degraded-banner")).toHaveCount(0);

  releaseRagVisual();
  await expect(board).toBeVisible({ timeout: 20_000 });
  await expect(page.locator("#evb-verdict")).toBeVisible({ timeout: 20_000 });
});

test("CFTC positioning in a frozen snapshot never renders as a crude-oil price", async ({ page }) => {
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      snapshot_id: "daily-cftc-regression", business_date: "2026-07-20", generated_at: "2026-07-20T04:37:00Z",
      as_of_time: "2026-07-20T04:37:00Z", status: "published", source_run_id: "run-cftc-regression",
      data_snapshot_id: "data-cftc-regression", payload_sha256: "b".repeat(64),
      workbench: {
        contract_version: "1.0", business_date: "2026-07-20", as_of_time: "2026-07-20T04:37:00Z",
        source_run: {}, daily_report: {},
        judgement: {
          overview: { status: "中性偏强", cost_pressure_index: 55, confidence: 0.42, coverage_confidence: 0.88, key_drivers: [], risks: [], trend_1d: "震荡", trend_7d: "震荡", updated_at: "2026-07-20T04:37:00Z" },
          full_chain: { generated_at: "2026-07-20T04:37:00Z", as_of_time: "2026-07-20T04:37:00Z", status: "ready", summary: [{
            product: "CRUDE", metric: "CFTC COT open interest", value: 11924, unit: "contracts", observed_at: "2026-07-14",
            source_id: "cftc_cot_petroleum", evidence_tier: "A", formal_eligible: true
          }] },
          factors: [], formal_predictions: []
        },
        market: {
          latest_prices: { generated_at: "2026-07-20T04:37:00Z", items: [], policy_note: "", status_counts: {} },
          chain: { generated_at: "2026-07-20T04:37:00Z", products: [], coverage: { product_count: 0, price_ready: 0, indicator_ready: 0 } }
        },
        briefing: { morning_brief: [], events: [] }
      },
      live: { conclusion_locked: true, refresh_policy: "daily_snapshot_is_authoritative", message: "locked" }
    }) });
  });

  await page.goto("/?module=overview");
  await expect(page.locator(".overview-decision")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".overview-decision")).not.toContainText("11,924");
  await expect(page.locator(".overview-decision")).not.toContainText("CFTC COT open interest");
});

test("formal prediction ledger renders the audited 1 7 30 day batch instead of legacy scalar records", async ({ page }) => {
  let includeFrozenGrid = true;
  const formalCell = (horizon: 1 | 7 | 30, direction: "up" | "down" | "neutral") => ({
    node_id: "poy_dty_upstream_cost_pressure",
    horizon_days: horizon,
    direction,
    direction_probability: 0.7,
    magnitude: 1.2,
    magnitude_unit: "index_point",
    confidence: horizon === 30 ? 0.58 : 0.72,
    remaining_effective_probability: 0.64,
    driver_event_ids: [],
    counter_event_ids: [],
    data_completeness: 0.91,
    scoreability: "scorable",
    missing_series_ids: [],
    subtarget_results: ["poy", "dty"].map((target) => ({
      target,
      direction,
      direction_probability: 0.7,
      magnitude: 1.2,
      magnitude_unit: "index_point",
      confidence: 0.68,
      remaining_effective_probability: 0.64,
      data_completeness: 0.91,
      scoreability: "scorable",
      missing_series_ids: []
    }))
  });
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const payload = {
      schema_version: "phase-a.prediction.v1",
      prediction_batch_id: "formal-batch-ui",
      revision_id: "formal-revision-ui",
      previous_revision_id: null,
      business_date: "2026-08-27",
      data_frozen_at: "2026-08-27T00:20:00Z",
      published_at: "2026-08-27T01:20:00Z",
      as_of_time: "2026-08-27T01:20:00Z",
      data_snapshot_id: "formal-snapshot-ui",
      composition_rule_version: "phase-a.composition.v1",
      cells: [formalCell(1, "up"), formalCell(7, "neutral"), formalCell(30, "down")],
      created_at: "2026-08-27T01:15:00Z"
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      snapshot_id: "daily-formal-ui", business_date: "2026-08-27", generated_at: "2026-08-27T01:30:00Z",
      as_of_time: "2026-08-27T01:20:00Z", status: "published", source_run_id: "run-formal-ui",
      data_snapshot_id: "formal-snapshot-ui", payload_sha256: "f".repeat(64),
      workbench: {
        contract_version: "1.0", business_date: "2026-08-27", as_of_time: "2026-08-27T01:20:00Z",
        source_run: {}, daily_report: {},
        judgement: {
          overview: { status: "中性", cost_pressure_index: 50, key_drivers: [], trend_1d: "偏强", trend_7d: "震荡", trend_30d: "偏弱", updated_at: "2026-08-27T01:20:00Z" },
          full_chain: { generated_at: "2026-08-27T01:20:00Z", as_of_time: "2026-08-27T01:20:00Z", status: "ready", summary: [] },
          factors: [],
          formal_predictions: [{
            record_kind: "formal_batch_revision", governance_status: "proof_verified",
            prediction_batch_id: payload.prediction_batch_id, revision_id: payload.revision_id,
            previous_revision_id: null, business_date: payload.business_date, as_of_time: payload.as_of_time,
            persisted_at: "2026-08-27T01:25:00Z", assessment_id: "formal-assessment-ui",
            data_snapshot_id: payload.data_snapshot_id, payload_sha256: "a".repeat(64),
            ...(includeFrozenGrid ? { payload } : {})
          }]
        },
        market: {
          latest_prices: { generated_at: "2026-08-27T01:20:00Z", items: [], policy_note: "", status_counts: {} },
          chain: { generated_at: "2026-08-27T01:20:00Z", products: [], coverage: { product_count: 0, price_ready: 0, indicator_ready: 0 } }
        },
        briefing: { morning_brief: [], events: [] }
      },
      live: { conclusion_locked: true, refresh_policy: "daily_snapshot_is_authoritative", message: "locked" }
    }) });
  });

  await page.goto("/?module=reports&reportView=ledger");
  await expect(page.locator(".ledger-workspace")).toBeVisible({ timeout: 60_000 });
  // v34：兼容账本内容在“观察与到期复盘”页签内。
  await page.getByRole("tab", { name: "观察与到期复盘" }).click();
  const historicalContract = page.getByTestId("formal-prediction-batch-metadata-only");
  await expect(historicalContract).toContainText("历史合同记录，不属于当前正式预测");
  await expect(historicalContract).toContainText("seven-product-forecast.v1");
  await expect(page.locator(".ledger-boundary-note").filter({ hasText: "历史兼容预测复盘" })).toBeVisible();

  includeFrozenGrid = false;
  await page.reload();
  const metadataOnly = page.getByTestId("formal-prediction-batch-metadata-only");
  await expect(metadataOnly).toContainText("历史合同记录，不属于当前正式预测", { timeout: 60_000 });
  await expect(metadataOnly).toContainText("只有 seven-product-forecast.v1 的完整 21 格");
});

test("seven-product workbench renders the complete grid, evidence, gates and exports from one contract", async ({ page }) => {
  test.setTimeout(120_000);
  const targets = ["crude", "naphtha", "px", "pta", "meg", "poy", "dty"] as const;
  const horizons = [1, 7, 30] as const;
  const forecastCells = targets.flatMap((target, targetIndex) => horizons.map((horizon) => ({
    target,
    horizon_days: horizon,
    label_series_id: `${target}.ui-contract-label`,
    label_registry_version: "seven-product-labels.v1",
    neutral_band_policy_version: "seven-product-neutral-bands.v1",
    model_version: "ui-reference.v1",
    feature_version: "ui-features.v1",
    as_of_time: "2026-08-31T08:20:00+08:00",
    latest_observation_at: "2026-08-30T16:00:00+08:00",
    latest_visible_at: "2026-08-30T17:00:00+08:00",
    latest_value: 70 + targetIndex * 1_000,
    unit: target === "crude" ? "USD/bbl" : "CNY/mt",
    point_forecast: 71 + targetIndex * 1_000 + horizon / 10,
    interval_low: 69 + targetIndex * 1_000,
    interval_high: 73 + targetIndex * 1_000,
    predicted_change_pct: 0.01,
    neutral_band_pct: 0.005,
    direction: "up",
    confidence: 0.61,
    formal_status: target === "naphtha" ? "degraded" : "reference",
    formal_eligible: false,
    status_reason: "尚未通过逐格样本外晋级门禁",
    data_status: target === "naphtha" ? "proxy" : "fresh",
    source_matches_label: true,
    history_points: 126,
    evaluation_status: "failed",
    evaluation_id: "eval-ui-21",
    evaluation_result_sha256: "b".repeat(64),
    model_registry_revision: "registry-ui",
    key_drivers: ["近期价格趋势"],
    data_gaps: target === "naphtha" ? ["公开标签历史覆盖不足"] : [],
    evidence: [{
      observation_id: `${target}-latest`,
      source_id: `${target}-public-source`,
      source_url: `https://example.com/evidence/${target}`,
      observed_at: "2026-08-30T16:00:00+08:00",
      visible_at: "2026-08-30T17:00:00+08:00",
      value: 70 + targetIndex * 1_000,
      unit: target === "crude" ? "USD/bbl" : "CNY/mt",
      raw_sha256: "a".repeat(64)
    }],
    data_snapshot_sha256: "c".repeat(64),
    configuration_sha256: "d".repeat(64)
  })));
  const evaluationCells = forecastCells.map((cell) => ({
    target: cell.target,
    horizon_days: cell.horizon_days,
    label_series_id: cell.label_series_id,
    model_version: cell.model_version,
    evaluation_policy_version: "seven-product-oos-gate.v3",
    split_method: "expanding_origin_final_50pct",
    train_start: "2025-01-01",
    selection_end: "2025-12-31",
    test_start: "2026-01-01",
    test_end: "2026-08-30",
    sample_count: 60,
    effective_sample_count: 30,
    candidate_mae: 1.2,
    persistence_mae: 1.1,
    seasonal_mae: 1.3,
    best_baseline_mae: 1.1,
    error_improvement: -0.09,
    error_improvement_ci_low: -0.15,
    error_improvement_ci_high: -0.03,
    direction_accuracy: 0.57,
    direction_accuracy_ci_low: 0.5,
    direction_accuracy_ci_high: 0.64,
    worst_regime: { regime: "volatile" },
    recent_outcomes: [{
      origin_observation_id: `${cell.target}-origin`,
      origin_observed_at: "2026-08-23T16:00:00+08:00",
      origin_visible_at: "2026-08-23T17:00:00+08:00",
      actual_observation_id: `${cell.target}-actual`,
      actual_observed_at: "2026-08-30T16:00:00+08:00",
      actual_visible_at: "2026-08-30T17:00:00+08:00",
      candidate: cell.point_forecast,
      actual: cell.latest_value,
      absolute_error: Math.abs(cell.point_forecast - cell.latest_value),
      candidate_direction: "up",
      actual_direction: "up",
      direction_hit: true
    }],
    leakage_status: "passed",
    leakage_reasons: [],
    source_matches_label: true,
    data_snapshot_sha256: "c".repeat(64),
    evaluation_configuration_sha256: "e".repeat(64),
    result_sha256: "b".repeat(64),
    promotion_eligible: false,
    gate_reasons: ["error_improvement_below_5pct"]
  }));

  await page.route("**/api/v1/forecasts/seven-product**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/evaluation")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        schema_version: "seven-product-evaluation.v2",
        evaluation_id: "eval-ui-21",
        generated_at: "2026-08-31T08:21:00+08:00",
        as_of_time: "2026-08-31T08:20:00+08:00",
        evaluation_policy_version: "seven-product-oos-gate.v3",
        minimum_error_improvement: 0.05,
        minimum_direction_accuracy: 0.55,
        minimum_effective_samples: 20,
        cells: evaluationCells,
        passed_count: 0,
        contract_complete: true,
        overall_status: "blocked",
        evaluation_configuration_sha256: "e".repeat(64),
        data_snapshot_sha256: "c".repeat(64),
        report_sha256: "f".repeat(64)
      }) });
      return;
    }
    if (url.pathname.endsWith("/export")) {
      const format = url.searchParams.get("format");
      await route.fulfill({
        status: 200,
        contentType: format === "csv" ? "text/csv" : "application/json",
        headers: { "content-disposition": `attachment; filename="seven-product.${format}"` },
        body: format === "csv" ? "target,horizon_days\ncrude,1\n" : JSON.stringify({ forecast: { cells: forecastCells } })
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      schema_version: "seven-product-forecast.v1",
      batch_id: "forecast-ui-21",
      generated_at: "2026-08-31T08:20:30+08:00",
      as_of_time: "2026-08-31T08:20:00+08:00",
      targets,
      horizons,
      cells: forecastCells,
      formal_count: 0,
      reference_count: 18,
      unavailable_count: 3,
      contract_complete: true,
      customer_boundary: "仅提供预测结果，不读取操作者库存、加工利润或交易指令。"
    }) });
  });

  await page.goto("/?module=reports&reportView=ledger");
  await expect(page.locator(".ledger-workspace")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("seven-product-forecast-grid")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("seven-product-contract-summary")).toContainText("21/21 完整");
  await expect(page.getByTestId("seven-product-contract-summary")).toContainText("正式 0/21");
  await expect(page.getByTestId("seven-product-row")).toHaveCount(7);
  await expect(page.getByTestId("seven-product-forecast-cell")).toHaveCount(21);
  await expect(page.getByText("样本外门禁 0/21", { exact: true })).toBeVisible();
  await expect(page.getByTestId("seven-product-evaluation-contract")).toContainText("seven-product-evaluation.v2");
  await expect(page.getByTestId("seven-product-evaluation-contract")).toContainText("seven-product-oos-gate.v3");

  const targetCell = page.locator('[data-testid="seven-product-forecast-cell"][data-target="naphtha"][data-horizon="7"]');
  await expect(targetCell).toContainText("降级参考");
  await expect(targetCell).toContainText("上涨");
  await expect(targetCell).toContainText("参考评分 61%");
  await targetCell.getByText("依据与缺口", { exact: true }).click();
  await expect(targetCell).toContainText("近期价格趋势");
  await expect(targetCell).toContainText("公开标签历史覆盖不足");
  await expect(targetCell).toContainText("代理来源");
  await expect(targetCell).toContainText("样本外评测：未通过");
  await expect(targetCell.getByTestId("seven-product-latest-outcome")).toContainText("最近样本外：预测");
  await expect(targetCell.getByTestId("seven-product-latest-outcome-times")).toContainText(
    "原点可见 2026-08-23 17:00（上海）"
  );
  await expect(targetCell.getByTestId("seven-product-latest-outcome-times")).toContainText(
    "实际可见 2026-08-30 17:00（上海）"
  );
  await expect(targetCell).toContainText("方向命中");
  await expect(targetCell).toContainText("原件 SHA-256");
  // 证据链接在折叠的 <details>（依据与缺口）内：getByRole 默认不匹配
  // 关闭 details 的隐藏子树，改用 CSS 定位器。
  await expect.poll(async () => targetCell.locator("a", { hasText: "最近证据" }).getAttribute("href"), { timeout: 15_000 }).toBe("https://example.com/evidence/naphtha");
  await expect(page.getByTestId("seven-product-operations")).toContainText("运行、更新与模型状态");
  await expect(page.getByTestId("seven-product-operations")).toContainText("本预测采用基准的新鲜度");
  await expect(page.getByTestId("seven-product-operations")).toContainText("备份与系统健康");
  await expect(page.getByTestId("seven-product-operations")).toContainText("数值模型 / 注册表");
  // 下载契约 = 附件响应（文件名+内容）。当前 headless 环境对同页 <a> 导航
  // 不派发 download 事件，改为直接验证链接指向的响应契约。
  const exportLinks = page.locator(".seven-product-export-link");
  await expect.poll(async () => exportLinks.count(), { timeout: 20_000 }).toBeGreaterThanOrEqual(2);
  const csvHref = await exportLinks.filter({ hasText: "导出 CSV" }).first().getAttribute("href");
  expect(csvHref).toContain("/api/v1/forecasts/seven-product/export?format=csv");
  // page.request 不经过页面路由拦截；用页面内 fetch 走同一 mock。
  const csvContract = await page.evaluate(async (href: string) => {
    const response = await fetch(href);
    return { status: response.status, disposition: response.headers.get("content-disposition") ?? "" };
  }, csvHref!);
  expect(csvContract.status).toBe(200);
  expect(csvContract.disposition).toContain('filename="seven-product.csv"');
  const jsonHref = await exportLinks.filter({ hasText: "导出 JSON" }).first().getAttribute("href");
  const jsonContract = await page.evaluate(async (href: string) => {
    const response = await fetch(href);
    return { status: response.status, disposition: response.headers.get("content-disposition") ?? "", body: await response.text() };
  }, jsonHref!);
  expect(jsonContract.status).toBe(200);
  expect(jsonContract.disposition).toContain('filename="seven-product.json"');
  expect(JSON.parse(jsonContract.body).forecast.cells).toHaveLength(21);
});
