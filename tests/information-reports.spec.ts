import { expect, test } from "@playwright/test";

test("four information report types generate, preview and download without formal eligibility", async ({ page }) => {
  const items: Array<Record<string, string>> = [];
  let serial = 0;
  await page.route("**/api/v1/information-reports**", async route => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST") {
      const { kind } = route.request().postDataJSON();
      const item = { id: `report-${++serial}`, kind, title: `信息与证据${kind}`, summary: "已收集资料", generated_at: "2026-09-08T01:00:00Z", qualification: "information_only", sha256: "frozen-hash" };
      items.unshift(item);
      return route.fulfill({ json: item });
    }
    if (url.pathname.endsWith("/download")) return route.fulfill({ contentType: "text/markdown", headers: { "content-disposition": 'attachment; filename="information.md"' }, body: "# 信息报告\n正式资格0/21" });
    if (url.pathname.endsWith("/content")) {
      const id = url.pathname.split("/").at(-2);
      return route.fulfill({ json: { ...items.find(item => item.id === id), content: "# 信息报告\n正式资格0/21\n原文：https://example.com/evidence" } });
    }
    return route.fulfill({ json: { items } });
  });
  await page.goto("/?module=reports&reportView=reports");
  for (const [tab, kind] of [["日报", "日报"], ["周报", "周报"], ["复盘报告", "复盘"], ["专题报告", "专题"]]) {
    await page.locator(".report-workspace-switch").getByText(tab, { exact: true }).click();
    await page.getByRole("button", { name: `生成信息${kind}`, exact: true }).click();
    await expect(page.getByTestId("information-report-content")).toContainText("正式资格0/21");
    await page.getByTestId("information-reports").locator("summary").filter({ hasText: "来源链接" }).click();
    await expect(page.getByRole("link", { name: "https://example.com/evidence", exact: true })).toHaveAttribute("href", "https://example.com/evidence");
    await page.getByTestId("information-reports").locator("summary").filter({ hasText: "来源链接" }).click();
    const download = page.waitForEvent("download");
    await page.getByRole("link", { name: "下载信息报告", exact: true }).click();
    expect((await download).suggestedFilename()).toBe("information.md");
  }
  expect(items).toHaveLength(4);
  await page.reload();
  await expect(page.getByTestId("information-report-content")).toContainText("example.com/evidence");
  await page.setViewportSize({ width: 850, height: 768 });
  await expect(page.getByRole("button", { name: "生成信息专题", exact: true })).toBeVisible();
});

test("uncertain generation queries history instead of claiming success", async ({ page }) => {
  let postCount = 0;
  await page.route("**/api/v1/information-reports**", route => {
    if (route.request().method() === "POST") { postCount += 1; return route.fulfill({ status: 503, json: { error: "unavailable" } }); }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto("/?module=reports&reportView=reports");
  await page.getByRole("button", { name: "生成信息日报", exact: true }).click();
  await expect(page.getByTestId("information-reports")).toContainText("尚未确认生成结果");
  expect(postCount).toBe(1);
  await expect(page.getByRole("button", { name: "下载信息报告", exact: true })).toBeDisabled();
});

test("frozen reports display short local times without changing downloads or trace IDs", async ({page}) => {
  const timestamp = "2026-09-20T10:05:13.936321+00:00";
  const batchId = "seven-be101171394915d295ef060e";
  const sourceUrl = `https://example.com/evidence?as_of=${timestamp}`;
  const item = {id: "old-report", kind: "日报", title: "历史信息日报", summary: "已冻结", generated_at: timestamp, sha256: "unchanged-hash"};
  const content = `# 历史信息日报\n\n生成时间：${timestamp}（UTC）\n资料收录窗口：2026-09-19T10:05:13.936321+00:00 至 ${timestamp}（UTC）\n\n## 关键风险、反证与下一步观察\n旧观察条件应隐藏\n\n## 判断如何变化\n旧条件分析应隐藏\n\n## 已发行主预测\n批次：${batchId}；输入截止：${timestamp}。\n\nPOY 9,342.50 元/吨，参考评分 45.0%。\n原文：${sourceUrl}`;
  let writes = 0;
  await page.route("**/api/v1/information-reports**", route => {
    if (route.request().method() !== "GET") writes++;
    if (route.request().url().endsWith("/download")) return route.fulfill({body: content, contentType: "text/markdown"});
    return route.fulfill({json: route.request().url().endsWith("/content") ? {...item, content} : {items: [item]}});
  });
  await page.goto("/?module=reports&reportView=reports");
  const body = page.getByTestId("information-report-content");
  await expect(body).toContainText("生成时间：2026-09-20 18:05（上海）");
  await expect(body).toContainText("资料收录窗口：2026-09-19 18:05 至 2026-09-20 18:05（上海）");
  await expect(body).toContainText("POY 9,342.50 元/吨，参考评分 45.0%");
  await expect(body).not.toContainText("关键风险、反证与下一步观察");
  await expect(body).not.toContainText("判断如何变化");
  await expect(body).not.toContainText("旧观察条件应隐藏");
  await expect(body).not.toContainText("旧条件分析应隐藏");
  await expect(body.getByText(new RegExp(`批次：${batchId}`))).toBeHidden();
  await body.getByText("预测追溯信息", {exact: true}).click();
  await expect(body.getByText(new RegExp(`批次：${batchId}`))).toBeVisible();
  await page.getByTestId("information-reports").getByText("来源链接（1）", {exact: true}).click();
  await expect(page.getByRole("link", {name: sourceUrl, exact: true})).toHaveAttribute("href", sourceUrl);
  const downloadUrl = await page.getByRole("link", {name: "下载信息报告", exact: true}).getAttribute("href");
  const original = await page.evaluate(async url => (await fetch(url!)).text(), downloadUrl);
  expect(original).toBe(content);
  expect(writes).toBe(0);
});

test("historical report is dated and duplicate introductory judgement is omitted only in the reader", async ({ page }) => {
  const item = { id: "historical-duplicate", kind: "日报", title: "历史日报", generated_at: "2020-01-01T01:00:00Z", summary: "摘要", sha256: "frozen" };
  await page.route("**/api/v1/information-reports**", route => route.fulfill({ json: route.request().url().endsWith("/content") ? { ...item, content: "# 历史日报\n\n相同研判文字\n\n## 核心研判\n\n相同研判文字\n\n## 来源\n原文材料" } : { items: [item] } }));
  await page.goto("/?module=reports&reportView=reports");
  await expect(page.getByText("正在阅读历史日报 · 2020-01-01", { exact: true })).toBeVisible();
  await expect(page.getByTestId("information-report-content").getByText("相同研判文字", { exact: true })).toHaveCount(1);
});
