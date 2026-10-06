import { expect, test } from "@playwright/test";

test("AI direction counter-review remains silent in the customer workbench", async ({ page }) => {
  await page.goto("/");
  const workbench = page.locator(".delivery-workbench");
  await expect(workbench).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("正在读取交付状态", { exact: true })).not.toBeVisible({ timeout: 60_000 });

  const visibleText = (await workbench.innerText()).toLowerCase();
  for (const marker of [
    "strategy_c",
    "direction_review_audit",
    "counter_review",
    "provider_calls",
    "prompt_tokens",
    "completion_tokens",
    "feature_flag",
    "deepseek",
  ]) {
    expect(visibleText, `customer-visible text must not expose ${marker}`).not.toContain(marker);
  }
});
