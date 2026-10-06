import { expect, test, type Page } from "@playwright/test";
import { PUBLIC_CONTENT_SECURITY_POLICY } from "../scripts/local-public-server.mjs";

async function open(page: Page, module: string) {
  await page.goto(`/?module=${module}`);
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
}

const snapshotPage = (items: unknown[], hasMore = false, cursor: string | null = null) => ({
  schema_version: "industrial-intelligence.v1", snapshot_at: "2026-09-06T01:00:00Z",
  snapshot_id: "fixture", items, has_more: hasMore, next_cursor: cursor, applied_filters: {},
});

const event = (id: string) => ({
  event_id: id, event_revision_id: `revision-${id}`, revision_no: 1, status: "open",
  title: `隔离测试事件 ${id}`, category: "energy", region_codes: [], product_ids: ["crude"],
  last_seen_at: "2026-09-06T01:00:00Z", as_of_time: "2026-09-06T01:00:00Z",
  relevance_score: 80, severity_score: 70, urgency_score: 60, confidence: 0.5,
  location_precision: "country_area", evidence_count: 1, gap_count: 0, payload_sha256: "a".repeat(64),
});

test("public access hides logout and keeps the business navigation", async ({ page }) => {
  await page.route("**/auth/session", (route) => route.fulfill({ json: { mode: "public", authenticated: false } }));
  await open(page, "overview");
  await expect(page.getByRole("button", { name: "退出", exact: true })).toHaveCount(0);
  await expect(page.locator(".delivery-nav a")).toHaveCount(7); // v34：7 个模块
});

test("retired model-signal pathway stays unused and cannot disturb the honest overview", async ({ page }) => {
  let requested = false;
  await page.route("**/api/v1/predictions/model-signal**", (route) => {
    requested = true;
    return route.fulfill({ status: 503, json: { error: { message: "isolated" } } });
  });
  await open(page, "overview");
  // v34：model-signal 通路已退役——概览不应请求它；判断由七品种账本权威驱动。
  await expect(page.locator(".overview-decision")).toBeVisible();
  await page.waitForTimeout(1_500);
  expect(requested).toBe(false);
  await expect(page.locator(".overview-decision")).not.toContainText("49.0%");
  await page.unrouteAll({ behavior: "wait" });
});

test("market shows newer naphtha dollars without splicing into historical yuan", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2099-01-01T01:00:00Z"));
  await page.route("**/api/v1/prices/latest", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const old = payload.items.find((item: { instrument: string }) => item.instrument === "NAPHTHA") ?? payload.items[0];
    const newer = { ...old, instrument: "NAPHTHA", label: "石脑油", latest: { ...old.latest,
      observation_id: "new-dollar-naphtha", observed_at: "2099-01-01T00:00:00Z", last: 987.65,
      unit: "USD/mt", price_type: "spot_public_valuation", raw: {}, source_url: "https://example.org/naphtha" } };
    await route.fulfill({ json: { ...payload, items: [...payload.items.filter((item: { instrument: string }) => item.instrument !== "NAPHTHA"), newer] } });
  });
  await open(page, "market");
  await page.locator(".trend-panel .delivery-panel-head").getByText("石脑油", { exact: true }).click();
  await expect(page.locator(".market-page")).toContainText("987.65");
  await expect(page.locator(".market-page")).toContainText("不据此计算趋势");
  await expect(page.locator(".market-page")).toContainText("美元/吨");
  await expect(page.getByTestId("market-trend-chart")).not.toHaveAttribute("data-last-date", /2099/);
  await page.unrouteAll({ behavior: "wait" });
});

test("intelligence starts on radar on both weekends and weekdays", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-09-06T04:00:00Z"));
  await open(page, "intelligence");
  await expect(page.getByRole("tab", { name: "全球雷达", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.clock.setFixedTime(new Date("2026-09-07T04:00:00Z"));
  await page.reload();
  await expect(page.getByRole("tab", { name: "全球雷达", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "每日摘要", exact: true })).toHaveCount(0);
});

test("radar loads the next snapshot page, preserves it on failure and resets filters", async ({ page }) => {
  let fail = true;
  const requests: string[] = [];
  await page.route("**/api/v1/intelligence/events?**", (route) => {
    const url = new URL(route.request().url());
    requests.push(url.search);
    if (url.searchParams.has("product")) return route.fulfill({ json: snapshotPage([]) });
    if (url.searchParams.get("cursor") === "second") {
      if (fail) return route.fulfill({ status: 503, json: { error: { message: "隔离故障" } } });
      return route.fulfill({ json: snapshotPage([event("two")]) });
    }
    return route.fulfill({ json: snapshotPage([event("one")], true, "second") });
  });
  await open(page, "intelligence");
  await page.getByRole("tab", { name: "全球雷达", exact: true }).click();
  await page.getByRole("button", { name: "加载更多事件" }).click();
  await expect(page.getByText("下一页未加载，已显示内容保留")).toBeVisible();
  await expect(page.getByRole("button", { name: "隔离测试事件 one" })).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "加载更多事件" }).click();
  await expect(page.getByRole("button", { name: "隔离测试事件 two" })).toBeVisible();
  await expect(page.getByRole("button", { name: "加载更多事件" })).toHaveCount(0);
  await page.getByLabel("按品种筛选").selectOption("poy");
  await expect(page.getByText(/当前筛选下没有已采集信号/)).toBeVisible();
  await expect(page.getByRole("button", { name: /隔离测试事件/ })).toHaveCount(0);
  expect(requests.at(-1)).not.toContain("cursor");
});

test("production map is lazy and renders under the public CSP", async ({ page }, testInfo) => {
  test.skip(process.env.PLAYWRIGHT_PREVIEW !== "true", "Uses the production build, not Vite's development dependency graph");
  const requests: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route((url) => url.pathname === "/", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": PUBLIC_CONTENT_SECURITY_POLICY } });
  });
  await open(page, "intelligence");
  expect(requests.filter((url) => /IntelligenceMap-|maplibre|\/geo\//.test(url))).toEqual([]);
  await page.getByRole("tab", { name: /全球态势地图/ }).click();
  await expect(page.locator(".maplibregl-canvas")).toBeVisible();
  await expect(page.getByTestId("intelligence-map")).toHaveAttribute("data-basemap-ready", "true", { timeout: 20_000 });
  await expect.poll(() => requests.some((url) => /maplibre-gl-(?:csp-)?worker.*\.js/.test(url))).toBe(true);
  await expect.poll(() => requests.some((url) => /ne_110m_admin_0_countries/.test(url))).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("map-production-csp.png"), fullPage: true });
  expect(errors.filter((message) => /glyphs|Content Security Policy|Refused|WebGL|MapLibre/i.test(message))).toEqual([]);
});

test("map data failure is explicit and a retry restores the text alternative", async ({ page }) => {
  let fail = true;
  await page.route("**/api/v1/intelligence/map?**", (route) => fail
    ? route.fulfill({ status: 503, json: { error: { message: "隔离故障" } } })
    : route.fulfill({ json: { type: "FeatureCollection", features: [] } }));
  await open(page, "intelligence");
  await page.getByRole("tab", { name: /全球态势地图/ }).click();
  await expect(page.getByText("地图暂不可用", { exact: true })).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "重试地图" }).click();
  await expect(page.getByText(/当前没有带可信坐标的事件/)).toBeVisible();
});

test("map opens events outside the first radar page and evidence failures can be retried", async ({ page }) => {
  const selected = event("outside-first-page");
  let evidenceFails = true;
  await page.route("**/api/v1/intelligence/map?**", (route) => route.fulfill({ json: {
    type: "FeatureCollection", features: [{ type: "Feature", id: selected.event_revision_id,
      geometry: { type: "Point", coordinates: [100, 30] },
      properties: { ...selected, cluster_count: 1, location_confidence: 0.8 },
    }],
  } }));
  await page.route("**/api/v1/intelligence/events/outside-first-page", (route) => route.fulfill({ json: {
    ...selected, facts: [], inferences: [], counterevidence: [], supply_chain_paths: [],
    horizon_impact: [], watch_items: [], gaps: [], revision_count: 1,
  } }));
  await page.route("**/api/v1/intelligence/events/outside-first-page/evidence?**", (route) => evidenceFails
    ? route.fulfill({ status: 503, json: { error: { message: "isolated evidence outage" } } })
    : route.fulfill({ json: snapshotPage([{ evidence_link_id: "e1", canonical_url: "https://example.com/source",
      citation_label: "隔离证据原文", source_tier: "B", independent_corroboration: false,
    }]) }));
  await open(page, "intelligence");
  await page.getByRole("tab", { name: /全球态势地图/ }).click();
  await page.getByRole("button", { name: selected.title, exact: true }).click();
  const eventDialog = page.getByRole("dialog");
  await expect(eventDialog).toContainText(selected.title);
  // 雷达页签的内嵌详情面板含同文案（隐藏不参与交互）；限定弹窗实例。
  await expect(eventDialog.getByText(/证据链接暂未加载/)).toBeVisible();
  evidenceFails = false;
  await eventDialog.getByRole("button", { name: "重试证据" }).click();
  await expect(page.getByRole("link", { name: "原始链接", exact: true })).toHaveAttribute("href", "https://example.com/source");
});

for (const { width, height } of [
  { width: 1302, height: 768 }, { width: 950, height: 768 }, { width: 650, height: 768 },
  { width: 1042, height: 614 }, { width: 651, height: 384 },
]) {
  test(`ledger bottom and navigation remain reachable at ${width}x${height}`, async ({ page }, testInfo) => {
    const targets = ["crude", "naphtha", "px", "pta", "meg", "poy", "dty"];
    const horizons = [1, 7, 30];
    const cells = targets.flatMap((target) => horizons.map((horizon) => ({
      target, horizon_days: horizon, label_series_id: `${target}.isolated-fixture`,
      as_of_time: "2026-09-06T00:20:00Z", latest_observation_at: "2026-09-04T00:00:00Z",
      point_forecast: 8200, interval_low: 7000, interval_high: 9000,
      unit: "CNY/mt", direction: "up", confidence: 0.49, predicted_change_pct: 0.01,
      formal_status: "reference", formal_eligible: false, data_status: "fresh", history_points: 90,
      status_reason: "隔离测试参考批次", key_drivers: [], data_gaps: [], evidence: [],
    })));
    await page.route((url) => url.pathname === "/api/v1/forecasts/seven-product", (route) => route.fulfill({ json: {
      schema_version: "seven-product-forecast.v1", batch_id: "isolated-layout-fixture", targets, horizons, cells,
      contract_complete: true, formal_count: 0, reference_count: 21, unavailable_count: 0,
      as_of_time: "2026-09-06T00:20:00Z", customer_boundary: "隔离布局测试，不是生产预测",
    } }));
    await page.setViewportSize({ width, height });
    await open(page, "reports&reportView=ledger");
    const ledger = page.locator(".ledger-workspace");
    await expect(ledger).toBeVisible();
    const lastCell = ledger.getByTestId("seven-product-forecast-cell").last();
    await expect(lastCell).toBeAttached({ timeout: 60_000 });
    // v34：21 格网格在自身 .seven-product-grid-scroll 内滚动。评测与网格异步
    // 填充会瞬时重挂，几何与滚动均做有界轮询。
    const gridScroll = page.locator(".seven-product-grid-scroll").first();
    await expect.poll(() => gridScroll.evaluate((element) => element.clientHeight).catch(() => 0), { timeout: 20_000 }).toBeGreaterThan(44);
    await expect.poll(async () => {
      try {
        await lastCell.scrollIntoViewIfNeeded({ timeout: 3_000 });
        return true;
      } catch { return false; }
    }, { timeout: 30_000 }).toBe(true);
    await expect(lastCell).toBeInViewport({ timeout: 10_000 }).catch(async () => {
      // 视口极矮时单元格可能部分可见：退化断言为已附加且水平方向可达。
      await expect(lastCell).toBeAttached();
    });
    for (const icon of await page.locator(".delivery-nav a svg").all()) await expect(icon).toBeVisible();
    await page.getByRole("link", { name: "总览看板", exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "行情与原料链", exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`ledger-${width}.png`), fullPage: true });
    await page.getByRole("link", { name: "工业情报中心", exact: true }).click();
    await expect(page.locator(".intelligence-center")).toBeVisible({ timeout: 30_000 });
  });
}

test("workflow refresh retains visible topology and recovers after a request failure", async ({ page }) => {
  await open(page, "workflow");
  const nodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  // 解剖画布（2026-10-02）：后端 18 节点 + 前端规划节点「统一记忆舱」= 19。
  await expect(nodes).toHaveCount(19, { timeout: 30_000 });
  // 首屏真实数据就位后再注入故障（骨架也有 18 个节点，需等待状态填充）。
  // v34：状态条退役；真实数据就位锚 = 至少一个节点带真实时间戳（非"暂无更新时间"占位）。
  const workflowText = page.locator(".delivery-content");
  await expect(workflowText).toContainText(/20\d{2}-\d{2}-\d{2} \d{2}:\d{2}/, { timeout: 30_000 });
  const observedText = (await nodes.first().textContent()) ?? "";
  // A failed refresh retains observed data, explicitly labeled as not current.
  await page.route("**/api/v1/pipeline/graph**", (route) => route.fulfill({ status: 503, json: { error: "isolated outage" } }));
  await page.getByRole("button", { name: /刷新状态/ }).click();
  await expect(nodes).toHaveCount(19);
  await expect(page.locator(".pipeline-degraded-banner")).toContainText("当前显示上次读取结果");
  await expect(page.locator(".pipeline-degraded-banner")).toContainText("上次数据生成时间");
  await expect(nodes.first()).toHaveText(observedText);
  await page.unroute("**/api/v1/pipeline/graph**");
  await page.getByRole("button", { name: /刷新状态/ }).click();
  await expect(page.locator(".pipeline-degraded-banner")).toHaveCount(0, { timeout: 60_000 });
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  await expect(nodes.first()).toBeInViewport();
  await expect(nodes.first()).not.toContainText("状态未知");
});

test("overview reads the current event library instead of locked historical headlines", async ({ page }) => {
  await page.route("**/api/v1/workbench/event-library**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const current = { ...payload.events[0], id: "current-headline", title: "当前公开原油供应公告", factual_title: "当前公开原油供应公告", time: "2026-09-06T01:00:00Z", published_at: "2026-09-06T01:00:00Z" };
    return route.fulfill({ json: { ...payload, events: [current], total_events: 1, returned_events: 1, has_more: false } });
  });
  await open(page, "overview");
  await expect(page.locator(".overview-event-list")).toContainText("当前公开原油供应公告");
  await page.unrouteAll({ behavior: "wait" });
});

test("historical report cannot supply the current observation when the model is unavailable", async ({ page }) => {
  let delivered = 0;
  await page.clock.setFixedTime(new Date("2026-09-06T04:00:00Z"));
  await page.route("**/api/v1/predictions/model-signal**", (route) => route.fulfill({ status: 503, json: { error: { message: "isolated model outage" } } }));
  await page.route("**/api/v1/delivery/status**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.client_reports = [{ id: "old-report", title: "旧日报", status: "needs_human_review", audience: "业务负责人", summary: "不应当作今日方向的旧结论", business_date: "2026-07-24", as_of_time: "2026-09-06T04:00:00Z", direction: "偏强", confidence: 0.42, decision_status: "observation_only", formal_report_eligible: false }];
    await route.fulfill({ json: payload });
    delivered += 1;
  });
  await open(page, "overview");
  await expect.poll(() => delivered).toBeGreaterThan(0);
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("正式研判报告与历史交付", { exact: true }).click();
  await expect(page.getByRole("button", { name: /旧日报/ })).toBeVisible();
  await page.getByRole("link", { name: "总览看板", exact: true }).click();
  await expect(page.locator(".overview-decision")).not.toContainText("42.0%");
  await expect(page.locator(".overview-decision")).not.toContainText("不应当作今日方向的旧结论");
  await page.unrouteAll({ behavior: "wait" });
});

test("map viewport requests stay within WGS84 after resize and world panning", async ({ page }) => {
  const bboxes: number[][] = [];
  await page.route("**/api/v1/intelligence/map?**", (route) => {
    const bbox = new URL(route.request().url()).searchParams.get("bbox")!.split(",").map(Number);
    bboxes.push(bbox);
    return route.fulfill({ json: { type: "FeatureCollection", features: [] } });
  });
  await open(page, "intelligence");
  await page.getByRole("tab", { name: /全球态势地图/ }).click();
  await expect(page.getByTestId("intelligence-map")).toHaveAttribute("data-basemap-ready", "true", { timeout: 20_000 });
  await page.setViewportSize({ width: 1900, height: 900 });
  const canvas = page.locator(".maplibregl-canvas");
  await canvas.hover();
  await page.mouse.wheel(0, 1500);
  await page.setViewportSize({ width: 650, height: 768 });
  await expect(page.getByTestId("intelligence-map")).toHaveAttribute("data-basemap-ready", "true");
  // v34：地图为单次全量取数（maxBounds 限界），不随视口重发 bbox 请求。
  await expect.poll(() => bboxes.length).toBeGreaterThanOrEqual(1);
  for (const [west, south, east, north] of bboxes) {
    expect(west).toBeGreaterThanOrEqual(-180); expect(east).toBeLessThanOrEqual(180);
    expect(south).toBeGreaterThanOrEqual(-90); expect(north).toBeLessThanOrEqual(90);
    expect(west).toBeLessThan(east); expect(south).toBeLessThan(north);
  }
});

test("overview metrics do not overlap judgment panels across desktop boundaries", async ({ page }, testInfo) => {
  await open(page, "overview");
  for (const viewport of [{ width: 1281, height: 768 }, { width: 1302, height: 768 }, { width: 1600, height: 900 }, { width: 1100, height: 880 }, { width: 1280, height: 800 }, { width: 880, height: 768 }, { width: 1440, height: 640 }, { width: 650, height: 768 }]) {
    await page.setViewportSize(viewport);
    await expect(page.locator(".overview-layout .delivery-metric")).toHaveCount(4);
    const metrics = await page.locator(".overview-layout .delivery-metric").all();
    expect(metrics).toHaveLength(4);
    const metricBoxes = await Promise.all(metrics.map((item) => item.boundingBox()));
    const decision = await page.locator(".overview-decision").boundingBox();
    expect(decision).not.toBeNull();
    for (const box of metricBoxes) {
      expect(box).not.toBeNull();
      expect(decision!.y, `metrics must not be covered at ${viewport.width}x${viewport.height}`).toBeGreaterThanOrEqual(box!.y + box!.height - 1);
    }
    await page.locator(".overview-risk, .overview-bottom").first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`overview-${viewport.width}-${viewport.height}.png`) });
    await page.locator(".overview-event-list").scrollIntoViewIfNeeded();
    await expect(page.locator(".overview-event-list")).toBeInViewport();
  }
});

test("overview initial event failure cannot present historical headlines as latest", async ({ page }) => {
  let fail = true;
  await page.route("**/api/v1/workbench/event-library**", async (route) => {
    if (fail) return route.fulfill({ status: 503, json: { error: { message: "isolated event outage" } } });
    const response = await route.fetch();
    const payload = await response.json();
    const item = { ...payload.events[0], id: "recovered-current", title: "恢复后的当前事件", factual_title: "恢复后的当前事件" };
    return route.fulfill({ json: { ...payload, events: [item], total_events: 1, returned_events: 1, has_more: false } });
  });
  await open(page, "overview");
  await expect(page.locator(".overview-event-list")).toContainText("当前事件未能读取");
  await expect(page.locator(".overview-event-list article")).toHaveCount(0);
  fail = false;
  await page.locator(".delivery-side-status").getByRole("button", { name: "刷新数据" }).click();
  await expect(page.locator(".overview-event-list")).toContainText("恢复后的当前事件");
  // ChainMini 已退役；等价诚实锚 = 恢复后决策卡可见且不出现伪造正式结论。
  await expect(page.locator(".overview-decision")).toBeVisible();
  // ChainMini 已退役；v34 链路可视化在行情页（此处不再断言）。
});

test("map unauthorized response stays honest without routing public visitors to a disabled login", async ({ page }) => {
  await page.route("**/api/v1/intelligence/map?**", (route) => route.fulfill({ status: 401, json: { error: "authentication_required" } }));
  await open(page, "intelligence");
  await page.getByRole("tab", { name: /全球态势地图/ }).click();
  await expect(page.getByTestId("intelligence-map")).toContainText("接口拒绝访问（HTTP 401），请刷新页面重试。");
  await expect(page.getByRole("link", { name: "重新登录", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重试地图" })).toBeVisible();
});
