import { expect, test } from "@playwright/test";

test("new current observation refreshes reference history without changing product", async ({ page }) => {
  let revision = 0;
  let historyRequests = 0;
  const date = (n: number) => new Date(Date.now() + n * 60_000).toISOString();
  const dates = [date(1), date(2)];
  const observation = () => ({
    observation_id: `refresh-observation-${revision}`, instrument: "POY", symbol: "POY",
    source_id: "public-test", source_url: "https://example.com/quote", unit: "CNY/mt",
    price_type: "spot_public_valuation", observed_at: dates[revision], last: 9400 + revision,
  });
  await page.route("**/api/v1/prices/latest**", route => route.fulfill({ json: {
    generated_at: dates[revision], policy_note: "isolated regression", status_counts: { valuation: 1 },
    items: [{ instrument: "POY", label: "POY", freshness: "valuation", latest: observation() }],
  } }));
  await page.route("**/api/v1/prices/intraday**", route => {
    historyRequests += 1;
    return route.fulfill({ json: [observation()] });
  });
  await page.goto("/?module=market");
  const history = page.locator(".public-quote-history");
  await expect(history).toContainText("9,400", { timeout: 60_000 });
  // Ensure the initial live observation, rather than only the snapshot, was read.
  await page.getByRole("button", { name: /刷新/ }).first().click();
  await expect.poll(() => historyRequests).toBeGreaterThan(1);
  const before = historyRequests;
  revision = 1;
  await page.getByRole("button", { name: /刷新/ }).first().click();
  await expect(history).toContainText("9,401", { timeout: 30_000 });
  expect(historyRequests).toBeGreaterThan(before);
  await expect(history).not.toContainText("9,400");
});
