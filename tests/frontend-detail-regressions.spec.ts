import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";


test("legacy hash and invalid module are normalized without breaking the skip link", async ({ page }) => {
  await page.goto("/?module=removed-module#/原料链路");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });

  await expect.poll(() => new URL(page.url()).searchParams.get("module")).toBe("overview");
  expect(new URL(page.url()).hash).toBe("");
  await expect(page.locator("a.skip-link")).toHaveAttribute("href", "#main-content");

  await page.locator("a.skip-link").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
});


test("pipeline status counts stay explicit and drawer blocks stay labeled", async ({ page }) => {
  await page.goto("/?module=workflow");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });
  // v39 多 Agent 链：计数在管线面板头部（"节点总数 18"）。
  await expect(page.locator(".delivery-content")).toContainText(/节点总数\s*18/, { timeout: 60_000 });

  const drawerProbe = page.locator("[data-testid='agent-topology'] .react-flow__node").filter({ hasText: "反证扫描" });
  await drawerProbe.click();
  const drawer = page.locator("[data-testid='pipeline-node-drawer']");
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  await expect(drawer.locator(".pipeline-drawer-section[data-section='输入摘要']")).toBeVisible();
  await expect(drawer.locator(".pipeline-drawer-section[data-section='输出摘要']")).toBeVisible();
});


test("assistant initial references are described as source categories", async ({ page }) => {
  await page.goto("/?module=assistant");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("[data-testid='assistant-messages']")).toBeVisible({ timeout: 60_000 });
  // v34：引用证据面板在"引用证据"分页签（默认选中）。
  const evidence = page.locator(".assistant-evidence-panel").first();
  await expect(evidence).toContainText(/可供问答检索的来源类别|问答参考材料/);
  const sourceCategory = evidence.locator(".assistant-evidence-group").filter({ hasText: "可供问答检索的来源类别" });
  if (await sourceCategory.count()) {
    await expect(sourceCategory.locator(".assistant-evidence-group-title")).toContainText(/\d+ 类来源/);
    await expect(sourceCategory.locator(".assistant-evidence-group-title")).not.toContainText(/\d+ 条/);
  }
});


test("copy and sanitization contracts are encoded without ambiguous counts", async () => {
  const source = await readFile(new URL("../src/pages/AgentWorkbenchPage.tsx", import.meta.url), "utf8");

  expect(source).toContain("可供问答检索的来源类别");
  expect(source).toContain('unit: referencesAreSourceCategories ? "类来源" : "条"');
  expect(source).toContain('[/\\bAPI\\b/gi, "系统接口"]');
  expect(source).not.toContain('[/API/gi, "系统接口"]');
  // 12 角色旧视图退役后，页面不再渲染固化角色叙述与 12 角色静态图。
  expect(source).not.toContain("workflowAgents");
  expect(source).not.toContain("materialized-only");
  expect(source).toContain("管道状态暂不可读");
});
