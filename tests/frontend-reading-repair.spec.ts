import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 1920, height: 1080 } });

test("chain inspection shows actual code prompt and leaves unprovided configuration unknown", async ({ page }) => {
  await page.goto("/?module=workflow");
  await expect(page.locator(".pipeline-flow-node")).toHaveCount(19);
  await page.locator('.react-flow__node[data-id="political_analysis"]').click();
  const profile = page.getByTestId("agent-implementation-profile");
  await expect(profile).toContainText("当前服务代码配置；不是当次调用快照");
  await profile.getByText("系统提示词 · 当前代码原文", { exact: true }).click();
  await expect(profile).toContainText("利益方分析");
  await expect(profile).toContainText("禁止使用你训练数据里对后续走势的记忆");
  await page.locator(".pipeline-node-drawer").getByRole("button", { name: "关闭", exact: true }).last().click();
  await page.getByRole("button", { name: "Agent 通信", exact: true }).first().click();
  const technical = page.getByTestId("pipeline-technical-view");
  await expect(technical).toContainText("推理结果如何交接");
  await expect(technical.getByRole("button", { name: "政局解读", exact: true })).toBeVisible();
  await expect(technical.getByRole("button", { name: "political_analysis", exact: true })).toHaveCount(0);
});

test("radar keeps missing analysis explicit without rendering empty impact tables", async ({ page }) => {
  const event = { event_id: "reading-event", event_revision_id: "revision", title: "港口暂停装卸", category: "shipping_ports", product_ids: ["crude"], last_seen_at: "2026-10-03T08:00:00Z", confidence: .8, relevance_score: 80, evidence_count: 1 };
  await page.route("**/api/v1/intelligence/events?**", route => route.fulfill({ json: { items: [event], has_more: false } }));
  await page.route("**/api/v1/intelligence/events/reading-event", route => route.fulfill({ json: { ...event, facts: [{ claim_id: "fact", text: "港口公告暂停装卸", evidence_link_ids: [] }], inferences: [], supply_chain_paths: [], horizon_impact: [], counterevidence: [], watch_items: [], gaps: [], revision_count: 1 } }));
  await page.route("**/api/v1/intelligence/events/reading-event/evidence?**", route => route.fulfill({ json: { items: [] } }));
  await page.goto("/?module=intelligence");
  await page.locator(".radar-event-card").first().click();
  const detail = page.locator(".review-radar-detail");
  await expect(detail).toContainText("港口公告暂停装卸");
  await expect(detail).toContainText("传导推断、分期限影响未返回");
  await expect(detail.locator(".ant-descriptions")).toHaveCount(0);
  await expect(detail.getByText("暂无推断。", { exact: true })).toHaveCount(0);
  await detail.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(detail).toContainText("选择左侧事件");
});

test("old frozen report gets separate observation and judgment columns without changing download data", async ({ page }) => {
  const content = "## 七品种传导判断\n\n| 品种 | 当前判断（系统推断） | 直接依据 |\n|---|---|---|\n| PTA | 价格事实：6400至6500元。事件判断：有相关事件，方向尚未明确 | [价格观察·PTA]；[事件1] |";
  const item = { id: "old", kind: "日报", title: "旧信息报告", summary: "原文摘要", generated_at: "2026-10-03T08:00:00Z", sha256: "original-hash" };
  let releaseList: (() => void) | undefined;
  await page.route("**/api/v1/information-reports**", async route => {
    if (!route.request().url().endsWith("/content")) await new Promise<void>(resolve => { releaseList = resolve; });
    await route.fulfill({ json: route.request().url().endsWith("/content") ? { ...item, content } : { items: [item] } });
  });
  await page.goto("/?module=reports");
  await expect(page.locator("[data-testid='information-reports'] .content-loading")).toBeVisible();
  await expect(page.getByText("当前类型尚无信息报告，点击生成即可整理现有资料。", { exact: true })).toHaveCount(0);
  await expect.poll(() => Boolean(releaseList)).toBe(true);
  releaseList!();
  const body = page.getByTestId("information-report-content");
  await expect(body.getByRole("heading", { name: "七品种观察与事件判断", exact: true })).toBeVisible();
  const cells = body.locator("tbody tr").first().locator("td");
  await expect(cells.nth(1)).toHaveText("有相关事件，方向尚未明确");
  await expect(cells.nth(2)).toHaveText("6400至6500元");
  await expect(cells.nth(3)).toHaveText("[事件1]");
  await expect(body).toContainText("保存原文与下载内容保持不变");
  await page.getByText("报告校验值", { exact: true }).click();
  await expect(page.getByText("original-hash", { exact: true })).toBeVisible();
});
