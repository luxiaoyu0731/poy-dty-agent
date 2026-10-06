import { expect, test } from "@playwright/test";

test.describe("industrial intelligence module (eighth module)", () => {
  test("legacy seven modules keep their routes and the intelligence module appends after them", async ({
    page,
  }) => {
    await page.goto("/?module=overview");
    await expect(page.locator("#main-content")).toBeVisible();
    const navLabels = await page
      .locator(".delivery-nav a")
      .evaluateAll((links) => links.map((link) => link.getAttribute("aria-label")));
    expect(navLabels).toEqual([
      "总览看板",
      "行情与原料链",
      "证据图谱",
      "Agent 系统",
      "AI 研判助手",
      "研判报告",
      "工业情报中心",
    ]);
  });

  test("intelligence module renders its shell and never becomes the default landing module", async ({
    page,
  }) => {
    await page.goto("/?module=intelligence");
    await expect(page.locator('#main-content [aria-label="工业情报中心"]')).toBeVisible();
    // Radar is the single browsing entry; the report module owns daily reports.
    await expect(page.getByRole("tab", { name: /每日摘要/ })).toHaveCount(0);
    await expect(page.getByRole("tab", { name: /全球雷达/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tab", { name: /全球态势地图/ })).toBeVisible();
    await expect(page.getByRole("tab", { name: /运行与来源/ })).toBeVisible();

    // Legacy module route still resolves to the same module content.
    await page.goto("/?module=market");
    await expect(page.locator("#main-content")).toBeVisible();
  });
});


test("radar detail expands a complete attributed excerpt", async ({ page }) => {
  const fullText = "原始报道段落。".repeat(60) + "完整结尾。";
  const event = { event_id: "event-test", event_revision_id: "revision-test", revision_no: 1,
    title: "原油管道报道", category: "shipping_ports", product_ids: ["crude"],
    last_seen_at: "2026-09-14T10:00:00Z", as_of_time: "2026-09-14T10:10:00Z",
    relevance_score: 75, confidence: 0.85, evidence_count: 1, revision_count: 2 };
  let historyCalls = 0;
  await page.route("**/api/v1/intelligence/**", async route => {
    const url = new URL(route.request().url());
    const envelope = (items: unknown[]) => ({ items, has_more: false, next_cursor: null });
    let body: unknown = envelope([event]);
    if (url.pathname.endsWith("/revisions")) { historyCalls += 1; body = envelope([{ ...event, facts: [], gaps: [] }]); }
    if (url.pathname.endsWith("/evidence")) body = envelope([{ evidence_link_id: "evidence-test",
      evidence_role: "fact", citation_label: "CNBC", source_tier: "B", canonical_url: "https://www.cnbc.com/test" }]);
    else if (url.pathname.endsWith("/event-test")) body = { ...event, facts: [{ claim_id: "claim-test",
      text: fullText, evidence_link_ids: ["evidence-test"] }], inferences: [], horizon_impact: [],
      gaps: [], counterevidence: [], watch_items: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?module=intelligence&intelligenceView=radar");
  await page.getByRole("button", { name: "原油管道报道" }).click();
  await expect(page.getByText(fullText, { exact: true })).not.toBeVisible();
  await page.locator("summary").filter({ hasText: "展开完整摘录" }).click();
  await expect(page.getByText(fullText, { exact: true })).toBeVisible();
  await expect(page.getByText("CNBC", { exact: true })).toBeVisible();
  expect(historyCalls).toBe(0);
  await page.getByRole("button", { name: "展开修订", exact: true }).click();
  await expect.poll(() => historyCalls).toBe(1);
  await expect(page.getByText("第 1 版", { exact: true })).toBeVisible();
});
