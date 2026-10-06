import { expect, test, type Page } from "@playwright/test";

const ids = ["collect", "clean", "index", "event_summary", "event_overview", "factor_score", "event_signal", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion", "seven_product", "shadow_eval", "counter_scan", "daily_interpretation", "report_assembly", "assistant"];
const names = ["采集", "清洗去重门禁", "证据索引", "事件摘要", "事件总览", "因子打分", "当日事件精选", "政局解读", "历史经验", "品种研判", "交叉质证", "预测定案", "七产品预测", "复盘校准", "反证扫描", "日报解读", "研报组装", "研判助手"];
const agentIds = new Set(["event_summary", "event_overview", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "counter_scan", "daily_interpretation", "assistant"]);
const stageCounts: Record<string, [number, number, number]> = { political_analysis: [12, 16, 1], historical_analog: [6, 8, 0], product_synthesis: [7, 7, 0], skeptic_review: [7, 7, 0] };
const rawCollect = "可达源 error+timeout 143/850（source_runs 34、source_runs 34、source_runs 34、source_runs 34）；automation=degraded";

async function openCanvas(page: Page, unavailable = false, chainBudget?: Record<string, unknown>, memory?: Record<string, unknown>) {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.route("**/api/v1/pipeline/graph**", (route) => route.fulfill(unavailable ? { status: 503, json: { detail: "graph unavailable" } } : { json: {
    schema_version: "pipeline-graph.v1", business_date: "2026-10-03", generated_at: "2026-10-03T08:10:00+08:00",
    ...(chainBudget ? {chain_budget: chainBudget} : {}),
    ...(memory ? {memory} : {}),
    nodes: ids.map((id, i) => ({ id, name: names[i], kind: agentIds.has(id) ? "agent" : "code", status: id === "collect" ? "degraded" : "ok", status_detail: id === "collect" ? rawCollect : id === "seven_product" ? "formal=0 reference=21 unavailable=0；OOS evaluation blocked" : "已完成当日处理", timestamp: "2026-10-03T08:10:00+08:00", edge_group: i < 3 ? "data" : i < 6 ? "judgement" : id === "assistant" ? "assistant" : "prediction", ...(stageCounts[id] ? { metrics: { calls: stageCounts[id][0], call_cap: stageCounts[id][1], fallback: stageCounts[id][2] } } : {}) })),
    edges: [...ids.slice(0, 16).map((from, i) => ({ from, to: ids[i + 1], kind: "flow" })),
      ...["political_analysis", "historical_analog"].map(to => ({from:"shadow_eval",to,kind:"feedback"})),
      ...["event_summary", "skeptic_review", "daily_interpretation"].map(from => ({from,to:"assistant",kind:"dashed"}))]
  } }));
  await page.goto("/?module=workflow");
  await expect(page.getByTestId("agent-topology").locator(".pipeline-flow-node")).toHaveCount(19, { timeout: 30_000 });
}

function card(page: Page, id: string) {
  return page.getByTestId("agent-topology").locator(`.react-flow__node[data-id='${id}'] .pipeline-flow-node`);
}

async function expectCardsFit(page: Page) {
  const overflow = await page.getByTestId("agent-topology").locator(".pipeline-flow-node").evaluateAll((cards) => cards.flatMap((el) => {
    const bounds = el.getBoundingClientRect();
    return Array.from(el.querySelectorAll(".pipeline-node-head, .pipeline-node-status, .pipeline-node-budget-row, .pipeline-relay-steps, .review-node-footer, .pipeline-node-name")).flatMap((part) => {
      const rect = part.getBoundingClientRect();
      const clippedTitle = part.matches(".pipeline-node-name") && part.scrollWidth > part.clientWidth;
      return clippedTitle || rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1 ? [`${el.getAttribute("aria-label")}: ${part.className} (${part.scrollWidth}/${part.clientWidth})`] : [];
    });
  }));
  expect(overflow).toEqual([]);
}

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

const memoryResources = {
  schema_version: "pipeline-memory.v1", recall_enabled: true, capability_source: "AGENT_MEMORY_RECALL_ENABLED",
  voting_enabled: false, voting_source: "AGENT_MEMORY_VOTING_ENABLED",
  index_docs: 8914, index_docs_source: "semantic_index.active_index.document_count",
  lessons_active: 30, lessons_active_source: "agent_lessons:active.validity_window",
  data_as_of: "2026-10-03T08:10:00+08:00", status: "ok", status_evidence: {}
};
for (const [name, status, evidence] of [
  ["enabled resources alone", "unknown", {}],
  ["observed successful recall", "ok", {source: "event-agent-chain-latest.json:memory.recalls", business_date: "2026-10-03", run_id: "chain-test", observed_at: "2026-10-03T08:00:00+08:00", recall_count: 2, fragment_count: 6}],
  ["stale recall receipt", "unknown", {source: "event-agent-chain-latest.json:memory.recalls", business_date: "2026-10-03", run_id: "chain-test", observed_at: "2026-10-02T08:00:00+08:00", recall_count: 2, fragment_count: 6}]
] as const) {
  test(`memory card separates capability and resources from ${name}`, async ({page}) => {
    let detailRequests = 0;
    await page.route("**/api/v1/pipeline/nodes/unified_memory**", route => {detailRequests++; return route.fulfill({status: 404, json: {}});});
    await openCanvas(page, false, undefined, {...memoryResources, status_evidence: evidence});
    const memoryCard = card(page, "unified_memory");
    await expect(memoryCard.locator(".pipeline-node-status-pill")).toHaveAttribute("data-status", status);
    await expect(memoryCard).toContainText("召回已启用");
    await expect(memoryCard).toContainText("8914 文档");
    await expect(memoryCard).toContainText("新增召回不参与当前计票");
    await expect(page.getByLabel("节点运行统计")).toContainText("资源 1");
    await memoryCard.click();
    await expect(page.getByTestId("pipeline-memory-resources")).toContainText("活跃教训30");
    await expect(page.getByTestId("pipeline-node-drawer")).toContainText(status === "ok" ? "本次 2 次召回 · 6 个片段" : "暂无可核验的当日召回记录");
    await expectCardsFit(page);
    expect(detailRequests).toBe(0);
  });
}

test("disabled memory preserves planning state even with a green receipt", async ({page}) => {
  await openCanvas(page, false, undefined, {...memoryResources, recall_enabled: false});
  await expect(card(page, "unified_memory").locator(".pipeline-node-status-pill")).toHaveAttribute("data-status", "planning");
  await expect(card(page, "unified_memory")).toContainText("索引 8914 文档");
});

test("workflow cards distinguish rule decision, issuance and independent stage budgets", async ({ page }, testInfo) => {
  await openCanvas(page);
  await expect(card(page, "event_fusion").locator(".pipeline-relay-steps li").last()).toContainText("按定案规则生成最终方向");
  await expect(card(page, "seven_product").locator(".pipeline-relay-steps li").last()).toContainText("二十一格预测冻结入账");
  await expect(card(page, "seven_product")).toContainText("正式 0 · 观察 21");
  for (const [id, [used, cap, fallback]] of Object.entries(stageCounts)) {
    const node = card(page, id);
    await expect(node.locator(".pipeline-node-budget-row")).toHaveText(`调用 ${used}/${cap} · 回退 ${fallback}`);
    await expect(node.locator(".pipeline-node-status [data-testid='pipeline-budget-badge']")).toHaveCount(0);
    const positions = await node.evaluate((el) => {
      const status = el.querySelector(".pipeline-node-status")!.getBoundingClientRect();
      const budget = el.querySelector(".pipeline-node-budget-row")!.getBoundingClientRect();
      return { bottom: status.bottom, top: budget.top };
    });
    expect(positions.top).toBeGreaterThanOrEqual(positions.bottom - 1);
  }
  await expect(page.getByTestId("pipeline-chain-budget")).toContainText("全链已用 —/60");
  const collect = card(page, "collect").locator(".pipeline-node-detail");
  await expect(collect).toContainText("采集异常 143/850");
  await expect(collect).toContainText("自动采集降级");
  await expect(collect).not.toContainText("内部记录");
  await expect(collect).not.toContainText("source_runs");
  await expect(card(page, "unified_memory").locator(".pipeline-node-status-pill")).toHaveAttribute("data-status", "planning");
  await expect(card(page, "unified_memory")).toContainText("互证才有计票权");

  // Real card layout boxes, independent of React Flow's zoom transform. Truncation
  // inside a result paragraph is allowed; identity, badges and footer must fit.
  await expectCardsFit(page);
  await page.screenshot({ path: testInfo.outputPath("workflow-cards-1920.png"), fullPage: true });
  await page.route("**/api/v1/pipeline/nodes/political_analysis**", (route) => route.fulfill({ json: {
    node_id: "political_analysis", kind: "agent", name: "政局解读", status_block: { status: "ok", status_detail: "存活 12", timestamp: "2026-10-03T08:10:00+08:00", sources: [] }, input_summary: {}, output_summary: {}, evidence_entries: []
  } }));
  await card(page, "political_analysis").click();
  await expect(page.getByTestId("pipeline-node-drawer")).toBeVisible();
  await expectCardsFit(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectCardsFit(page);
});

test("failed graph preserves eighteen unknown cards and a separate honest planning card", async ({ page }) => {
  await openCanvas(page, true);
  await expect(page.getByTestId("agent-topology").locator(".pipeline-node-status-pill[data-status='unknown']")).toHaveCount(18);
  await expect(card(page, "unified_memory").locator(".pipeline-node-status-pill")).toHaveText("规划中");
  await expect(page.getByTestId("agent-topology").locator(".pipeline-node-status-pill[data-status='ok']")).toHaveCount(0);
  await expect(card(page, "political_analysis").locator(".pipeline-node-budget-row")).toHaveCount(0);
  await expect(page.locator(".pipeline-degraded-banner")).toContainText("状态未知");
});

test("workflow routes avoid card interiors and lane headings in both drawer states", async ({page}) => {
  await openCanvas(page);
  const assertRoutesClear = async () => {
    // A horizontal SVG path has zero bounding-box height despite a visible stroke.
    await expect(page.locator('.react-flow__edge-path')).toHaveCount(21);
    const collisions = await page.getByTestId('agent-topology').evaluate(root => {
      const obstacles = Array.from(root.querySelectorAll('.pipeline-flow-node, .pipeline-lane-head')).map(el => ({
        name: el.getAttribute('aria-label') || el.textContent?.slice(0, 28), rect: el.getBoundingClientRect()
      }));
      const found: string[] = [];
      for (const path of root.querySelectorAll<SVGPathElement>('.react-flow__edge-path')) {
        const matrix = path.getScreenCTM();
        if (!matrix) continue;
        const length = path.getTotalLength();
        for (let distance = 0; distance <= length; distance += 2) {
          const point = path.getPointAtLength(distance).matrixTransform(matrix);
          const obstacle = obstacles.find(({rect}) => point.x > rect.left + 1 && point.x < rect.right - 1 && point.y > rect.top + 1 && point.y < rect.bottom - 1);
          if (obstacle) { found.push(`${path.closest('.react-flow__edge')?.getAttribute('data-id')}: ${obstacle.name}`); break; }
        }
      }
      return found;
    });
    expect(collisions).toEqual([]);
  };
  await assertRoutesClear();
  await card(page, 'political_analysis').click();
  await expect(page.getByTestId('pipeline-node-drawer')).toBeVisible();
  await assertRoutesClear();
});

for (const [name, overrides, expected] of [
  ['valid HTTP ledger', {}, '40/43'],
  ['unobserved ledger', {status:'unknown',attempts_used:null}, '—/43'],
  ['different business day', {business_date:'2026-10-02'}, '—/43'],
  ['over cap ledger', {attempts_used:44}, '—/43'],
  ['invalid configuration', {cap:null}, '—/—'],
] as const) {
  test(`workflow total distinguishes ${name} from stage sum`, async ({page}) => {
    await openCanvas(page, false, {attempts_used:40,cap:43,business_date:'2026-10-03',
      basis:'HTTP 尝试级，含 schema 重试',source:'event-agent-chain-latest.json:budget',status:'ok', ...overrides});
    await expect(page.getByTestId('pipeline-chain-budget')).toContainText(`四阶段调用 32 · 全链已用 ${expected}`);
  });
}

for (const voting of [true, undefined] as const) {
  test(`memory voting ${String(voting)} never implies recall success`, async ({page}) => {
    await openCanvas(page, false, undefined, {...memoryResources, voting_enabled: voting});
    const memoryCard = card(page, "unified_memory");
    await expect(memoryCard.locator(".pipeline-node-status-pill")).toHaveAttribute("data-status", "unknown");
    await expect(memoryCard).toContainText(voting ? "召回计票已启用" : "新增计票状态未知");
    await memoryCard.click();
    await expect(page.getByTestId("pipeline-node-drawer")).toContainText("按计票开关进入历史先验");
    await expect(memoryCard).not.toContainText("试运行");
    await expect(memoryCard).not.toContainText("规划：");
    await expect(page.locator(".pipeline-mainline-pill")).toHaveText("统一研判主线 · v2");
    await expect(page.getByTestId("pipeline-node-drawer")).not.toContainText("效果尚未验收");
  });
}
