import { expect, test } from "@playwright/test";

test("production events are unique and graph relation targets stay accessible on mobile", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });

  const eventsResponse = await page.request.get("/api/v1/events");
  expect(eventsResponse.ok()).toBeTruthy();
  const events = await eventsResponse.json() as Array<{ title: string; event_type: string }>;
  const identities = events.map((event) =>
    `${event.title.toLocaleLowerCase("zh-CN").replace(/[\s\p{P}\p{S}]+/gu, "")}|${event.event_type}`
  );
  expect(new Set(identities).size).toBe(identities.length);

  const libraryResponse = await page.request.get("/api/v1/workbench/event-library?limit=300");
  expect(libraryResponse.ok()).toBeTruthy();
  const library = await libraryResponse.json() as { events: Array<{ id: string }> };
  // Distinct source events can legitimately share the same source title and
  // date. The canonical API identity, rather than presentation text, is the
  // uniqueness contract used by pagination and selection.
  const libraryIdentities = library.events.map((event) => event.id);
  expect(new Set(libraryIdentities).size).toBe(libraryIdentities.length);

  await page.getByRole("link", { name: "证据图谱", exact: true }).click();
  // v34：核验台就绪锚点 = 板块渲染 + 重新读取按钮可用。
  const board = page.locator("[data-testid='evidence-verification-board']");
  await expect(board).toBeVisible({ timeout: 30_000 });
  await expect(board.getByRole("button", { name: /重新读取/ })).toBeEnabled({ timeout: 30_000 });
  // 图谱只画真实关系；无材料时具体空态而非伪造节点。
  const graphNodes = board.locator("[data-testid='evb-graph-canvas'] .evb-flow-node");
  if ((await graphNodes.count()) <= 1) {
    await expect(board.getByText(/当前档案没有可成图的真实证据关系/)).toBeVisible({ timeout: 20_000 });
  }
  await expect(page.locator(".react-flow__edge[tabindex='0']")).toHaveCount(0);
});

test("overview does not render duplicate events after customer-facing title normalization", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".overview-event-list article").first()).toBeVisible({ timeout: 30_000 });
  const identities = await page.locator(".overview-event-list article").evaluateAll((items) => items.map((item) => {
    const title = item.querySelector("strong")?.textContent ?? "";
    const time = item.querySelector("time")?.textContent ?? "";
    return `${title.toLocaleLowerCase("zh-CN").replace(/^[^：:]{1,12}[：:]/u, "").replace(/[\s\p{P}\p{S}]+/gu, "")}|${time}`;
  }));
  expect(new Set(identities).size).toBe(identities.length);
});

test("radar drawer separates source facts from derived impact analysis", async ({ page }) => {
  const summary = {
    event_id: "ux-boundary", event_revision_id: "rev-ux-boundary", revision_no: 1,
    status: "open", title: "原油供应变化待核验", overview_text: "原油供应变化的中文概述。",
    category: "energy", region_codes: [], product_ids: ["crude"],
    last_seen_at: "2026-09-14T08:00:00Z", as_of_time: "2026-09-14T08:00:00Z",
    relevance_score: 75, severity_score: 50, urgency_score: 40, confidence: 0.85,
    location_precision: null, evidence_count: 1, gap_count: 0, payload_sha256: "0".repeat(64),
  };
  await page.route("**/api/v1/intelligence/events?**", (route) => route.fulfill({ json: {
    schema_version: "1", snapshot_at: "2026-09-14T08:00:00Z", snapshot_id: "snap",
    items: [summary], has_more: false, next_cursor: null, applied: {},
  } }));
  await page.route("**/api/v1/intelligence/events/ux-boundary", (route) => route.fulfill({ json: {
    ...summary,
    facts: [], inferences: [], counterevidence: [], supply_chain_paths: [],
    horizon_impact: [], watch_items: [], gaps: [], revision_count: 1,
    revisions_url: "", evidence_url: "", presentation_status: "full", redactions: [],
  } }));
  await page.route("**/api/v1/intelligence/events/ux-boundary/evidence**", (route) => route.fulfill({ json: {
    schema_version: "1", snapshot_at: "2026-09-14T08:00:00Z", snapshot_id: "snap",
    items: [], has_more: false, next_cursor: null, applied: {},
  } }));
  await page.goto("/?module=intelligence&intelligenceView=radar");
  await page.getByRole("button", { name: /原油供应变化/ }).first().click();
  // v34：事件详情为页面内嵌面板（含"中文阅读摘要"区块）。
  const drawer = page.locator(".review-radar-layout");
  await expect(drawer).toContainText("原油供应变化待核验", { timeout: 30_000 });
  await expect(drawer).toContainText("中文阅读摘要");
  await expect(drawer).toContainText("最近收录");
  // 事实区与推断区分离标注。
  await expect(drawer).toContainText("来源报道不能据此变成方向结论");
  await expect(drawer.locator(".ant-descriptions")).toHaveCount(0);
  await expect(drawer).not.toContainText(/traceback|api key|upstream error|重试次数/i);
});

test("graph state and assistant boundary use unambiguous business language", async ({ page }) => {
  await page.goto("/?module=evidence");
  // v34：核验台就绪 = 判断区渲染且技术诊断在折叠详情内（不与业务内容混排）。
  const board = page.locator("[data-testid='evidence-verification-board']");
  await expect(board).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#evb-verdict")).toBeVisible({ timeout: 30_000 });
  await expect(board.locator(".evb-body > .evb-section").filter({ hasText: "证据筛选步骤" })).toHaveCount(0);

  await page.goto("/?module=assistant");
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/不形成正式|不构成.*指令|非正式/).first()).toBeVisible({ timeout: 30_000 });
});
