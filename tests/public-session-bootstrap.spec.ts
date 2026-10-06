import { expect, test } from "@playwright/test";

test("public origin does not perform a loopback session handshake", async ({ page, baseURL }) => {
  const sessionRequests: string[] = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/v1/auth/local-session") sessionRequests.push(request.method());
  });
  await page.route("https://public-workbench.test/**", async route => {
    try {
      const original = new URL(route.request().url());
      const response = await route.fetch({ url: `${baseURL}${original.pathname}${original.search}` });
      await route.fulfill({ response });
    } catch {
      // 页面关闭/二次 fulfill 竞态：在途 handler 的清理噪音不应判定测试失败。
    }
  });
  await page.goto("https://public-workbench.test/?module=overview");
  await expect(page.locator(".overview-layout")).toBeVisible();
  await page.getByRole("link", { name: "研判报告", exact: true }).click();
  await expect(page.getByRole("button", { name: "生成信息日报", exact: true })).toBeVisible();
  // unrouteAll(wait) 会等待在途 handler；被吞掉的双 fulfill 可能永不清空。
  // 改为仅移除该路由后直接断言（会话请求从未发出才是被测契约）。
  await page.unroute("https://public-workbench.test/**").catch(() => undefined);
  expect(sessionRequests).toEqual([]);
});
