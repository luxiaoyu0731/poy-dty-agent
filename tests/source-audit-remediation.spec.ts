import { test, expect } from "@playwright/test";

test("current reference history separates bases and survives product switching", async ({ page }) => {
  await page.route("**/api/v1/prices/intraday?**", async (route) => {
    const instrument = new URL(route.request().url()).searchParams.get("instrument") || "POY";
    const base = { instrument, symbol: instrument, source_id: "public_daily", source_url: "https://example.com/quote", unit: "CNY/mt", price_type: "spot_public_valuation", quality: "reference", raw: {}, notes: "", interval_seconds: 900 };
    await route.fulfill({ json: [
      { ...base, observation_id: "current", created_at: "2026-09-10T01:00:00Z", observed_at: "2026-09-09", last: 9080 },
      { ...base, observation_id: "old", created_at: "2026-09-09T01:00:00Z", observed_at: "2026-09-08", last: 9070 },
      { ...base, observation_id: "other", source_id: "other_basis", created_at: "2026-09-08T01:00:00Z", observed_at: "2026-09-07", last: 11111 }
    ] });
  });
  await page.goto("/?module=market");
  await page.getByRole("tab", { name: "近期报价", exact: true }).click();
  const history = page.locator(".public-quote-history");
  await expect(history).toContainText("9,080");
  await expect(history).toContainText("9,070");
  await expect(history).not.toContainText("11,111");
  await page.locator(".trend-panel label.ant-segmented-item").filter({ hasText: /^DTY$/ }).click();
  await page.getByRole("tab", { name: "近期报价", exact: true }).click();
  await expect(history.locator("summary")).toContainText("DTY");
  await expect(history.getByRole("link", { name: "原始来源" })).toHaveCount(2);
  await page.setViewportSize({ width: 760, height: 900 });
  await expect(history).toBeVisible();
});

test("retired summary deep link opens radar without fetching a daily brief", async ({ page }) => {
  const briefRequests: string[] = [];
  page.on("request", request => { if (request.url().includes("/intelligence/brief")) briefRequests.push(request.url()); });
  await page.goto("/?module=intelligence&intelligenceView=summary");
  await expect(page.getByRole("tab", { name: "全球雷达", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "每日摘要", exact: true })).toHaveCount(0);
  expect(briefRequests).toEqual([]);
});
