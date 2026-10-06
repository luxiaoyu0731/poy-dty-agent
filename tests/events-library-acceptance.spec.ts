import { expect, test } from "@playwright/test";

const eventFixture = (
  id: string,
  title: string,
  productIds: string[],
  options: { evidenceCount?: number; status?: string; confidence?: number; relevance?: number } = {},
) => ({
  event_id: id,
  event_revision_id: `rev-${id}`,
  revision_no: 1,
  status: options.status ?? "open",
  title,
  overview_text: title,
  category: "energy",
  region_codes: [],
  product_ids: productIds,
  last_seen_at: "2026-09-14T08:00:00Z",
  as_of_time: "2026-09-14T08:00:00Z",
  relevance_score: options.relevance ?? 75,
  severity_score: 50,
  urgency_score: 40,
  confidence: options.confidence ?? 0.85,
  location_precision: null,
  evidence_count: options.evidenceCount ?? 1,
  gap_count: 0,
  payload_sha256: "0".repeat(64),
});

test("event library API stays sorted by business time for its consumers", async ({ request }) => {
  const apiResponse = await request.get("/api/v1/workbench/event-library?limit=300&offset=0");
  expect(apiResponse.ok()).toBeTruthy();
  const payload = await apiResponse.json() as { events: Array<{ time: string }>; has_more: boolean };
  const times = payload.events.map((event) => Date.parse(event.time)).filter(Number.isFinite);
  expect(times).toEqual([...times].sort((a, b) => b - a));
});

test("radar search queries the server index and opens the hit detail", async ({ page }) => {
  await page.route("**/api/v1/intelligence/search**", async (route) => {
    const url = new URL(route.request().url());
    const hit = (id: string) => ({
      ref_type: "event",
      ref_id: id,
      title: `霍尔木兹海峡事件 ${id}`,
      category: "shipping_ports",
      detail_url: `/api/v1/intelligence/events/${id}`,
      payload_sha256: "0".repeat(64),
    });
    const cursor = url.searchParams.get("cursor");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1",
        snapshot_at: "2026-09-14T08:00:00Z",
        snapshot_id: "snap",
        items: cursor ? [hit("evt-hormuz-b")] : [hit("evt-hormuz-a")],
        has_more: Boolean(!cursor),
        next_cursor: cursor ? null : "page-2",
        applied: {},
      }),
    });
  });
  await page.route("**/api/v1/intelligence/events/evt-hormuz-a", async (route) => {
    const detail = {
      ...eventFixture("evt-hormuz-a", "霍尔木兹海峡通行受影响", ["crude"]),
      facts: [], inferences: [], counterevidence: [], supply_chain_paths: [],
      horizon_impact: [], watch_items: [], gaps: [], revision_count: 1,
      revisions_url: "", evidence_url: "", presentation_status: "full", redactions: [],
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(detail) });
  });
  await page.goto("/?module=intelligence&intelligenceView=radar");
  const search = page.getByRole("searchbox", { name: "搜索已采集事件" });
  await search.fill("霍尔木兹");
  await search.press("Enter");
  await expect(page.locator("[aria-label='事件搜索结果'] li")).toHaveCount(1, { timeout: 30_000 });
  // Cursor pagination continues the deduped result set.
  await page.getByRole("button", { name: "加载更多搜索结果" }).click();
  await expect(page.locator("[aria-label='事件搜索结果'] li")).toHaveCount(2, { timeout: 30_000 });
  await expect(page.getByRole("button", { name: "加载更多搜索结果" })).toHaveCount(0);
  await page.locator("[aria-label='事件搜索结果'] button").first().click();
  // v34：radar 页签内搜索命中打开右侧内嵌详情面板。
  const detail = page.locator(".review-radar-layout");
  await expect(detail).toContainText("霍尔木兹海峡通行受影响", { timeout: 30_000 });
});
