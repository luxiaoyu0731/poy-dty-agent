import { expect, test, type Page } from "@playwright/test";

// Agent 链路前端收尾用例（与 public-audit-remediation 同一 route 拦截手法）：
// 1) 事件依据卡：影子模式已退役，卡片按「融合主线审计」口径渲染融合改写行；
// 2) 节点抽屉：?module=workflow 点击「政治分析」节点，四块结构抽屉出现。
// 解剖画布（2026-10-02）新增纯前端规划节点「统一记忆舱」，画布渲染 18+1。
// 两条用例均不改 18 节点后端契约，只读真实后端 + 定点拦截。

async function open(page: Page, module: string) {
  await page.goto(`/?module=${module}`);
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
}

test("event evidence card renders fusion-mainline wording with the crude rewrite row", async ({ page }) => {
  await page.route("**/api/v1/prediction/event-factors**", (route) => route.fulfill({ json: {
    schema_version: "prediction-event-factors.v1",
    business_date: "2026-10-01",
    chain_status: "ok",
    input_sha256: "c".repeat(64),
    selected_count: 2,
    fusion_rows: [{
      target: "crude",
      horizon_days: 7,
      baseline_direction: "neutral",
      event_factor_direction: "up",
      event_factor_confidence: 0.62,
      fusion_rule: "R2",
      event_adjusted_direction: "up",
      switch_reason: "政治供应事件先验切换",
      supporting_event_ids: ["ev-pol-1"],
      outcome_baseline: null,
      outcome_adjusted: null,
    }],
    political: [],
    analog: [],
    event_titles: {},
  } }));
  // 事件依据卡的取数入口在概览模块（moduleLiveDependencies.overview），卡片本体
  // 渲染在 研判报告 → 预测账本 → 历史正式批次；live 数据为页内全局缓存，需应用内跳转。
  const factorsLoaded = page.waitForResponse("**/api/v1/prediction/event-factors**");
  await open(page, "overview");
  expect((await factorsLoaded).status()).toBe(200);
  // A received response is not yet the committed overview cache. Navigating
  // during its remaining fan-out invalidates that module's UI commit; reports
  // does not fetch event factors itself. Wait for the post-response module
  // completion before testing the shared-cache consumer.
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await page.getByText("预测账本与到期复盘", { exact: true }).click();
  await page.getByRole("tab", { name: "历史正式批次" }).click();

  const card = page.getByTestId("event-evidence-card");
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card).toContainText("事件依据卡（预测定案）");
  await expect(card).toContainText("事件改写 1 格");
  await expect(card).toContainText("CRUDE");
  await expect(card).toContainText("7天 纯价格 震荡 → 定案 偏强");
  await expect(card).toContainText("事件改写");
  // ADR-9 单主线口径：卡片内不得出现 影子/融合/双轨 旧措辞。
  await expect(card).not.toContainText("影子");
  await expect(card).not.toContainText("融合");
  await expect(card).not.toContainText("双轨");
  await page.unrouteAll({ behavior: "wait" });
});

test("shadow-eval drawer lists the lesson registry with provenance", async ({ page }) => {
  // 审计 A3 读侧：复盘校准抽屉必须能看到教训内容与来源标记。
  await page.route("**/api/v1/agent-lessons**", (route) => route.fulfill({ json: {
    schema_version: "agent-lessons-registry.v1",
    total: 2,
    active: 2,
    lessons: [
      {
        lesson_id: "sim2025-auto-2026-04-07-1",
        agent: "calibration_memory_agent",
        lesson: "当事件因子置信度不足时维持纯价格基准（R4 场景占优）。",
        category: "calibration",
        evidence_run_ids: ["operation-sim-2025"],
        valid_from: "2026-10-03T00:00:00+00:00",
        valid_until: null,
        status: "active",
        revoked_at: null,
        revoked_by: null,
        metadata: { source: "operation_sim_import" }
      },
      {
        lesson_id: "distill-2026-10-05-1",
        agent: "calibration_memory_agent",
        lesson: "周蒸馏样本量不足时保持空库（clean-skip）。",
        category: "process",
        evidence_run_ids: ["weekly-distill"],
        valid_from: "2026-10-05T01:05:00+00:00",
        valid_until: null,
        status: "active",
        revoked_at: null,
        revoked_by: null,
        metadata: {}
      }
    ]
  } }));
  await open(page, "workflow");
  const nodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  await expect(nodes).toHaveCount(19, { timeout: 30_000 });
  await nodes.filter({ hasText: "复盘校准" }).click();

  const drawer = page.locator("[data-testid='pipeline-node-drawer']");
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  const list = drawer.locator("[data-testid='agent-lessons-list']");
  await expect(list).toBeVisible({ timeout: 15_000 });
  await expect(list).toContainText("共 2 条 · 活跃 2 条");
  await expect(list).toContainText("模拟导入");
  await expect(list).toContainText("蒸馏");
  await page.unrouteAll({ behavior: "wait" });
});

test("workflow node drawer shows the political-analysis four-block structure", async ({ page }) => {
  await page.route("**/api/v1/pipeline/nodes/political_analysis**", (route) => route.fulfill({ json: {
    schema_version: "pipeline-node-detail.v1",
    business_date: "2026-10-01",
    node_id: "political_analysis",
    kind: "agent",
    name: "政局解读",
    status_block: {
      status: "ok",
      status_detail: "调阅 18 · 规格覆盖 2 · 高决 5",
      timestamp: "2026-10-01T08:10:00+08:00",
      metrics: {},
      sources: ["political_analysis"],
    },
    input_summary: { events: 3, evidence_documents: 6 },
    output_summary: { question: "供应侧政治事件对上游成本的方向含义" },
    evidence_entries: [{ kind: "state_source", id: "political_analysis", note: "completed" }],
  } }));
  await open(page, "workflow");
  const nodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  // 真实后端 18 节点 + 前端规划节点「统一记忆舱」= 19。
  await expect(nodes).toHaveCount(19, { timeout: 30_000 });
  await nodes.filter({ hasText: "政局解读" }).click();

  const drawer = page.locator("[data-testid='pipeline-node-drawer']");
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".pipeline-drawer-title")).toContainText("政局解读");
  for (const section of ["节点职责", "今日状态与时间戳", "输入摘要", "输出摘要", "证据入口"]) {
    await expect(drawer.locator(`.pipeline-drawer-section[data-section='${section}']`)).toBeVisible();
  }
  await expect(drawer).toContainText("调阅 18 · 规格覆盖 2 · 高决 5");
  await expect(drawer).toContainText("证据文档数");
  await expect(drawer).toContainText("2026-10-01");
  await page.unrouteAll({ behavior: "wait" });
});

test("anatomy canvas reads graph budgets and edges, stays read-only, and refreshes lazy details", async ({ page }) => {
  let failGraph = false;
  let detailRequests = 0;
  let planningRequests = 0;
  await page.route("**/api/v1/pipeline/graph**", async (route) => {
    if (failGraph) return route.fulfill({ status: 503, json: { detail: "test graph unavailable" } });
    const response = await route.fetch();
    const graph = await response.json();
    graph.memory = { ...graph.memory, recall_enabled: false };
    const calls: Record<string, number> = { political_analysis: 12, historical_analog: 6, product_synthesis: 7, skeptic_review: 6 };
    graph.nodes = graph.nodes.map((node: { id: string }) => calls[node.id] === undefined ? node : {
      ...node, status: "degraded", status_detail: "stale：快照过期，保留原始状态说明", metrics: { calls: calls[node.id], fallback: 1, rejected: 0 }
    });
    graph.edges = graph.edges.filter((edge: { kind: string }) => edge.kind !== "dashed");
    graph.edges.push({ from: "event_summary", to: "assistant", kind: "dashed" });
    return route.fulfill({ json: graph });
  });
  await page.route("**/api/v1/pipeline/nodes/political_analysis**", (route) => {
    detailRequests++;
    return route.fulfill({ json: {
      node_id: "political_analysis", kind: "agent", name: "政局解读",
      status_block: { status: "degraded", status_detail: `详情版本 ${detailRequests}`, timestamp: "2026-10-03T08:10:00+08:00", sources: [] },
      input_summary: {}, output_summary: {}, evidence_entries: []
    } });
  });
  page.on("request", (request) => { if (request.url().includes("/pipeline/nodes/unified_memory")) planningRequests++; });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await open(page, "workflow");
  const nodes = page.locator("[data-testid='agent-topology'] .react-flow__node");
  await expect(nodes).toHaveCount(19);
  await expect(page.getByTestId("pipeline-chain-budget")).toContainText("四阶段调用 31 · 全链已用 —/60");
  await expect(page.locator(".pipeline-lane-budget")).toContainText("四阶段 31");
  await expect(page.locator(".is-pipeline-interaction")).toHaveCount(1);
  await expect(page.locator(".react-flow__node.draggable")).toHaveCount(0);
  const political = nodes.filter({ hasText: "政局解读" });
  await expect(political).toContainText("调用 12/16 · 回退 1");
  await expect(political).toContainText("stale：快照过期");
  await page.screenshot({ path: "agent-context/anatomy-canvas/canvas-1920.png" });
  await political.click();
  const drawer = page.getByTestId("pipeline-node-drawer");
  await expect(drawer).toContainText("详情版本 1");
  await expect(drawer.locator(".pipeline-drawer-budget")).toContainText("今日链累计—/60");
  await expect(drawer).toContainText("schema 修复重试同样计费");
  await page.screenshot({ path: "agent-context/anatomy-canvas/budget-drawer-1920.png" });
  await page.getByRole("button", { name: /刷新状态/ }).click();
  await expect(drawer).toContainText("详情版本 2");
  await page.locator(".ant-drawer-open .pipeline-node-drawer .ant-drawer-footer button").click();
  await nodes.filter({ hasText: "统一记忆舱" }).click();
  await expect(drawer).toContainText("未启用时保留规划态");
  expect(planningRequests).toBe(0);
  await page.locator(".ant-drawer-open .pipeline-node-drawer .ant-drawer-footer button").click();
  failGraph = true;
  await page.getByRole("button", { name: /刷新状态/ }).click();
  await expect(page.locator(".pipeline-degraded-banner")).toContainText("当前显示上次读取结果");
  await expect(page.locator(".pipeline-degraded-banner")).toContainText("上次数据生成时间");
  await expect(nodes).toHaveCount(19);
  await expect(nodes.locator('[data-status="unknown"]')).toHaveCount(0);
  await expect(nodes.locator('[data-status="planning"]')).toHaveCount(1);
  await expect(page.getByTestId("pipeline-chain-budget")).toContainText("四阶段调用 31");
  await expect(political).toContainText("调用 12/16 · 回退 1");
  await political.click();
  await expect(drawer.locator(".pipeline-drawer-budget")).toContainText("12 / 16");
  await page.locator(".ant-drawer-open .pipeline-node-drawer .ant-drawer-footer button").click();
  failGraph = false;
  await page.getByRole("button", { name: /刷新状态/ }).click();
  await expect(page.locator(".pipeline-degraded-banner")).toHaveCount(0);
  await expect(page.getByTestId("pipeline-chain-budget")).toContainText("四阶段调用 31 · 全链已用 —/60");
  await page.unrouteAll({ behavior: "wait" });
});


test("technical lenses explain retrieval admission, provenance and real graph status", async ({ page }) => {
  let planningRequests = 0;
  page.on("request", request => { if (request.url().includes("/pipeline/nodes/unified_memory")) planningRequests++; });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await open(page, "workflow");
  await expect(page.locator(".react-flow__node")).toHaveCount(19);
  await page.getByRole("button", { name: "RAG 检索增强", exact: true }).first().click();
  const panel = page.getByTestId("pipeline-technical-view");
  await expect(panel).toContainText("召回参与定案");
  await expect(panel).toContainText("品种、期限绑定");
  await expect(panel).not.toContainText("效果尚未验收");
  await expect(panel).toContainText("≥2");
  await expect(panel).toContainText("≥3");
  await expect(page.locator(".pipeline-flow-node.is-lens-active")).toHaveCount(3);
  await panel.getByRole("button", { name: "历史经验", exact: true }).click();
  await expect(page.getByTestId("pipeline-node-drawer")).toBeVisible();
  await page.locator(".ant-drawer-open .pipeline-node-drawer .ant-drawer-footer button").click();
  await page.getByRole("button", { name: "记忆模块", exact: true }).first().click();
  await expect(panel).toContainText("教训来源与更新");
  await expect(panel).not.toContainText("−1.27pp");
  await expect(panel).toContainText("7,209");
  await page.locator(".ant-drawer-open .pipeline-node-drawer .ant-drawer-footer button").click();
  await page.getByRole("button", { name: "验证证据与边界 ↗" }).click();
  await expect(panel).toContainText("来源与时刻");
  await expect(panel).toContainText("OOS 门禁");
  expect(planningRequests).toBe(0);
});
