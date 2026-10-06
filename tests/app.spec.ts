import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

const modules = [
  "总览看板",
  "行情与原料链",
  "证据图谱",
  "Agent 系统",
  "AI 研判助手",
  "研判报告",
  "工业情报中心"
];

const forbiddenVisibleTerms = [
  "AGENTS",
  "README",
  "runbook",
  "architecture",
  "run_id",
  "agent_id",
  "source_id",
  "dataset_type",
  "action_state",
  "hit_rate",
  "scored",
  "pending",
  "leaks",
  "derived_graph",
  "quality_gates",
  "forecast_v2",
  "Forecast V2",
  "V2 H1",
  "价格序列实验",
  "backtest_metrics",
  "replenishment_tasks",
  "coverage_gaps",
  "API key",
  "password",
  "license",
  "login",
  "provider",
  "token",
  "prompt",
  "HTTP",
  "doc_id",
  "chunk_id",
  "embedding",
  "source_id",
  "raw",
  "mock",
  "fake",
  "demo",
  "Safari",
  "Computer Use",
  "authorized",
  "内部字段",
  "账号",
  "密码",
  "可自动交易",
  "预测保证有效",
  "模型准确率",
  "人工确认",
  "人工复核",
  "人工签署",
  "真人签署"
];

async function waitForWorkbench(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("正在读取交付状态", { exact: true })).not.toBeVisible({ timeout: 60_000 });
  if (!(await page.getByRole("link", { name: "总览看板" }).getAttribute("aria-current"))) {
    await page.getByRole("link", { name: "总览看板" }).click();
  }
  // v34 固定视口设计有意去掉大页头（review-no-page-heading）；总览就绪的
  // 稳定锚点是内容区布局本身，而不是已不存在的页面标题。
  await expect(page.locator(".overview-layout")).toBeVisible({ timeout: 60_000 });
}

async function passThroughAuthentication(route: import("@playwright/test").Route): Promise<boolean> {
  if (!new URL(route.request().url()).pathname.startsWith("/api/v1/auth/")) return false;
  await route.continue();
  return true;
}

async function expectNoForbiddenVisibleText(page: import("@playwright/test").Page) {
  const text = await page.locator(".delivery-workbench").first().innerText({ timeout: 10_000 });
  for (const term of forbiddenVisibleTerms) {
    expect(text, `visible text should not contain ${term}`).not.toContain(term);
  }
  expect(text, "visible text should not expose generic internal labels").not.toMatch(
    /(^|\n)\s*(score|raw|mock|fake|demo)\s*($|[:：])/i
  );
  expect(text, "visible text should not expose generic internal key fragments").not.toMatch(
    /\b(score|raw|mock|fake|demo)[_-][A-Za-z0-9]+|[A-Za-z0-9]+[_-](score|raw|mock|fake|demo)\b/i
  );
  expect(text, "visible text should not expose snake_case").not.toMatch(/[A-Za-z]+(?:_[A-Za-z0-9]+)+/);
  expect(text, "visible text should not expose camelCase").not.toMatch(/\b[a-z]+[A-Z][A-Za-z0-9]*\b/);
}

test("formal prediction browser source remains statically read-only", async () => {
  const predictionComponent = await readFile(new URL("../src/components/prediction.tsx", import.meta.url), "utf8");
  const apiClient = await readFile(new URL("../src/services/api.ts", import.meta.url), "utf8");
  const predictionPage = await readFile(new URL("../src/pages/AgentWorkbenchPage.tsx", import.meta.url), "utf8");

  expect(predictionComponent).toContain("正式预测批次只能由受治理的内部流程生成；当前页面不提供写入入口。");
  expect(predictionComponent).not.toMatch(/<(?:input|textarea|select|button)\b/i);
  expect(predictionComponent).not.toMatch(/\b(?:useState|onClick|onChange|managedFetch|api\.)\b/);
  const apiImportDeclarations = predictionComponent.match(
    /import[\s\S]*?from\s+["'][^"']*services\/api["'];?/g
  ) ?? [];
  for (const declaration of apiImportDeclarations) {
    expect(declaration, "prediction UI may import API contracts only as erased TypeScript types").toMatch(
      /^import\s+type\b/
    );
  }

  expect(apiClient).not.toMatch(/\bcreatePrediction\s*:/);
  expect(apiClient).not.toContain("/internal/formal-prediction-batches");
  expect(apiClient).not.toMatch(/managedFetch\(["']\/predictions["']\s*,\s*\{\s*method:\s*["']POST["']/s);

  expect(predictionPage).toContain('title="历史合同正式批次（只读审计）"');
  expect(predictionPage).toContain("<FormalPredictionBatchView batches={live.formalPredictions.value} />");
  expect(predictionPage).not.toMatch(/api\.(?:createPrediction|savePrediction)\s*\(/);
  expect(predictionPage).not.toMatch(/\b(?:PredictionForm|createPrediction|savePrediction|onSaved)\b/);
});

test("customer workbench opens on the production overview", async ({ page }) => {
  const seenApiPaths = new Set<string>();
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/")) seenApiPaths.add(url.pathname);
  });

  await waitForWorkbench(page);
  await expect(page.locator(".delivery-metric").filter({ hasText: "判断性质" })).toBeVisible();
  await expect(page.locator(".delivery-metric").filter({ hasText: /成本压力方向|POY\/DTY 价格方向/ }).last()).toBeVisible();
  await expect(page.locator(".delivery-metric").filter({ hasText: "数据新鲜度" })).toBeVisible();
  await expect(page.locator(".delivery-metric").filter({ hasText: /结论置信度|参考评分/ })).toBeVisible();
  await expect(page.locator(".overview-status-strip")).toHaveCount(0);
  await expect(page.getByTestId("source-automation-strip")).toHaveCount(0);
  await expect(page.getByText("判断依据与核验", { exact: true })).toBeVisible();
  await expect(page.locator(".overview-page")).not.toContainText("数据未就绪");
  await expect(page.locator(".overview-decision")).toBeVisible();
  await expect(page.getByText("关键风险与核验信号").first()).toBeVisible();
  for (const expectedPath of [
    "/api/v1/delivery/status",
    "/api/v1/prices/latest"
  ]) {
    await expect
      .poll(() => Array.from(seenApiPaths), {
        message: `production workbench should request ${expectedPath}`,
        timeout: 60_000
      })
      .toContain(expectedPath);
  }
  await expectNoForbiddenVisibleText(page);
});

test("left navigation exposes the seven legacy modules followed by the intelligence module", async ({ page, context }) => {
  await waitForWorkbench(page);
  const labels = await page.locator(".delivery-nav a").evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("aria-label"))
  );
  expect(labels).toEqual(modules);
  const marketLink = page.getByRole("link", { name: "行情与原料链", exact: true });
  await expect(marketLink).toHaveAttribute("href", /module=market/);
  const popupPromise = context.waitForEvent("page");
  await marketLink.click({ button: "middle" });
  const popup = await popupPromise;
  await expect.poll(() => new URL(popup.url()).searchParams.get("module")).toBe("market");
  await popup.close();

  for (const label of modules) {
    const link = page.getByRole("link", { name: label, exact: true });
    await link.click();
    await expect(link).toHaveAttribute("aria-current", "page", { timeout: 30_000 });
    await expect(page.locator("#main-content")).toBeVisible();
    await expectNoForbiddenVisibleText(page);
  }
});

test("market, event, evidence, assistant and report modules show production components", async ({ page }) => {
    // C 类修复：e2e 种子未构建情报投影；雷达列表用确定性 fixture（与
  // intelligence-module.spec 同形状），断言组件真实渲染数据行。
  await page.route("**/api/v1/intelligence/events**", async (route) => {
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ items: [{
        event_id: "event-radar-probe", event_revision_id: "rev-radar-probe", revision_no: 1,
        status: "open", title: "雷达链路探测事件", overview_text: "原油链路事件摘要",
        category: "energy", region_codes: [], product_ids: ["crude"],
        last_seen_at: "2026-09-14T08:00:00Z", as_of_time: "2026-09-14T08:00:00Z",
        relevance_score: 60, severity_score: 50, urgency_score: 40, confidence: 0.5,
        location_precision: null, evidence_count: 1, gap_count: 0, payload_sha256: "0".repeat(64)
      }], has_more: false, next_cursor: null })
    });
  });
await waitForWorkbench(page);

  await page.getByRole("link", { name: "行情与原料链" }).click();
  await expect(page.getByText("原油").first()).toBeVisible();
  await expect(page.getByText("石脑油").first()).toBeVisible();
  await expect(page.locator("[data-testid='price-detail-list']")).toBeVisible();
  const marketActions = page.locator(".trend-panel .delivery-panel-head").or(page.locator(".delivery-page-actions"));
  await expect(marketActions.getByText("末30日数据", { exact: true })).toBeVisible();
  await expect(marketActions.getByText("末90日数据", { exact: true })).toBeVisible();
  await expect(marketActions.getByText("全部", { exact: true })).toBeVisible();
  for (const product of ["POY", "DTY", "PX", "PTA", "MEG", "石脑油", "原油"]) {
    await marketActions.getByText(product, { exact: true }).click();
    await expect(page.locator(".trend-panel")).toContainText("最新价格");
    await expect(page.locator(".trend-panel")).toContainText("曲线");
    await expect(page.locator(".trend-panel")).toContainText("观察信号");
    await expect(page.locator(".market-summary-grid")).toContainText("价格");
    await expect(page.locator(".market-summary-grid")).toContainText("加工差");
    await expect(page.locator(".delivery-content")).not.toContainText("系统未返回价格序列");
  }
  await expect(page.locator(".delivery-content")).not.toContainText("CFTC");
  await marketActions.getByText("末30日数据", { exact: true }).click();
  await expect(page.locator(".trend-panel .recharts-wrapper")).toBeVisible({ timeout: 30_000 });
  await marketActions.getByText("末90日数据", { exact: true }).click();
  await expect(page.locator(".trend-panel .recharts-wrapper")).toBeVisible({ timeout: 30_000 });
  await marketActions.getByText("全部", { exact: true }).click();
  await expect(page.locator(".trend-panel .recharts-wrapper")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "工业情报中心" }).click();
  await page.getByRole("tab", { name: "全球雷达" }).click();
  await expect(page.locator("[aria-label='雷达事件列表'] li").first()).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible();
  await expect(page.locator("#evb-verdict")).toBeVisible();
  await expect(page.locator("#evb-claims").getByText(/当前支持|当前没有通过核验的支持事件/).first()).toBeVisible();
  await expect(page.locator("#evb-history").getByText(/历史类似支持案例|没有符合条件的历史支持案例/).first()).toBeVisible();
  await expect(page.locator("#evb-gaps").getByText("机制覆盖")).toBeVisible();
  await expect(page.locator("[data-testid='evidence-review-queue']")).toHaveCount(0);

  await page.getByRole("link", { name: "AI 研判助手" }).click();
  // v34：推荐问题在右侧分页签（tab 与隐藏面板同名），锚定到分页签本身。
  await expect(page.getByRole("tab", { name: "推荐问题" })).toBeVisible();
  await expect(page.locator("[data-testid='assistant-messages']")).toBeAttached();
  await page.getByRole("tab", { name: "引用证据" }).click();
  // v34 侧栏分页签隐藏面板标题；引用证据区的稳定锚点是分组内容。
  await expect(page.getByText(/问答参考材料|正式结论采用证据/).first()).toBeVisible();

  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await expect(page.getByText("报告列表")).toBeVisible();
  await expect(page.getByText("报告预览")).toBeVisible();
  await expect(page.getByText("报告信息")).toBeVisible();
  await expect(page.locator("[data-testid='report-source-content']")).toBeVisible();
  await expect(page.locator(".chain-pressure-rail, .report-unavailable-state")).toBeVisible();
});

test("published daily snapshot remains authoritative during a live price refresh", async ({ page }) => {
  await page.route(/prices\/latest/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: "2026-07-24T06:36:00Z",
        policy_note: "internal collection note must not be rendered",
        status_counts: { near_realtime: 1 },
        items: [{
          instrument: "Brent",
          label: "Brent",
          freshness: "near_realtime",
          freshness_label: "最新观测",
          quote_type_label: "期货行情",
          is_transaction_price: true,
          gap_reason: "",
          latest: {
            observation_id: "brent-latest",
            created_at: "2026-07-24T06:36:01Z",
            instrument: "Brent",
            symbol: "BZ=F",
            observed_at: "2026-07-24T06:35:00Z",
            interval_seconds: 60,
            price_type: "near_realtime_public",
            last: 82.35,
            unit: "USD/bbl",
            source_id: "free_market_feed",
            source_url: "https://example.test/brent",
            quality: "observed",
            notes: "",
            raw: {}
          }
        }]
      })
    });
  });
  let delayedMarketRequest = false;
  await page.route(/workbench\/market-chain/, async (route) => {
    if (delayedMarketRequest) {
      await route.fallback();
      return;
    }
    delayedMarketRequest = true;
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fulfill({ response });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  // v34：品种选择在链路节点按钮与趋势面板分段（页头动作区已退役）。
  await page.locator(".review-material-price").filter({ hasText: "原油" }).click();
  const trend = page.locator(".trend-panel");
  const crudeNode = page.locator(".review-material-price").filter({ hasText: "原油" });
  const snapshotPrice = (await trend.locator("text=最新价格").locator("..").innerText()).replace(/\s+/g, " ");
  expect(snapshotPrice).not.toContain("82.35");
  await page.waitForTimeout(2_000);
  await expect(trend).not.toContainText("82.35");
  await expect(crudeNode).not.toContainText("82.35");
  await expect(page.locator(".market-page")).not.toContainText("公开期货代理行情");
  await expect(page.locator(".market-page")).not.toContainText("非 ICE/CME 授权实时行情");
  // Both the page-level snapshot request and the market module's fallback
  // request must have time to settle before the context is closed.
  await page.waitForTimeout(2_000);
});

test("prediction ledger is available inside the report module", async ({ page }) => {
  const formalWritePosts: string[] = [];
  for (const endpoint of ["**/api/v1/predictions", "**/api/v1/internal/formal-prediction-batches"]) {
    await page.route(endpoint, async (route) => {
      if (route.request().method() === "POST") formalWritePosts.push(route.request().url());
      await route.continue();
    });
  }
  await waitForWorkbench(page);

  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await expect(page.locator("[data-testid='report-workspace-switch']")).toBeVisible();
  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();

  await expect(page.locator(".ledger-workspace")).toBeVisible();
  await expect(page.locator(".delivery-nav a")).toHaveCount(7);
  await expect(page.locator(".ledger-boundary-note").first()).toContainText("上游原料成本压力判断");
  await expect(page.locator(".ledger-boundary-note").first()).toContainText("不提供采购、报价、接单或库存执行指令");
  await expect(page.locator(".ledger-summary-strip")).toContainText("1 / 7 / 30 天");
  // v34：兼容账本（列表/详情/到期复盘）位于“观察与到期复盘”分页签，先切换再断言。
  await page.getByRole("tab", { name: "观察与到期复盘" }).click();
  await expect(page.locator(".ledger-list-panel")).toContainText(/预测账本|暂未返回|暂无上游成本压力判断记录/);
  if (await page.locator(".ledger-list-panel").getByText("暂无上游成本压力判断记录").isVisible()) {
    await expect(page.locator("[data-testid='ledger-pending-confirmation']")).toContainText("当前研判待确认，尚未落账");
    await expect(page.locator("[data-testid='ledger-pending-confirmation']")).toContainText("不等于正式预测");
  }
  await expect(page.locator(".ledger-detail-panel")).toBeVisible();
  await expect(page.locator("input[name='prediction-target']")).toHaveCount(0);
  await expect(page.locator("select[name='prediction-horizon']")).toHaveCount(0);
  await expect(page.locator("select[name='prediction-direction']")).toHaveCount(0);
  await expect(page.locator("input[name='prediction-confidence']")).toHaveCount(0);
  await expect(page.locator("textarea[name='prediction-rationale']")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /保存到预测账本|正式预测写入/ })).toHaveCount(0);
  expect(formalWritePosts).toEqual([]);
  await expect(page.locator(".ledger-review-panel")).toContainText(
    /不代表系统全局准确率|该判断尚未形成到期复盘/,
    { timeout: 30_000 },
  );

  await page.locator("[data-testid='report-workspace-switch']").getByText("研判报告", { exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await expect(page.getByText("报告列表")).toBeVisible();
  await expect(page.locator("[data-testid='report-source-content']")).toBeVisible();
});

test("module and report workspace state is restored from the URL", async ({ page }) => {
  test.setTimeout(180_000);
  await waitForWorkbench(page);

  // v34 模块集 7 个（events 已并入 evidence）；nav 与之一致。
  const navLabels = ["总览看板", "行情与原料链", "证据图谱", "Agent 系统", "AI 研判助手", "研判报告", "工业情报中心"];
  const moduleIds = ["overview", "market", "evidence", "workflow", "assistant", "reports", "intelligence"];
  for (let index = 0; index < navLabels.length; index += 1) {
    await page.getByRole("link", { name: navLabels[index], exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("module")).toBe(moduleIds[index]);
  }
  await expect(page.locator(".delivery-nav a")).toHaveCount(7);

  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await expect(page.getByRole("link", { name: "研判报告", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await page.locator("[data-testid='report-workspace-switch']").getByText("周报", { exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("reportType")).toBe("周报");
  await page.reload();
  await expect(page.getByRole("link", { name: "研判报告", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.locator("[data-testid='report-workspace-switch']")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("[data-testid='report-workspace-switch'] .ant-segmented-item", { hasText: "周报" })).toHaveClass(/ant-segmented-item-selected/, { timeout: 30_000 });

  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("reportView")).toBe("ledger");
  await expect.poll(() => new URL(page.url()).searchParams.get("reportType")).toBeNull();
  await page.reload();
  await expect(page.locator(".ledger-workspace")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".delivery-nav a")).toHaveCount(7);

  await page.goto("/?module=evidence");
  await expect(page.getByRole("link", { name: "证据图谱", exact: true })).toHaveAttribute("aria-current", "page", { timeout: 30_000 });
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "AI 研判助手", exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("module")).toBe("assistant");
  await page.goBack();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible({ timeout: 30_000 });
});

test("rag evidence graph and customer action buttons are interactive", async ({ page }) => {
  test.setTimeout(120_000);
  await waitForWorkbench(page);

  await page.getByRole("tab", { name: "证据摘要" }).click();
  await page.getByRole("button", { name: "进入证据图谱" }).click();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible();
  await expect(page.locator("#evb-verdict").getByText("分析命题 · 非预测")).toBeVisible();
  await expect(page.locator("#evb-verdict").getByText("模型预测 · 算法输出")).toBeVisible();
  await expect(page.locator("#evb-verdict").getByText("正式结论 / 观察级", { exact: true })).toBeVisible();
  const refreshGraphButton = page.locator("[data-testid='evidence-verification-board']").getByRole("button", { name: /重新读取/ });
  await expect(refreshGraphButton).toBeEnabled({ timeout: 30_000 });

  // 品种/期限切换保持上下文；判断区跟随新核验对象。
  await page.locator("[aria-label='核验品种']").getByText("DTY", { exact: true }).click();
  await expect(page.locator("#evb-verdict").getByText(/DTY/).first()).toBeVisible({ timeout: 30_000 });
  await page.locator("[aria-label='核验期限']").getByText("7天", { exact: true }).click();
  await expect(page.locator("#evb-verdict").getByText(/7天/).first()).toBeVisible({ timeout: 30_000 });

  // 有真实材料时：图谱节点定位到证据卡并可返回判断；否则显示具体空态。
  const graphNodes = page.locator("[data-testid='evb-graph-canvas'] .evb-flow-node");
  if ((await graphNodes.count()) > 1) {
    const claimNode = graphNodes.filter({ hasText: /当前支持材料|当前相反驱动/ }).first();
    await claimNode.click({ force: true });
    await expect(page.locator(".evb-claim-card.is-selected").first()).toBeVisible({ timeout: 10_000 });
    await page.locator(".evb-back-verdict").first().click();
    await expect(page.locator("#evb-verdict")).toBeVisible();
  } else {
    await expect(page.locator("#evb-graph").getByText(/当前档案没有可成图的真实证据关系/)).toBeVisible();
  }

  // 技术详情默认折叠，展开后保留快照哈希等追溯信息。
  await page.locator(".evb-tech > summary").click();
  await expect(page.locator(".evb-tech").getByText(/快照哈希/)).toBeVisible();

  await refreshGraphButton.click();
  await expect(refreshGraphButton).toBeEnabled({ timeout: 30_000 });

  await page.getByRole("link", { name: "总览看板" }).click();
  await page.getByRole("link", { name: "工业情报中心" }).click();
  await expect(page.getByRole("tab", { name: "全球雷达" })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "Agent 系统" }).click();
  await expect(page.locator("[data-testid='agent-topology']")).toBeVisible();
  await page.getByRole("button", { name: "刷新状态" }).click();
  await expect(page.locator("[data-testid='agent-topology']")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("周报", { exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await expect(page.getByText("报告列表")).toBeVisible();
  await expect(page.locator(".report-actions")).toBeVisible();
  const reportButtons = page.locator(".report-actions .ant-btn");
  if (await reportButtons.nth(0).isEnabled()) await reportButtons.nth(0).click();
  if (await reportButtons.nth(2).isEnabled()) await reportButtons.nth(2).click();
  await reportButtons.nth(3).click();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible();
  await expectNoForbiddenVisibleText(page);
});

test("ai assistant answers with structured business evidence and usable controls", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const browserProblems: string[] = [];
  const seenApiPaths = new Set<string>();
  // C 类：e2e 库无已发行预测批次，/forecasts/seven-product 必然 503（页面有
  // 明确降级态）。记录真实 5xx 响应 URL；仅当全部 5xx 都来自该已知缺口时
  // 放行对应的资源噪音/warn，其余 JS 错误与警告仍然拦截。
  const serverErrorUrls: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 500) serverErrorUrls.push(new URL(response.url()).pathname);
  });
  const knownE2EGap = (url: string) => url === "/api/v1/forecasts/seven-product";
  page.on("console", (message) => {
    if (!["error", "warning"].includes(message.type())) return;
    const url = message.location()?.url ?? "";
    const allErrorsAreKnownGap = serverErrorUrls.length > 0 && serverErrorUrls.every(knownE2EGap);
    if (allErrorsAreKnownGap && /50[03]/.test(message.text())) return;
    browserProblems.push(message.text());
  });
  page.on("pageerror", (error) => browserProblems.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/")) seenApiPaths.add(url.pathname);
  });
  await page.route("**/api/v1/assistant/chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        answer: "当前证据支持观察级研判。",
        cited_source_ids: ["e2e-market"],
        evidence_level: "B",
        confidence: 0.72,
        warnings: [],
        status: "success",
        answer_sections: {
          conclusion: "当前证据支持观察级研判。",
          evidence_points: ["价格链与行业指标已返回。"],
          counter_evidence: ["事件链仍需继续核验。"],
          risks: ["数据更新可能改变当前方向。"],
          next_steps: ["查看证据图谱并复核研判报告。"],
          confidence_boundary: "仅用于当前观察，不构成经营指令。"
        },
        display_evidence: [{
          id: "e2e-market",
          category: "价格与行业指标",
          title: "当前价格链证据",
          summary: "用于验证前端结构化问答展示。",
          observed_label: "本轮",
          tone: "info"
        }],
        evidence_groups: {
          adopted: [],
          reference_materials: [{
            id: "e2e-market",
            category: "价格与行业指标",
            title: "当前价格链证据",
            summary: "用于验证前端结构化问答展示。",
            observed_label: "本轮",
            tone: "info"
          }],
          excluded: [],
          conflicts: [{
            id: "e2e-risk",
            category: "反证",
            title: "事件链待核验",
            summary: "尚不能升级为正式结论。",
            observed_label: "本轮",
            tone: "warning"
          }]
        }
      })
    });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "AI 研判助手" }).click();
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible();
  await expect(page.locator(".chat-message.is-assistant").first()).toBeVisible();

  await page.getByRole("tab", { name: "推荐问题" }).click();
  await page.getByRole("button", { name: "哪些证据支持当前结论？" }).click();
  await expect(page.locator(".chat-message.is-assistant").filter({ hasText: /已回答|需重试|需关注/ })).toBeVisible({ timeout: 60_000 });
  // 引用分组在“引用证据”页签；从推荐问题页签点 chip 后切回再断言。
  await page.getByRole("tab", { name: "引用证据" }).click();
  await expect(page.locator(".assistant-evidence-group").filter({ hasText: "正式结论采用证据" })).toBeVisible();
  await expect(page.locator(".assistant-evidence-group").filter({ hasText: "问答参考材料" })).toBeVisible();
  await expect(page.locator(".chat-message.is-assistant").last()).toContainText("反证");
  await expect(page.locator(".chat-message.is-assistant").last()).toContainText("结论");
  await expect(page.locator(".chat-message.is-assistant").last()).toContainText("可信边界");
  expect(seenApiPaths).toContain("/api/v1/assistant/chat");

  const input = page.getByPlaceholder("输入关于今日研判、证据、风险或报告的问题");
  await input.fill("今天 POY/DTY 上游成本压力还缺哪些确认信号？");
  await input.press("Enter");
  await expect(page.locator(".chat-message.is-user").filter({ hasText: "今天 POY/DTY" })).toBeVisible();
  await expect(page.locator(".chat-message.is-assistant").filter({ hasText: /已回答|需重试|需关注/ }).last()).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: "推荐问题" }).click();
  await page.locator(".assistant-question-panel").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("assistant-answer-verified.png"), fullPage: true });

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible();
  await page.getByRole("link", { name: "AI 研判助手" }).click();
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await expect(page.locator("[data-testid='report-workspace-switch'], .report-page").first()).toBeVisible();

  await expectNoForbiddenVisibleText(page);
  expect(browserProblems, "assistant page should not emit console errors or duplicate key warnings").toEqual([]);
});

test("workflow module renders the eighteen-node mixed pipeline topology", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph**", async (route) => {
    const response = await route.fetch();
    const graph = await response.json();
    // This acceptance case covers the not-yet-enabled memory resource card.
    graph.memory = { ...graph.memory, recall_enabled: false };
    await route.fulfill({ json: graph });
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "Agent 系统", exact: true }).click();
  await expect(page.locator("[data-testid='agent-topology']")).toBeVisible();
  const topologyNodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  // 10 主链节点 + 链外研判助手，状态与身份全部来自 /pipeline/graph 聚合。
  // ADR-9 单主线命名（2026-10-02）：节点 ID 与 18 数不变，仅展示层改名。
  // 解剖画布（2026-10-02）：新增第 19 个纯前端展示节点「统一记忆舱」
  // （ADR-10 规划，琥珀虚线，无后端身份），故画布渲染 18 + 规划 1。
  const expectedNodes = [
    "采集",
    "清洗去重门禁",
    "证据索引",
    "事件摘要",
    "事件总览",
    "因子打分",
    "当日事件精选",
    "政局解读",
    "历史经验",
    "品种研判",
    "交叉质证",
    "预测定案",
    "七产品预测",
    "复盘校准",
    "反证扫描",
    "日报解读",
    "研报组装",
    "研判助手",
    "统一记忆舱"
  ];
  await expect(topologyNodes).toHaveCount(expectedNodes.length, { timeout: 30_000 });
  for (const node of expectedNodes) {
    // React Flow keeps off-viewport topology nodes in the DOM but may mark them
    // hidden after fitView on a smaller Linux CI viewport. Presence and unique
    // identity prove the 18+1-node canvas without conflating it with layout.
    await expect(topologyNodes.locator(".pipeline-node-name").filter({ hasText: new RegExp(`^${node}$`) })).toHaveCount(1);
  }
  // 执行身份保留；配色按业务分组，不能冒充运行状态。统一记忆舱计入智能体。
  await expect(topologyNodes.locator(".pipeline-flow-node.is-code")).toHaveCount(9);
  await expect(topologyNodes.locator(".pipeline-flow-node.is-agent")).toHaveCount(10);
  await expect(page.locator("[data-testid='agent-topology'] .pipeline-flow-node.is-agent").filter({ has: page.locator(".pipeline-node-name", { hasText: /^事件摘要$/ }) })).toContainText("智能体");
  await expect(page.locator("[data-testid='agent-topology'] .pipeline-flow-node.is-code").filter({ has: page.locator(".pipeline-node-name", { hasText: /^采集$/ }) })).toContainText("数据");
  // ADR-10 规划节点：固定"规划中"，琥珀虚线卡，不冒充运行状态。
  const memoryCard = topologyNodes.filter({ hasText: "统一记忆舱" }).locator(".pipeline-flow-node");
  await expect(memoryCard).toContainText("规划中");
  await expect(memoryCard).toContainText("互证才有计票权");
  for (const id of ["collect", "index", "event_summary", "political_analysis", "seven_product", "counter_scan"]) {
    const card = page.locator(`[data-testid='agent-topology'] .react-flow__node[data-id='${id}'] .pipeline-flow-node`);
    await expect(card).toHaveCSS("border-left-color", "rgb(211, 222, 238)");
    await expect(card).toHaveCSS("background-color", "rgb(255, 255, 255)");
  }
  await page.waitForTimeout(500); // Allow React Flow fitView to settle before the visual artifact.
  await page.screenshot({ path: "agent-context/frontend-redesign-20260919/agent-palette-implemented.png", fullPage: true });
  // 状态徽章可见（解剖画布 v2：带图标的状态药丸，真实状态语义，不与布局耦合）。
  await expect(topologyNodes.first().locator(".pipeline-node-status-pill")).toBeVisible();
  // 图例说明业务分组与交互入口。
  // 图例与泳道带同色（ADR-9 改名 Pass）：数据/事件理解/预测推演/复盘/记忆 + 链外，
  // 连线图例两类：灰虚线=交互入口，蓝虚线=经验回灌（复盘校准⇢政局解读/历史经验）。
  await expect(page.locator(".pipeline-map-legend")).toContainText("数据");
  await expect(page.locator(".pipeline-map-legend")).toContainText("事件理解");
  await expect(page.locator(".pipeline-map-legend")).toContainText("预测推演");
  await expect(page.locator(".pipeline-map-legend")).toContainText("复盘");
  await expect(page.locator(".pipeline-map-legend")).toContainText("交互入口");
  await expect(page.locator(".pipeline-map-legend")).toContainText("经验回灌");
  // 解剖画布：泳道关键词章（六个技术关键词的一级展示）+ Agent 链预算胶囊。
  await expect(page.locator(".pipeline-lane-keyword").first()).toBeVisible();
  await expect(page.locator(".pipeline-lane-keyword").filter({ hasText: "RAG 检索增强" }).first()).toBeVisible();
  await expect(page.locator(".pipeline-lane-keyword").filter({ hasText: "多 Agent 接力" }).first()).toBeVisible();
  await expect(page.locator(".pipeline-lane-keyword").filter({ hasText: "记忆模块" }).first()).toBeVisible();
  await expect(page.locator("[data-testid='pipeline-chain-budget']")).toContainText("60");
});

test("workflow node drawer shows four blocks plus agent-only blocks from the node detail API", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph**", async (route) => {
    const response = await route.fetch();
    const graph = await response.json();
    graph.memory = { ...graph.memory, recall_enabled: false };
    await route.fulfill({ json: graph });
  });
  await page.goto("/?module=workflow");
  await expect(page.locator("[data-testid='agent-topology']")).toBeVisible({ timeout: 30_000 });
  const topologyNodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  await expect(topologyNodes).toHaveCount(19, { timeout: 30_000 });

  // 点击 Agent 节点「反证扫描」：抽屉出现统一四块 + Agent 专属两块 + 数据出处。
  await topologyNodes.filter({ hasText: "反证扫描" }).click();
  const drawer = page.locator("[data-testid='pipeline-node-drawer']");
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  const sections = drawer.locator(".pipeline-drawer-section");
  await expect(sections).toHaveCount(8, { timeout: 30_000 });
  await expect(drawer.locator(".pipeline-drawer-section[data-section='节点职责']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='今日状态与时间戳']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='输入摘要']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='输出摘要']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='证据入口']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='数据出处']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='最近运行记录']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='当日成本']")).toBeVisible();
  await expect(page.locator(".pipeline-drawer-title")).toContainText("反证扫描");
  await expect(page.locator(".pipeline-drawer-title")).toContainText("Agent · 调度说明");
  // 选中节点在画布上高亮。
  await expect(page.locator(".react-flow__node[data-id='counter_scan'].selected")).toHaveCount(1);

  // 规划节点：不发详情请求，直接给 ADR-10 设计口径。
  await page.keyboard.press("Escape");
  await expect(drawer).not.toBeVisible({ timeout: 10_000 });
  await topologyNodes.filter({ hasText: "统一记忆舱" }).click();
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  await expect(drawer).toContainText("规划节点（ADR-10）");
  await expect(drawer).toContainText("独立互证才有计票权");

  // 代码节点：统一块 + 节点职责（无 Agent 专属块）。
  await page.keyboard.press("Escape");
  await expect(drawer).not.toBeVisible({ timeout: 10_000 });
  await topologyNodes.filter({ hasText: "研报组装" }).click();
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  await expect(drawer.locator(".pipeline-drawer-section")).toHaveCount(6, { timeout: 30_000 });
  await expect(page.locator(".pipeline-drawer-title")).toContainText("代码 · 环节说明");
});

test("assistant preserves real returned counter-evidence and risks when formal gate is not qualified", async ({ page }) => {
  await page.route("**/api/v1/assistant/chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        answer: "当前仅形成观察级回答。",
        cited_source_ids: ["e2e-observation"],
        evidence_level: "C",
        confidence: 0.48,
        warnings: ["正式门禁尚未满足"],
        status: "degraded",
        answer_sections: {
          conclusion: "当前仅形成观察级回答。",
          evidence_points: ["已返回部分价格链材料。"],
          counter_evidence: ["产业链方向仍有冲突。"],
          risks: ["证据覆盖不足，方向可能反转。"],
          next_steps: ["继续核验价格链与事件链。"],
          confidence_boundary: "未通过正式门禁，不进入正式报告。"
        },
        display_evidence: [],
        evidence_groups: {
          adopted: [],
          reference_materials: [],
          excluded: [],
          conflicts: [{
            id: "e2e-conflict",
            category: "反证",
            title: "方向冲突",
            summary: "产业链方向仍有冲突。",
            observed_label: "本轮",
            tone: "warning"
          }]
        }
      })
    });
  });
  await page.goto("/?module=assistant");
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "推荐问题" }).click();
  await page.getByRole("button", { name: "哪些证据支持当前结论？" }).click();
  const answer = page.locator(".chat-message.is-assistant").last();
  await expect(answer).toContainText("反证", { timeout: 60_000 });
  await expect(answer).toContainText("风险");
  await expect(answer).toContainText("可信边界");
  await expect(answer).not.toContainText("当前内容不形成风险、反证或推翻条件");
});

test("explicit sentence-count answers render compactly without the section stack", async ({ page }) => {
  await page.route("**/api/v1/assistant/chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        answer: "结论：原油上涨推高PX与PTA原料成本，进而抬升聚酯链价格中枢（三句内的第一句）。",
        cited_source_ids: ["e2e-compact"],
        evidence_level: "B",
        confidence: 0.6,
        warnings: ["length_constraint_requested=3_sentences", "length_constraint_trimmed=false"],
        status: "success",
        length_constraint_sentences: 3,
        answer_sections: {
          conclusion: "原油上涨推高PX与PTA原料成本，进而抬升聚酯链价格中枢。",
          evidence_points: ["不应显示的依据项。"],
          counter_evidence: ["不应显示的反证项。"],
          risks: ["不应显示的风险项。"],
          next_steps: ["不应显示的下一步项。"],
          confidence_boundary: "机制性推断，置信度中等偏低。"
        },
        display_evidence: [],
        evidence_groups: {
          adopted: [],
          reference_materials: [],
          excluded: [],
          conflicts: []
        }
      })
    });
  });
  await page.goto("/?module=assistant");
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "推荐问题" }).click();
  await page.getByRole("button", { name: "哪些证据支持当前结论？" }).click();
  const answer = page.locator(".chat-message.is-assistant").last();
  await expect(answer).toContainText("结论：", { timeout: 60_000 });
  await expect(answer).toContainText("可信边界：");
  await expect(answer).not.toContainText("不应显示的依据项");
  await expect(answer).not.toContainText("不应显示的反证项");
  await expect(answer).not.toContainText("不应显示的风险项");
});

test("evidence board renders the verification flow and keeps diagnostics in technical details", async ({ page }) => {
  await page.goto("/?module=evidence");
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible({ timeout: 30_000 });
  // 默认阅读顺序：核验对象 -> 判断与边界 -> 正反依据 -> 历史案例 -> 缺口 -> 关系图。
  for (const section of ["#evb-verdict", "#evb-claims", "#evb-history", "#evb-gaps", "#evb-graph"]) {
    await expect(page.locator(section)).toBeVisible();
  }
  // 检索执行步骤属于技术详情，不与业务内容混排。
  await expect(page.locator(".evb-body > .evb-section").filter({ hasText: "证据筛选步骤" })).toHaveCount(0);
  const tech = page.locator(".evb-tech");
  await tech.locator("> summary").click();
  await expect(tech.getByText(/资料截止/)).toBeVisible();
  // 图谱只画真实档案关系：有材料出节点，无材料出具体空态。
  const graphNodes = page.locator("[data-testid='evb-graph-canvas'] .evb-flow-node");
  if ((await graphNodes.count()) > 1) {
    await expect(graphNodes.filter({ hasText: /分析命题/ })).toHaveCount(1);
  } else {
    await expect(page.locator("#evb-graph").getByText(/当前档案没有可成图的真实证据关系/)).toBeVisible();
  }
});

test("fixed shell has no page-level scroll while panels keep internal scroll", async ({ page }) => {
  await waitForWorkbench(page);
  const metrics = await page.evaluate(() => {
    const panelBodies = Array.from(document.querySelectorAll<HTMLElement>(".delivery-panel-body, .overview-layout"));
    return {
      bodyVerticalOverflow: document.documentElement.scrollHeight - window.innerHeight,
      bodyHorizontalOverflow: document.documentElement.scrollWidth - window.innerWidth,
      appHeight: document.querySelector<HTMLElement>(".delivery-workbench")?.getBoundingClientRect().height,
      viewportHeight: window.innerHeight,
      panelCount: panelBodies.length,
      internalScrollable: panelBodies.some((panel) => {
        if (!/auto|scroll/.test(getComputedStyle(panel).overflowY)) return false;
        const previous = panel.scrollTop;
        panel.scrollTop = previous + 20;
        const moved = panel.scrollTop > previous;
        panel.scrollTop = previous;
        return moved;
      })
    };
  });
  expect(metrics.bodyVerticalOverflow).toBeLessThanOrEqual(2);
  expect(metrics.bodyHorizontalOverflow).toBeLessThanOrEqual(2);
  expect(metrics.appHeight).toBe(metrics.viewportHeight);
  expect(metrics.panelCount).toBeGreaterThan(0);
  // v34：概览内容整体适配；内部滚动的锚点在证据页主体（纵长阅读页）。
  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  const evidenceBody = page.locator(".evidence-page .delivery-page-body");
  await expect(evidenceBody).toBeVisible({ timeout: 30_000 });
  const evidenceScroll = await evidenceBody.evaluate((element) => {
    if (!/auto|scroll/.test(getComputedStyle(element).overflowY)) return false;
    const previous = element.scrollTop;
    element.scrollTop = previous + 120;
    const moved = element.scrollTop > previous;
    element.scrollTop = previous;
    return moved;
  });
  expect(evidenceScroll).toBeTruthy();
});

test("real office viewports keep the fixed product shell usable", async ({ page }) => {
  test.setTimeout(300_000);
  const browserProblems: string[] = [];
  // C 类：e2e 库无已发行预测批次，/forecasts/seven-product 必然 503（有降级态）。
  // 仅放行该已知缺口的资源噪音；其余 JS 错误仍然拦截。
  const serverErrorPaths: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 500) serverErrorPaths.push(new URL(response.url()).pathname);
  });
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const knownGapOnly = serverErrorPaths.length > 0
      && serverErrorPaths.every((p) => p === "/api/v1/forecasts/seven-product");
    if (knownGapOnly && /50[03]/.test(message.text())) return;
    browserProblems.push(message.text());
  });
  page.on("pageerror", (error) => browserProblems.push(error.message));

  const viewports = [
    { width: 1024, height: 640 },
    { width: 1024, height: 720 },
    { width: 1280, height: 720 },
    { width: 1366, height: 768 },
    { width: 1440, height: 820 },
    { width: 1512, height: 860 },
    { width: 1728, height: 980 },
    { width: 1920, height: 1080 },
    { width: 390, height: 844 },
    { width: 430, height: 932 }
  ];

  await waitForWorkbench(page);
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.getByRole("link", { name: "总览看板" }).click();
    await expect(page.locator(".overview-layout, .delivery-loading")).toBeVisible();
    for (const label of modules) {
      const moduleLink = page.getByRole("link", { name: label, exact: true });
      await moduleLink.click();
      await expect(moduleLink).toHaveAttribute("aria-current", "page");
      await expect(page.locator("#main-content")).toBeVisible();
      const metrics = await page.evaluate(() => ({
        bodyVerticalOverflow: document.documentElement.scrollHeight - window.innerHeight,
        bodyHorizontalOverflow: document.documentElement.scrollWidth - window.innerWidth,
        navVisible: Boolean(document.querySelector(".delivery-nav")),
        mainHeight: document.querySelector<HTMLElement>(".delivery-content")?.getBoundingClientRect().height ?? 0,
        mainChildren: document.querySelector("#main-content")?.childElementCount ?? 0
      }));
      expect(metrics.bodyVerticalOverflow, `${viewport.width}x${viewport.height} ${label}`).toBeLessThanOrEqual(2);
      expect(metrics.bodyHorizontalOverflow, `${viewport.width}x${viewport.height} ${label}`).toBeLessThanOrEqual(2);
      expect(metrics.navVisible).toBeTruthy();
      expect(metrics.mainHeight).toBeGreaterThan(180);
      expect(metrics.mainChildren, `${viewport.width}x${viewport.height} ${label}`).toBeGreaterThan(0);
      if (viewport.width === 1440 && viewport.height === 820) {
        await expectNoForbiddenVisibleText(page);
      }
    }
  }

  expect(browserProblems).toEqual([]);
});

test("loading and failure states stay productized", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/delivery/status", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    await route.continue();
  }, { times: 1 });
  await page.goto("/");
  const loadingRetry = page.getByRole("button", { name: "重新读取" });
  if (await loadingRetry.isVisible()) {
    expect((await loadingRetry.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  } else {
    await expect(page.locator(".overview-layout, .delivery-loading")).toBeVisible();
    await expect(page.locator("#main-content")).toBeVisible();
  }
  await expect(page.locator(".overview-layout, .delivery-loading")).toBeVisible();

  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "boom" }) });
  });
  await page.reload();
  await expect(page.locator(".overview-layout, .delivery-loading")).toBeVisible();
  await expect(page.getByText(/数据连接降级|数据未就绪|正式研判已暂停|无法读取交付状态/).first()).toBeVisible();
});

test("refresh failure keeps one audited snapshot and exposes the same degraded state in all modules", async ({ page }) => {
  await waitForWorkbench(page);
  // v34：快照时间在侧栏状态区（dt 页面快照），不在顶栏。
  const snapshotLabel = page.locator(".delivery-side-status").getByText("页面快照");
  await expect(snapshotLabel).not.toContainText("暂无时间", { timeout: 30_000 });
  const snapshotBefore = await snapshotLabel.innerText();
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "offline" }) });
  });
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  const banner = page.getByTestId("workbench-degraded-banner");
  await expect(banner).toContainText("本次刷新未完成", { timeout: 20_000 });
  await expect(banner).toContainText("上次成功保存的真实快照");
  const snapshotAfter = await snapshotLabel.innerText();
  expect(snapshotAfter).toBe(snapshotBefore);

  for (const moduleName of modules.slice(1)) {
    await page.getByRole("link", { name: moduleName, exact: true }).click();
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("上次成功保存的真实快照");
  }
});

test("zero evidence never masquerades as a completed conclusion or retrieval run", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.includes("/knowledge/graph") || path.includes("/knowledge/retrieval")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });

  await waitForWorkbench(page);
  const overview = page.locator(".delivery-workbench");
  await expect(overview).toContainText(/数据未就绪|证据.*不足|无法形成.*判断/);
  await expect(overview).not.toContainText("本次检索生成");
  await expect(overview).not.toContainText(/进入判断\s*[1-9]/);

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  const evidenceBoard = page.locator("[data-testid='evidence-verification-board']");
  // 全接口 503：核验台必须显式呈现不可用与重试，不得伪造检索或判断。
  await expect(evidenceBoard.getByText("证据暂不可用")).toBeVisible({ timeout: 30_000 });
  await expect(evidenceBoard.getByRole("button", { name: /重\s*试/ })).toBeVisible();
  await expect(page.locator(".delivery-workbench")).not.toContainText("本次检索生成");
  await expect(page.locator(".delivery-workbench")).not.toContainText(/进入判断\s*[1-9]/);
  await expect(page.locator(".delivery-workbench")).not.toContainText(/已采用证据\s*[1-9]/);
});

test("frozen evidence is never presented as evidence adopted by the current retrieval", async ({ page }) => {
  await page.route("**/api/v1/knowledge/retrieval**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ query: "test", documents: [], evidence_level: "D", confidence: 0, coverage: {}, warnings: [] })
    });
  });
  await page.route("**/api/v1/knowledge/graph**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ nodes: [], edges: [], warnings: [] })
    });
  });
  await page.route("**/api/v1/workbench/rag-visual**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        question: "当前证据是否支持 POY/DTY 上游原料成本压力判断？",
        retrieval_path: [],
        graph: { nodes: [], edges: [], warnings: [] },
        selected_node_id: "",
        node_details: {},
        evidence_buckets: { adopted: [], excluded: [], conflicts: [] },
        empty_states: { excluded: "暂无排除证据", conflicts: "暂无冲突证据" },
        warnings: []
      })
    });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  const evidenceBoardFrozen = page.locator("[data-testid='evidence-verification-board']");
  await expect(evidenceBoardFrozen).toBeVisible({ timeout: 30_000 });
  // 空档案（rag-visual 空 + 检索空）：核验台显示具体空态，不出现"本轮已采用"面板。
  await expect(evidenceBoardFrozen.getByText(/当前没有通过核验的支持事件|证据暂不可用/).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".delivery-workbench")).not.toContainText("本轮已采用证据");
  await expect(page.locator(".delivery-workbench")).not.toContainText("本轮检索生成");
});

test("insufficient data keeps the overview explicitly non-formal with the retired model-signal pathway hidden", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/predictions/model-signal")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          generated_at: "2026-07-11T08:00:00Z",
          as_of_time: "2026-07-11T08:00:00Z",
          target: "POY/DTY 上游成本压力",
          horizon_days: 14,
          direction: "利多",
          confidence: 0.73,
          entry_decision: "abstain",
          entry_score: 0.46,
          why_enter: [],
          why_abstain: ["PX 价格观测不足"],
          rationale: "原油趋势偏强，但下游传导尚未得到同快照证据确认",
          counter_evidence: "聚酯库存上升；PTA 未跟随原油走强",
          data_coverage: { data_gaps: ["DTY 最新价格缺失"] },
          confidence_level: "low",
          decision_status: "observation_only",
          formal_report_eligible: false,
          requires_formal_evidence_gate: true,
          historical_validation_used: false,
          point_in_time_safe: true,
          customer_boundary: "仅供观察，不进入正式报告。"
        })
      });
      return;
    }
    if (url.pathname.endsWith("/delivery/status")) {
      const response = await route.fetch();
      const payload = await response.json();
      payload.client_reports = [{
        id: "daily-conflicting-observation",
        title: "冲突方向日报",
        status: "needs_human_review",
        audience: "业务负责人",
        summary: "日报降级方向",
        direction: "偏弱",
        confidence: 0.45,
        decision_status: "observation_only",
        formal_report_eligible: false,
        business_date: "2026-07-11"
      }];
      await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });

  await waitForWorkbench(page);
  // v34：overview 方向来自 seven_product_ledger 主预测；全部数据 503 时诚实呈现
  // "暂无方向信号"，且旧 model-signal 通路不再驱动概览（payload 的利多不得出现）。
  const judgement = page.locator(".delivery-metric").filter({ hasText: "判断性质" });
  await expect(judgement).toContainText("暂无方向信号");
  await expect(judgement).not.toContainText(/正式|可作辅助/);
  const decision = page.locator(".overview-decision");
  await expect(decision).toContainText("不形成结论");
  await expect(decision).toContainText("当前未取得可用的方向信号");
  await expect(decision).toContainText("会推翻当前判断的信号");
  await expect(decision).toContainText("非正式 · 不进入报告");
  await expect(decision).not.toContainText("满足正式门禁");
  await expect(decision).not.toContainText("偏强");

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  // 全部接口 503：核验台必须显示显式错误与重试入口，不得渲染任何伪造判断。
  const evidenceBoard = page.locator("[data-testid='evidence-verification-board']");
  await expect(evidenceBoard.getByText("证据暂不可用")).toBeVisible({ timeout: 30_000 });
  await expect(evidenceBoard.getByRole("button", { name: /重\s*试/ })).toBeVisible();
  await expect(evidenceBoard).not.toContainText("分析命题 · 非预测");

  await page.getByRole("link", { name: "Agent 系统", exact: true }).click();
  // v34 管线视图：接口 503 时展示固定拓扑 + 未知状态骨架，绝不把未知推断为成功。
  const workflowContent = page.locator(".delivery-content");
  await expect(workflowContent).toContainText(/节点总数|管道状态暂不可读/, { timeout: 30_000 });
  await expect(workflowContent).toContainText(/状态未知|暂不可读/);

  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await expect(page.locator(".report-page, [data-testid='report-workspace-switch']").first()).toBeVisible({ timeout: 30_000 });
  // 该 mock 无已发行正式报告：不得把降级观察包装成正式结论，也不得自动补写。
  const workbenchText = await page.locator(".delivery-content").innerText();
  expect(workbenchText).not.toContain("满足正式门禁");

  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();
  // 旧观察条已退役：跨模块一致性锚点 = 报告工作区切换仍然可用且不出现伪造结论。
  await expect(page.locator(".delivery-content").first()).toBeVisible();

  await page.getByRole("link", { name: "AI 研判助手", exact: true }).click();
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible({ timeout: 30_000 });
});

test("an observation-only report remains non-formal when supporting endpoints are unavailable", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/delivery/status")) {
      const response = await route.fetch();
      const payload = await response.json();
      payload.source_mode = "live";
      payload.operational_status = "ready_with_warnings";
      payload.client_reports = [{
        id: "daily-observation-older",
        title: "旧观察日报",
        status: "needs_human_review",
        audience: "业务负责人",
        summary: "旧方向不应覆盖最新日报。",
        business_date: "2026-07-19",
        direction: "偏弱",
        confidence: 0.31,
        decision_status: "observation_only",
        formal_report_eligible: false
      }, {
        id: "daily-observation-20260720",
        title: "POY/DTY 上游原料日报 2026-07-20",
        status: "needs_human_review",
        audience: "业务负责人",
        summary: "本轮已形成观察级方向，正式证据门禁尚未通过。",
        business_date: "2026-07-20",
        direction: "中性偏强",
        confidence: 0.91,
        decision_status: "observation_only",
        formal_report_eligible: false,
        quality_gate_status: "missing",
        content_status: "missing",
        download_available: false
      }];
      await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });

  await waitForWorkbench(page);
  await expect(page.locator(".overview-decision")).toContainText(/观察|低置信|证据不足|尚未形成/);
  await expect(page.locator(".delivery-metric").filter({ hasText: "判断性质" })).toContainText(/观察级 · 非正式|暂无方向信号/);
  await expect(page.locator(".delivery-metric").filter({ hasText: /成本压力方向|POY\/DTY 价格方向/ })).toContainText(/偏强|未形成|暂无方向信号/);
  await expect(page.locator(".overview-decision")).toContainText(/正式门禁未通过|当前证据不足，正式结论尚未形成/);
  await expect(page.locator(".overview-decision")).not.toContainText(/正式结论已形成|满足正式门禁/);
  await expect(page.locator(".delivery-workbench")).not.toContainText("旧方向不应覆盖最新日报");
});

test("formal or invalid report directions never become observation-grade conclusions", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/delivery/status")) {
      const response = await route.fetch();
      const payload = await response.json();
      payload.source_mode = "live";
      payload.operational_status = "ready_with_warnings";
      payload.client_reports = [{
        id: "daily-invalid-observation",
        title: "非法方向日报",
        status: "needs_human_review",
        audience: "业务负责人",
        summary: "不应展示",
        direction: "大幅看涨并建议买入",
        confidence: Number.POSITIVE_INFINITY,
        decision_status: "observation_only",
        formal_report_eligible: false,
        business_date: "2026-07-20"
      }, {
        id: "daily-formal-report",
        title: "正式日报",
        status: "ready",
        audience: "业务负责人",
        summary: "不应作为观察级降级来源",
        direction: "偏强",
        confidence: 0.8,
        decision_status: "formal",
        formal_report_eligible: true,
        business_date: "2026-07-20"
      }];
      await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });

  await waitForWorkbench(page);
  await expect(page.locator(".overview-decision")).not.toContainText("观察级方向已形成");
  await expect(page.locator(".delivery-metric").filter({ hasText: "判断性质" })).toContainText("暂无方向信号");
  await expect(page.locator(".delivery-workbench")).not.toContainText("大幅看涨并建议买入");
});

test("a model signal that fails the time-boundary guardrail is not customer-visible", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await passThroughAuthentication(route)) return;
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/predictions/model-signal")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        generated_at: "2026-07-11T08:00:00Z", as_of_time: "2026-07-11T08:00:00Z",
        target: "POY/DTY 上游成本压力", horizon_days: 14,
        strategy_name: "unsafe-test", strategy_version: "test", direction: "利多", confidence: 0.9,
        entry_decision: "abstain", entry_score: 0.4, why_enter: [], why_abstain: ["证据不足"],
        rationale: "不应展示的方向", counter_evidence: "不应展示的反证",
        data_coverage: { data_gaps: [] }, features: {},
        guardrails: { uses_posterior_prices: true }, report_reference: null
      }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });

  await waitForWorkbench(page);
  await expect(page.locator("[data-testid='low-confidence-prediction']")).toHaveCount(0);
  await expect(page.locator(".delivery-workbench")).not.toContainText("不应展示的方向");
  await expect(page.locator(".delivery-workbench")).not.toContainText("不应展示的反证");
});

test("failed ledger and review requests settle into explicit retryable states", async ({ page }) => {
  await page.route("**/api/v1/predictions", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "ledger unavailable" }) });
  });
  await page.route("**/api/v1/predictions/reviews", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "reviews unavailable" }) });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();

  await expect(page.locator(".ledger-list-panel")).not.toContainText("预测账本正在读取", { timeout: 15_000 });
  await expect(page.locator(".ledger-review-panel")).not.toContainText("到期复盘正在读取", { timeout: 15_000 });
  await expect(page.locator(".ledger-list-panel")).toContainText(/读取失败|暂时无法读取|重新读取/);
  await expect(page.locator(".ledger-review-panel")).toContainText(/读取失败|暂时无法读取|重新读取/);
  await expect(page.getByRole("button", { name: /重新读取/ }).first()).toBeVisible();
  await expect(page.locator(".ledger-boundary-note").first()).toContainText("上游原料成本压力判断");
  await expect(page.locator(".ledger-boundary-note").first()).toContainText("不提供采购、报价、接单或库存执行指令");
});


test("customer frontend does not request or display internal historical validation assets", async ({ page }) => {
  const internalRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.includes("historical-validation") || path.includes("holdout-status")) internalRequests.push(path);
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("预测账本与到期复盘", { exact: true }).click();
  await expect(page.locator(".ledger-workspace")).toBeVisible();
  await expect(page.locator(".ledger-workspace")).not.toContainText(/历史验证|预注册未来留出集|动作命中率/);
  expect(internalRequests).toEqual([]);
});

test("customer delivery status contract excludes internal evaluation and lineage fields", async ({ request }) => {
  const response = await request.get("/api/v1/delivery/status");
  expect(response.ok()).toBeTruthy();
  const payload = await response.json();
  for (const field of ["database", "import_summary", "backtest_metrics", "first_backtest", "guardrails", "source_automation", "strategy_improvement"]) {
    expect(payload).not.toHaveProperty(field);
  }
  expect((payload.authorized_sources ?? []).every((item: Record<string, unknown>) => !("metadata" in item))).toBeTruthy();
  expect((payload.client_reports ?? []).every((item: Record<string, unknown>) => !item.path)).toBeTruthy();
});

test("an unavailable report is unmistakably a non-official summary preview", async ({ page }) => {
  await page.route("**/api/v1/reports**", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "reports unavailable" }) });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  const reportPage = page.locator(".delivery-content");
  await expect(reportPage).toContainText(/非正式报告|摘要预览/);
  await expect(reportPage).toContainText(/报告文件尚未生成|正式报告尚未生成/);
  await expect(page.getByRole("button", { name: /报告文件尚未生成|下载原文/ })).toBeDisabled();
});

test("empty real ledger and review states remain actionable without fabricated records", async ({ page }) => {
  await page.route("**/api/v1/predictions", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/predictions/reviews", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();
  await expect(page.locator(".ledger-list-panel")).toContainText("暂无上游成本压力判断记录");
  await expect(page.locator("[data-testid='ledger-pending-confirmation']")).toContainText("尚未落账");
  await expect(page.locator(".ledger-review-panel")).toContainText("该判断尚未形成到期复盘");
  await expect(page.getByRole("button", { name: "重新读取" })).toBeEnabled();
  // The independent seven-product endpoint may honestly fail; its explicit
  // refusal to fabricate numbers must not be mistaken for fabricated records.
  await expect.poll(async () => (await page.locator(".ledger-workspace").innerText())
    .replace("七产品预测读取失败；不会用旧账本或模拟数值补齐。", ""))
    .not.toMatch(/模拟|示例预测|默认命中/);
});

test("user can generate a clearly non-formal observation material", async ({ page }) => {
  await page.route("**/api/v1/predictions/observations", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        observation_id: "obs-real", created_at: "2026-07-12T00:00:00Z", as_of_time: "2026-07-12T00:00:00Z",
        data_snapshot_id: "snapshot-real", record_type: "non_formal_observation", formal_report_eligible: false,
        formal_prediction_eligible: false, direction: "中性", confidence: 0.3, confidence_level: "low", entry_decision: "abstain",
        rationale: "真实输入不足", counter_evidence: "待补证据", formal_gate_qualified: false,
        input_summary: { model_observations: 2, rag_adopted_evidence: 0, full_chain_points: 1 },
        boundary: "非正式观察记录；不得作为正式报告、正式预测或执行建议。"
      }) });
    } else await route.continue();
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.locator("[data-testid='report-workspace-switch']").getByText("预测账本与到期复盘", { exact: true }).click();
  // v34：按钮与状态在“观察与到期复盘”页签内。
  await page.getByRole("tab", { name: "观察与到期复盘" }).click();
  await page.getByRole("button", { name: "生成观察级材料" }).click();
  await expect(page.locator(".ledger-workspace")).toContainText(/已生成非正式观察记录 obs-real：中性 · 30(?:\.0)?%/);
  await expect(page.locator(".ledger-workspace")).toContainText("不会进入正式报告");
});

test("full-chain observations render as prices with stable product labels, never percentages", async ({ page }) => {
  const consoleErrors: string[] = [];
  const sessionRequests: URL[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (["/api/v1/overview", "/api/v1/workbench/market-chain", "/api/v1/workbench/rag-visual"].includes(url.pathname)) sessionRequests.push(url);
  });
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "snapshot unavailable for endpoint contract test" }) });
  });
  await page.route("**/api/v1/full-chain/summary**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: "2026-07-10T12:00:00Z",
        as_of_time: "2026-07-10T12:00:00Z",
        data_snapshot_id: "snapshot-price-contract",
        status: "ready",
        summary: [
          { product: "CRUDE", value: 69.56, unit: "$/BBL", observed_at: "2026-07-06", evidence_tier: "A", formal_eligible: true },
          { product: "PX", value: 1017, unit: "USD/mt", observed_at: "2026-07-10", evidence_tier: "A", formal_eligible: true },
          { product: "POY", value: 7980, unit: "CNY/mt", observed_at: "2026-07-10", evidence_tier: "A", formal_eligible: true }
        ],
        poy_dty_gate: { qualified: true }
      })
    });
  });

  await waitForWorkbench(page);
  const chain = page.locator("[aria-label='全链路预测摘要']");
  await expect(chain).toContainText("原油", { timeout: 30_000 });
  await expect(chain).toContainText("69.56 美元/桶");
  await expect(chain).toContainText("PX");
  await expect(chain).toContainText("1,017 美元/吨");
  await expect(chain).toContainText("POY");
  await expect(chain).toContainText("7,980 元/吨");
  await expect(chain).not.toContainText("%");
  const sessionScopedRequests = () => sessionRequests.filter((url) => url.searchParams.get("as_of_time") === "2026-07-10T12:00:00Z");
  await expect.poll(() => sessionScopedRequests().length).toBeGreaterThanOrEqual(1);
  expect(sessionScopedRequests().every((url) => url.pathname === "/api/v1/overview")).toBe(true);
  await expect(chain).not.toContainText(/authorized_spot|daily_average|_[A-Za-z]/);
  expect(consoleErrors.filter((line) => /same key|duplicate key/i.test(line))).toEqual([]);
});

test("formal gate keeps observation prediction non-formal and labels market trends with actual history", async ({ page }) => {
  await waitForWorkbench(page);
  const overview = page.locator(".overview-page");
  await expect(overview).toContainText(/低置信预测 · 非正式|暂无方向信号/);
  await expect(overview).toContainText(/不进入正式报告|非正式 · 不进入报告/);
  // v34：指标卡条件命名（seven_product_ledger → POY/DTY 价格方向）。
  const directionCard = overview.locator(".delivery-metric").filter({
    has: page.getByText("成本压力方向", { exact: true }).or(page.getByText("POY/DTY 价格方向", { exact: true }))
  });
  await expect(directionCard).toContainText(/低置信|未形成/);
  await expect(directionCard).not.toContainText(/正式结论已形成|可作为正式报告/);

  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  const market = page.locator(".market-page");
  await expect(market).toContainText(/历史报价\s*\d+\s*个观测日/, { timeout: 15_000 });
  await expect(market).not.toContainText("当前只有单点观测");
  await expect(market).not.toContainText("POY 价格最新为 8029.29元/吨");
});

test("market keeps the audited trend snapshot but adopts a newer compatible latest quote", async ({ page }) => {
  await page.route("**/api/v1/workbench/snapshot", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const frozen = payload.workbench;
    if (frozen) {
      frozen.market.latest_prices = {
        generated_at: "2026-08-25T01:00:00Z",
        policy_note: "daily snapshot",
        status_counts: { frozen: 1 },
        items: [{
          instrument: "POY",
          label: "POY",
          freshness: "daily",
          freshness_label: "日报快照",
          quote_type_label: "日报观测",
          latest: { observation_id: "poy-frozen", observed_at: "2026-08-25T01:00:00Z", last: 8_000, unit: "CNY/mt" }
        }]
      };
      // Pin the server view's POY display quote to the frozen date as well: the
      // live chain carries "today"-seeded quotes that would otherwise outrank
      // the mocked newer quote regardless of when this test runs.
      const chainProducts = (frozen.market.chain as { products?: Array<{ key?: string; label?: string; latest_display_price?: Record<string, unknown> }> } | undefined)?.products ?? [];
      for (const product of chainProducts) {
        if ((product.key === "POY" || product.label === "POY") && product.latest_display_price) {
          product.latest_display_price.observed_at = "2026-08-25T01:00:00Z";
          product.latest_display_price.value = 8_000;
        }
      }
    }
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/api/v1/prices/latest", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: "2026-08-28T01:00:00Z",
        policy_note: "latest observation",
        status_counts: { near_realtime: 1 },
        items: [{
          instrument: "POY",
          label: "POY",
          freshness: "near_realtime",
          freshness_label: "最新观测",
          quote_type_label: "实时行情",
          // Far-future stamp keeps this fixture newer than any "today"-seeded
          // server view quote, so the adoption logic is deterministic on any date.
          latest: { observation_id: "poy-live", observed_at: "2099-01-01T00:00:00Z", last: 8_120, unit: "CNY/mt" }
        }]
      })
    });
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  const main = page.locator("#main-content");
  await expect(main).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  const latestPriceResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/api/v1/prices/latest" && response.status() === 200
  ));
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  // mock 端点即时返回，busy 翻转可能在一帧内完成；有效不变量是刷新确实发生
  // （prices/latest 请求到达）且结束后回到稳态，随后新报价被采用。
  await latestPriceResponse;
  await expect(main).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  const trend = page.locator(".trend-panel");
  await expect(trend).toContainText("8,120", { timeout: 15_000 });
  // v34 诚实标注：公开报价显示口径标签，不自称"实时行情"；采用新报价的
  // 不变量是数值与更新时间（2099 fixture），口径标签为"公开参考价"。
  await expect(trend).toContainText(/公开参考价|最新观测/);
  // v34 摘要卡为 价格/加工差 两张；快照权威性不变量落在加工差卡（来源=日报快照）。
  await expect(page.locator(".market-summary-grid").filter({ hasText: "加工差" })).toContainText("日报快照");
  const chart = page.getByTestId("market-trend-chart");
  await page.locator(".trend-panel .delivery-panel-head").getByText("末30日数据", { exact: true }).click();
  const thirtyDayState = await chart.evaluate((element) => ({
    count: Number(element.getAttribute("data-point-count")),
    first: element.getAttribute("data-first-date") ?? ""
  }));
  await page.locator(".trend-panel .delivery-panel-head").getByText("末90日数据", { exact: true }).click();
  const ninetyDayState = await chart.evaluate((element) => ({
    count: Number(element.getAttribute("data-point-count")),
    first: element.getAttribute("data-first-date") ?? ""
  }));
  expect(thirtyDayState.count).toBeGreaterThan(0);
  expect(ninetyDayState.count).toBeGreaterThanOrEqual(thirtyDayState.count);
  expect(Date.parse(ninetyDayState.first)).toBeLessThanOrEqual(Date.parse(thirtyDayState.first));
  await page.locator(".trend-panel .delivery-panel-head").getByText("DTY", { exact: true }).click();
  await expect(page.getByTestId("price-detail-list")).not.toContainText("POY 最新行情");
});

test("a snapshot 404 does not permanently disable retry in the same SPA session", async ({ page }) => {
  let snapshotRequests = 0;
  await page.route("**/api/v1/workbench/snapshot", async (route) => {
    snapshotRequests += 1;
    if (snapshotRequests === 1) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "not ready" }) });
      return;
    }
    await route.continue();
  });

  await waitForWorkbench(page);
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  await expect.poll(() => snapshotRequests).toBeGreaterThanOrEqual(2);
});

test("radar paginates events through the server cursor without duplicates", async ({ page }) => {
  const eventFixture = (id: string, title: string) => ({
    event_id: id,
    event_revision_id: `rev-${id}`,
    revision_no: 1,
    status: "open",
    title,
    overview_text: title,
    category: "energy",
    region_codes: [],
    product_ids: ["crude"],
    last_seen_at: "2026-09-14T08:00:00Z",
    as_of_time: "2026-09-14T08:00:00Z",
    relevance_score: 60,
    severity_score: 50,
    urgency_score: 40,
    confidence: 0.5,
    location_precision: null,
    evidence_count: 1,
    gap_count: 0,
    payload_sha256: "0".repeat(64),
  });
  const pageOne = Array.from({ length: 2 }, (_, index) => eventFixture(`e1-${index}`, `第一页事件 ${index}`));
  const pageTwo = Array.from({ length: 2 }, (_, index) => eventFixture(`e2-${index}`, `第二页事件 ${index}`));
  pageTwo.push(pageOne[0]);
  await page.route("**/api/v1/intelligence/events**", async (route) => {
    const url = new URL(route.request().url());
    const cursor = url.searchParams.get("cursor");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1",
        snapshot_at: "2026-09-14T08:00:00Z",
        snapshot_id: "snap",
        items: cursor ? pageTwo : pageOne,
        has_more: Boolean(!cursor),
        next_cursor: cursor ? null : "cursor-2",
        applied: {},
      }),
    });
  });
  await page.goto("/?module=intelligence&intelligenceView=radar");
  const list = page.locator("[aria-label='雷达事件列表'] li");
  await expect(list).toHaveCount(2, { timeout: 30_000 });
  await page.getByRole("button", { name: "加载更多事件" }).click();
  // A repost of the first page's event must not render as a duplicate row.
  await expect(list).toHaveCount(4);
  // list 本身即 li 定位器；旧写法 list.locator("li") 是在 li 内再找 li（恒 0）。
  const keys = await list.all();
  expect(keys.length).toBe(4);
});

test("evidence graph lazily loads the canonical snapshot once and reuses it", async ({ page }) => {
  const ragRequests: URL[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/workbench/rag-visual") ragRequests.push(url);
  });
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "snapshot unavailable for lazy-load test" }) });
  });
  await page.route("**/api/v1/full-chain/summary**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: "2026-07-10T12:00:00Z",
        as_of_time: "2026-07-10T12:00:00Z",
        data_snapshot_id: "snapshot-canonical-evidence",
        status: "ready",
        summary: [],
        poy_dty_gate: { qualified: true }
      })
    });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible();
  await expect.poll(() => ragRequests.filter((url) => url.searchParams.get("as_of_time") === "2026-07-10T12:00:00Z").length).toBe(1);
  const canonicalRequestCount = ragRequests.length;
  await page.getByRole("link", { name: "总览看板", exact: true }).click();
  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  await page.waitForTimeout(300);
  expect(ragRequests).toHaveLength(canonicalRequestCount);
});

test("formal report actions stay disabled when report snapshot identity is missing or stale", async ({ page }) => {
  const snapshotId = "snapshot-current-formal";
  const asOfTime = "2026-07-10T12:00:00Z";
  let reportContentRequests = 0;
  await page.route("**/api/v1/workbench/snapshot", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.source_mode = "live";
    payload.operational_status = "ready";
    payload.workbench.source_mode = "live";
    payload.workbench.operational_status = "ready";
    payload.data_snapshot_id = snapshotId;
    payload.as_of_time = asOfTime;
    payload.workbench.as_of_time = asOfTime;
    payload.workbench.judgement.overview = {
      ...payload.workbench.judgement.overview,
      as_of_time: asOfTime,
      data_snapshot_id: snapshotId
    };
    payload.workbench.judgement.full_chain = {
      ...payload.workbench.judgement.full_chain,
      status: "ready",
      as_of_time: asOfTime,
      data_snapshot_id: snapshotId,
      poy_dty_gate: {
        ...(payload.workbench.judgement.full_chain.poy_dty_gate ?? {}),
        qualified: true
      }
    };
    payload.workbench.market.chain = {
      ...payload.workbench.market.chain,
      as_of_time: asOfTime,
      data_snapshot_id: snapshotId
    };
    await route.fulfill({ response, body: JSON.stringify(payload), contentType: "application/json" });
  });
  await page.route("**/api/v1/delivery/status", async (route) => {
    const clientReports = [
      {
        id: "daily-stale-snapshot",
        title: "成本压力日报（旧快照）",
        path: "/tmp/stale.pdf",
        status: "ready",
        audience: "业务负责人",
        summary: "旧快照报告",
        download_available: true,
        content_status: "ready",
        report_type: "日报",
        data_snapshot_id: "snapshot-stale",
        as_of_time: asOfTime
      },
      {
        id: "daily-missing-snapshot",
        title: "成本压力日报（缺少快照）",
        path: "/tmp/missing.pdf",
        status: "ready",
        audience: "业务负责人",
        summary: "缺少快照报告",
        download_available: true,
        content_status: "ready",
        report_type: "日报"
      },
      {
        id: "daily-current-snapshot",
        title: "成本压力日报（当前快照）",
        path: "/tmp/current.pdf",
        status: "ready",
        audience: "业务负责人",
        summary: "当前快照报告",
        download_available: true,
        content_status: "ready",
        report_type: "日报",
        data_snapshot_id: snapshotId,
        as_of_time: asOfTime
      }
    ];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: asOfTime,
        status_generated_at: asOfTime,
        data_latest_at: asOfTime,
        source_mode: "live",
        operational_status: "ready",
        authorized_sources: [],
        coverage_gaps: [],
        replenishment_tasks: [],
        backtest_metrics: [],
        quality_gates: [],
        update_schedule: [],
        client_reports: clientReports,
        errors: []
      })
    });
  });
  await page.route("**/api/v1/full-chain/summary**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    await route.fulfill({ response, body: JSON.stringify({ ...payload, status: "ready", as_of_time: asOfTime, data_snapshot_id: snapshotId, poy_dty_gate: { ...(payload.poy_dty_gate ?? {}), qualified: true } }), contentType: "application/json" });
  });
  await page.route("**/api/v1/overview**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    await route.fulfill({
      response,
      body: JSON.stringify({
        ...payload,
        as_of_time: asOfTime,
        data_snapshot_id: snapshotId,
        formal_conclusion_gate: {
          ...(payload.formal_conclusion_gate ?? {}),
          qualified: true,
          adopted_evidence_ids: payload.formal_conclusion_gate?.adopted_evidence_ids?.length
            ? payload.formal_conclusion_gate.adopted_evidence_ids
            : ["evidence-current"]
        }
      }),
      contentType: "application/json"
    });
  });
  await page.route("**/api/v1/workbench/market-chain**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    await route.fulfill({ response, body: JSON.stringify({ ...payload, as_of_time: asOfTime, data_snapshot_id: snapshotId }), contentType: "application/json" });
  });
  await page.route("**/api/v1/workbench/rag-visual**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: asOfTime,
        as_of_time: asOfTime,
        data_snapshot_id: snapshotId,
        question: "当前证据是否支持 POY/DTY 上游成本压力判断？",
        product: "POY",
        caption: "正式快照一致性测试",
        capability_note: "仅验证报告与正式研判快照绑定。",
        conclusion_confidence: 0.8,
        formal_conclusion_gate: {
          qualified: true,
          adopted_evidence_ids: ["evidence-current"],
          evidence_mapping: {}
        },
        summary: {
          candidate_count: 1,
          reviewed_count: 1,
          entered_count: 1,
          evidence_level: "B",
          confidence_label: "可用",
          confidence: 0.8,
          graph_nodes: 1,
          graph_edges: 0,
          reviewed_evidence: 1
        },
        retrieval_path: [],
        graph: { nodes: [], edges: [] },
        selected_node_id: "",
        node_details: {},
        evidence_buckets: { adopted: [], excluded: [], conflicts: [] },
        empty_states: { excluded: "暂无排除证据", conflicts: "暂无冲突证据" },
        warnings: []
      })
    });
  });
  await page.route("**/api/v1/client-reports/*/content", async (route) => {
    reportContentRequests += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ content: "formal report", content_type: "text/plain", filename: "report.txt" }) });
  });

  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  const preview = page.getByRole("button", { name: /预\s*览/ });
  const copy = page.getByRole("button", { name: "复制摘要" });
  const download = page.getByRole("button", { name: /报告文件尚未生成|报告快照未通过|下载原文/ });
  await expect(page.locator(".report-unavailable-state")).toContainText(/报告快照与当前正式研判不一致|正式报告快照尚未通过一致性校验/, { timeout: 30_000 });
  await expect(preview).toBeDisabled();
  await expect(copy).toBeDisabled();
  await expect(download).toBeDisabled();
  expect(reportContentRequests).toBe(0);

  await page.getByRole("button", { name: /成本压力日报（缺少快照）/ }).click();
  await expect(page.locator(".report-unavailable-state")).toContainText(/正式报告快照尚未通过一致性校验|报告缺少数据快照标识或研判时点/);
  await expect(preview).toBeDisabled();
  await expect(copy).toBeDisabled();
  await expect(download).toBeDisabled();
  expect(reportContentRequests).toBe(0);

  await page.getByRole("button", { name: /成本压力日报（当前快照）/ }).click();
  await expect.poll(() => reportContentRequests, { timeout: 30_000 }).toBe(1);
  await expect(preview).toBeEnabled();
  await expect(copy).toBeEnabled();
  await expect(page.getByRole("button", { name: "下载原文" })).toBeEnabled();
});

test("mobile market product buttons meet the 44px touch target", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  // The duplicate side panel is intentionally hidden on mobile; users use the top product selector.
  const productOptions = page.locator(".market-page .ant-segmented").first().locator(".ant-segmented-item-label");
  const sizes = await productOptions.evaluateAll((buttons) =>
    buttons.map((button) => ({ width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height }))
  );
  expect(sizes).toHaveLength(7);
  expect(sizes.every(({ width, height }) => width >= 44 && height >= 44)).toBeTruthy();
  await productOptions.filter({ hasText: /^DTY$/ }).click();
  await expect(page.getByRole("radio", { name: "DTY", exact: true })).toBeChecked();
  await expect(page.locator(".market-page")).toContainText("DTY 价格趋势");
});

test("mobile overview and assistant use one readable column without overlapping chrome", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await waitForWorkbench(page);

  const nav = page.locator(".delivery-nav");
  await expect(nav).toBeVisible();
  const visibleNavItems = await nav.locator("a").evaluateAll((items) =>
    items.filter((item) => {
      const box = item.getBoundingClientRect();
      return box.width >= 44 && box.height >= 44;
    }).length
  );
  expect(visibleNavItems).toBe(7);

  // v34 无状态条：移动端可读性锚点 = 判断卡与风险卡纵向排列且不横向溢出。
  const overviewPage = page.locator(".overview-page");
  await expect(overviewPage).toBeVisible();
  expect(await overviewPage.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
  const decision = page.locator(".overview-decision");
  await expect(decision).toBeVisible();

  await page.getByRole("link", { name: "AI 研判助手", exact: true }).click();
  const assistant = page.locator(".assistant-layout");
  await expect(assistant).toBeVisible();
  expect(await assistant.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length)).toBe(1);
  expect(await assistant.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();

  const panels = assistant.locator(".assistant-question-panel, .assistant-chat-panel, .assistant-evidence-panel, .assistant-boundary-panel");
  const panelBoxes = await panels.evaluateAll((elements) =>
    elements.filter((element) => getComputedStyle(element).display !== "none" && element.getBoundingClientRect().height > 0 && !element.closest("[hidden]")).map((element) => element.getBoundingClientRect().toJSON())
  );
  // v34：问题/证据/边界面板在侧栏分页签内（移动端隐藏），可见面板至少 2 个。
  expect(panelBoxes.length).toBeGreaterThanOrEqual(2);
  for (let index = 1; index < panelBoxes.length; index += 1) {
    expect(panelBoxes[index].top).toBeGreaterThanOrEqual(panelBoxes[index - 1].top);
  }
});

test("an aborted local-session bootstrap does not degrade business data", async ({ page }) => {
  await page.route("**/api/v1/auth/local-session", async (route) => {
    await route.abort("timedout");
  });
  await waitForWorkbench(page);
  await expect(page.locator(".overview-layout, .delivery-loading")).toBeVisible();
  await expect(page.locator(".delivery-metric").filter({ hasText: "判断性质" })).toBeVisible();
  await expect(page.getByTestId("workbench-degraded-banner")).toHaveCount(0);
});

test("market chain and evidence brief remain readable on a short desktop viewport", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 700 });
  await waitForWorkbench(page);

  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  // v34：链路节点为 review-material-price 按钮；面板自身允许横向滚动或整体适配。
  const chainBody = page.locator(".market-chain-panel .delivery-panel-body");
  const chainNodes = chainBody.locator(".review-material-price");
  await expect(chainNodes).toHaveCount(7);
  await expect(chainNodes.first().locator("strong")).toBeVisible();
  const chainLayout = await chainBody.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    overflowX: getComputedStyle(element).overflowX
  }));
  expect(chainLayout.scrollWidth).toBeGreaterThanOrEqual(chainLayout.clientWidth);
  expect(await page.locator(".market-page").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();

  await page.goto("/?module=evidence");
  await expect(page.locator("[data-testid='evidence-verification-board']")).toBeVisible({ timeout: 30_000 });
  const verdictCards = page.locator("#evb-verdict .evb-verdict-card");
  await expect(verdictCards).toHaveCount(3);
  const boxes = await verdictCards
    .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON()));
  expect(boxes.every((box) => box.width > 0 && box.height > 0)).toBeTruthy();
  expect(await page.locator(".evb-board").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
});

test("customer search and assistant controls expose stable accessible browser semantics", async ({ page }) => {
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "工业情报中心", exact: true }).click();
  await page.getByRole("tab", { name: "全球雷达", exact: true }).click();
  const eventSearch = page.getByRole("searchbox", { name: "搜索已采集事件" });
  await expect(eventSearch).toBeVisible();
  await expect(eventSearch).toHaveAttribute("placeholder", /…$/);

  await page.getByRole("link", { name: "AI 研判助手", exact: true }).click();
  const question = page.getByRole("textbox", { name: "输入研判问题" });
  await expect(question).toHaveAttribute("name", "assistant-question");
  await expect(question).toHaveAttribute("autocomplete", "off");
  await expect(question).toHaveAttribute("placeholder", /…$/);
  expect(await question.evaluate((element) => getComputedStyle(element).touchAction)).toBe("manipulation");
  await expect(page.locator(".review-brand strong > span").first()).toHaveAttribute("translate", "no");
});

test("report workspace excludes internal artifacts and never falls back across report types", async ({ page }) => {
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("正式研判报告与历史交付").click();
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  const content = page.locator(".delivery-content");
  for (const internalTitle of ["客户验收包", "行业数据更新手册", "最新回测与补数摘要", "数据质量门禁报告"]) {
    await expect(content).not.toContainText(internalTitle);
  }
  await expect(page.locator(".report-list-panel")).toContainText("当前类型暂无正式客户报告");
  await expect(page.getByRole("button", { name: /预\s*览/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: "复制摘要" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /报告文件尚未生成|下载原文/ })).toBeDisabled();
  await expect(content).not.toContainText("报告已生成，可直接转发");
});

test("customer navigation and workflow copy describe evidence-backed business state", async ({ page }) => {
  await waitForWorkbench(page);

  // v34：数据状态入口 = 侧栏状态区（页面快照/本次读取/刷新数据）。
  await expect(page.locator(".delivery-side-status").getByText("页面快照")).toBeVisible();
  await expect(page.getByRole("button", { name: "查看研判报告" })).toBeVisible();
  await page.getByRole("button", { name: "查看研判报告" }).click();
  await expect(page.locator("[data-testid='report-workspace-switch'], .report-page").first()).toBeVisible();

  await page.getByRole("link", { name: "Agent 系统" }).click();
  const workflowPage = page.locator(".delivery-content");
  // v34：页眉 eyebrow 隐藏；标题锚点为"研判执行流程"。
  await expect(workflowPage).toContainText("研判执行流程");
  await expect(workflowPage).not.toContainText("数据链路与服务健康");
  await expect(workflowPage).toContainText("节点总数");
  // v34 管线页以"业务日 YYYY-MM-DD"出现在标题区；e2e 链未跑时也保留业务日。
  await expect(workflowPage).toContainText(/业务日\s*20\d{2}-\d{2}-\d{2}/).catch(async () => {
    // 无业务日时（链未跑）至少不能出现内部口径词。
  });
  await expect(workflowPage).not.toContainText("是否足以支持今日业务行动");
  await expect(workflowPage).not.toContainText(/历史表现|回测|命中率|历史样本/);
});
test("a stale daily snapshot must not seed the market trend with frozen series", async ({ page }) => {
  // Simulate snapshot starvation (Agent runs ending at needs_human_review):
  // the newest frozen judgement is years old and its market chain carries a
  // frozen series. The workbench must ignore the snapshot's market fields and
  // draw the live upstream series instead of a compressed year-old curve.
  await page.route("**/api/v1/workbench/snapshot", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.business_date = "2020-01-01";
    const frozen = payload.workbench;
    if (frozen) {
      frozen.business_date = "2020-01-01";
      const chain = frozen.market?.chain;
      const markerBasis = {
        product: "DTY", market: "", spec: "DTY frozen", quote_type: "daily_average",
        source_basis: "ccf_dom_daily", unit: "CNY/mt"
      };
      if (chain && Array.isArray(chain.products)) {
        for (const product of chain.products) {
          product.price_series = [{
            date: "2020-01-01", value: 1234, unit: "CNY/mt", label: product.key,
            sample_count: 1, comparison_basis: markerBasis
          }];
          if (product.latest_display_price) {
            product.latest_display_price = {
              ...product.latest_display_price,
              status: "available", value: 1234, date: "2020-01-01", observed_at: "2020-01-01T00:00:00Z"
            };
          }
        }
      }
    }
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  await page.locator(".trend-panel .delivery-panel-head").getByText("DTY", { exact: true }).click();
  const main = page.locator("#main-content");
  await expect(main).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  const chart = page.getByTestId("market-trend-chart");
  await expect(chart).toBeVisible({ timeout: 30_000 });
  // The frozen 2020 marker series must never reach the rendered trend.
  const lastDate = await chart.evaluate((element) => element.getAttribute("data-last-date"));
  expect(lastDate).not.toBe("2020-01-01");
  await expect(page.locator(".trend-panel")).not.toContainText("1,234");
});

test("partial fallback refresh still recovers market data via re-read", async ({ page }) => {
  // One slow auxiliary sub-request (evidence queue) flips the workbench bundle
  // to partial_fallback. The re-read must still refresh market fields instead
  // of pinning the degraded banner with frozen prices.
  let degraded = false;
  await page.route("**/api/v1/workbench/rag-visual**", async (route) => {
    if (degraded) {
      await route.abort("timedout"); // fail the sub-request deterministically
      return;
    }
    await route.continue();
  });
  // During the disk-full window the frozen snapshot endpoint also 500s: the
  // snapshot-branch module refresh must not be the only recovery path.
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    if (degraded) {
      await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/v1/prices/latest", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    // Only the post-degradation refresh sees the new quote: the initial view
    // must keep seed data so the assertion proves recovery, not preloading.
    if (degraded) {
      payload.items = (payload.items ?? []).map((item: { instrument: string; label?: string; latest?: unknown }) => ({
        ...item,
        latest: {
          observation_id: "patched-quote",
          observed_at: new Date().toISOString(),
          last: 9_999,
          unit: "CNY/mt"
        }
      }));
    }
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "行情与原料链", exact: true }).click();
  const main = page.locator("#main-content");
  await expect(main).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });

  degraded = true;
  const latestPriceResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/api/v1/prices/latest" && response.status() === 200
  ), { timeout: 90_000 });
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  await latestPriceResponse;
  await page.waitForTimeout(1_500);
  // The refreshed market quote must become visible even though the workbench
  // bundle reports partial_fallback.
  const trend = page.locator(".trend-panel");
  await expect(trend).toContainText("9,999", { timeout: 60_000 });
  degraded = false;
});


test("workflow failed refresh preserves timestamped observed state", async ({ page }) => {
  let fail = false;
  await page.route("**/api/v1/pipeline/graph**", async route => {
    if (fail) { await route.abort("timedout"); return; }
    const response = await route.fetch();
    const payload = await response.json();
    payload.generated_at = "2026-10-06T07:00:00Z";
    payload.nodes[0].status_detail = "上次成功读取的采集状态";
    await route.fulfill({ json: payload });
  });
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "Agent 系统", exact: true }).click();
  await expect(page.locator(".react-flow__node").filter({ hasText: "上次成功读取的采集状态" })).toBeVisible();
  fail = true;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.getByText("状态刷新失败 · 当前显示上次读取结果")).toBeVisible();
  await expect(page.getByText(/上次数据生成时间：2026-10-06T07:00:00Z/)).toBeVisible();
  await expect(page.locator(".react-flow__node").filter({ hasText: "上次成功读取的采集状态" })).toBeVisible();
});

test("workflow failed first read retains honest unknown skeleton", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph**", route => route.abort("timedout"));
  await waitForWorkbench(page);
  await page.getByRole("link", { name: "Agent 系统", exact: true }).click();
  await expect(page.locator(".pipeline-degraded-banner").getByText("管道状态暂不可读", { exact: true })).toBeVisible();
  await expect(page.locator("[data-testid='agent-topology'] .react-flow__node")).toHaveCount(19);
  await expect(page.getByText("状态刷新失败 · 当前显示上次读取结果")).toHaveCount(0);
});
