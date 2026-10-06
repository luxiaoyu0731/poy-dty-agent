import { expect, test } from "@playwright/test";
import { eventEvidenceGaps } from "../src/features/industrial-intelligence/eventEvidence";

const event = { event_id: "event-gaps", event_revision_id: "revision-gaps", revision_no: 1,
  title: "原油管道事件", category: "shipping_ports", product_ids: ["crude"],
  last_seen_at: "2026-09-26T10:00:00Z", as_of_time: "2026-09-26T10:10:00Z",
  relevance_score: 75, confidence: .85, evidence_count: 1, revision_count: 1 };
const baseDetail = {...event, facts: [], inferences: [], horizon_impact: [], gaps: [], counterevidence: [],
  watch_items: [{watch_id: "w1", observable_condition: "应该删除的观察模块内容", horizon: "D7", product_ids: ["crude"]}]};

test("horizon gaps are deduplicated without losing product and horizon scope", () => {
  const impact = {product_id: "crude" as const, horizon: "D1" as const, gaps: ["幅度未证实", " "], direction: "unclear" as const, confidence: 0, basis_claim_ids: []};
  const detail = {gaps: [{code: "one", scope: "event", message_safe: "幅度未证实"}], horizon_impact: [impact, impact, {...impact, product_id: "pta" as const, horizon: "D7" as const}]};
  const frozen = JSON.stringify(detail);
  expect(eventEvidenceGaps(detail)).toEqual([{message: "幅度未证实", contexts: ["原油 · 1 天", "PTA · 7 天"]}]);
  expect(JSON.stringify(detail)).toBe(frozen);
});

for (const scenario of ["empty", "horizon", "counter", "headline"] as const) {
  test(`event detail evidence state: ${scenario}`, async ({page}) => {
    const detail: Record<string, unknown> = {...baseDetail};
    if (scenario === "horizon") detail.horizon_impact = [{product_id: "crude", horizon: "D7", direction: "unclear", confidence: .4, basis_claim_ids: [], gaps: ["中期恢复节奏未知"]}];
    if (scenario === "counter") detail.counterevidence = [{claim_id: "c1", text: "原始来源已发布管道恢复公告", evidence_link_ids: ["ce1"]}];
    if (scenario === "headline") detail.gaps = [{code: "headline_only", scope: "event", message_safe: "缺少已校验正文或摘要"}];
    await page.route("**/api/v1/intelligence/**", route => {
      const path = new URL(route.request().url()).pathname;
      const items = path.endsWith("/evidence") ? [{evidence_link_id: "ce1", evidence_role: "counterevidence", canonical_url: "https://example.com/restoration", source_tier: "A", citation_label: "恢复公告"}] : [event];
      return route.fulfill({json: path.endsWith("/event-gaps") ? detail : {items, has_more: false, next_cursor: null}});
    });
    await page.goto("/?module=intelligence&intelligenceView=radar");
    await page.getByRole("button", {name: new RegExp(event.title)}).click();
    await expect(page.locator('[aria-label="事件反证"]')).toBeVisible();
    await expect(page.getByText("接下来观察什么", {exact: true})).toHaveCount(0);
    await expect(page.getByText("应该删除的观察模块内容", {exact: true})).toHaveCount(0);
    if (scenario === "counter") {
      await expect(page.locator('[aria-label="事件反证"]')).toContainText("原始来源已发布管道恢复公告");
      await expect(page.getByRole("link", {name: "[反证来源 1]", exact: true})).toHaveAttribute("href", "https://example.com/restoration");
    } else await expect(page.locator('[aria-label="事件反证"]')).toContainText("不代表不存在反证");
    const gaps = page.locator('[aria-label="事件缺口"]');
    if (scenario === "horizon") {await expect(gaps).toContainText("中期恢复节奏未知"); await expect(gaps).toContainText("原油 · 7 天");}
    else if (scenario === "headline") await expect(gaps).toContainText("缺少已校验正文或摘要");
    else await expect(gaps).toContainText("不代表证据已完整");
  });
}
