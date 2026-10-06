import { expect, test } from "@playwright/test";

test("explicit sentence limit survives the API to UI boundary", async ({ page }) => {
  await page.route("**/api/v1/assistant/chat", route => route.fulfill({ json: {
    answer: "结论：原油经石脑油和PX影响PTA。\n可信边界：仅解释传导机制。",
    status: "degraded", length_constraint_sentences: 3, display_evidence: [], warnings: [],
    answer_sections: {
      conclusion: "原油经石脑油和PX影响PTA。", confidence_boundary: "仅解释传导机制。",
      evidence_points: ["不应自动展开的长篇依据"], counter_evidence: ["不应自动展开的反证"],
      risks: ["不应自动展开的风险"], next_steps: ["不应自动展开的下一步"]
    }
  } }));
  await page.goto("/?module=assistant");
  await page.getByLabel("输入研判问题").fill("请用三句话解释原油如何影响PTA");
  await page.getByRole("button", { name: /发\s*送/ }).click();
  const messages = page.locator(".chat-messages");
  await expect(messages).toContainText("原油经石脑油和PX影响PTA。");
  await expect(messages).toContainText("仅解释传导机制。");
  await expect(messages).not.toContainText("不应自动展开");
});

 test("assistant opens with a question prompt rather than a synthetic conclusion", async ({ page }) => {
  await page.goto("/?module=assistant");
  await expect(page.locator(".chat-messages")).toContainText("选择一个问题");
  await expect(page.locator(".chat-messages")).not.toContainText("结论：");
  await expect(page.getByLabel("开始提问")).toBeVisible();
  await page.getByRole("tab", { name: "推荐问题", exact: true }).click();
  await expect(page.getByLabel("开始提问")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "哪些证据支持当前结论？", exact: true })).toHaveCount(1);
});
