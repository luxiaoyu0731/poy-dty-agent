import { expect, test, type Page } from "@playwright/test";

test.afterEach(async ({ page }) => {
  // Slow response fixtures must finish before Playwright disposes the context;
  // otherwise a still-running route.fetch fails during an unrelated teardown.
  await page.unrouteAll({ behavior: "wait" });
});

async function open(page: Page, module: string) {
  await page.goto(`/?module=${module}`);
  await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
}

for (const age of [71, 3]) {
  test(`formal report pipeline distinguishes a ${age}-day-old delivery from a pending file`, async ({ page }) => {
    await page.clock.setFixedTime(new Date("2026-10-03T06:00:00Z"));
    const generatedAt = new Date(Date.parse("2026-10-03T06:00:00Z") - age * 86400000).toISOString();
    await page.route("**/api/v1/delivery/status**", async route => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.client_reports = [{ id: "runtime-daily", title: "POY/DTY 上游原料日报", report_type: "日报",
        status: "ready", audience: "业务负责人", summary: "历史正式报告", generated_at: generatedAt,
        content_status: "ready", download_available: false }];
      await route.fulfill({ json: payload });
    });
    await open(page, "reports");
    await page.getByText("正式研判报告与历史交付", { exact: true }).click();
    const list = page.locator(".report-list");
    await expect(list).toContainText("POY/DTY 上游原料日报");
    if (age > 14) {
      await expect(list).toContainText(`管线停摆自 ${generatedAt.slice(0, 10)}，正式报告待操作员重启`);
      await expect(page.getByTestId("report-source-content")).toContainText("正式报告待操作员重启");
      await expect(list).not.toContainText("待生成");
    } else {
      await expect(list).not.toContainText("管线停摆");
      await expect(list).toContainText("待生成");
    }
  });
}

test("mechanism gaps distinguish unverified materials from no materials", async ({ page }) => {
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => {
    const url = new URL(route.request().url());
    return route.fulfill({ json: {
      schema_version: "business-evidence-view.v1", view: url.searchParams.get("view"), target: url.searchParams.get("target"), horizon_days: 7,
      status: "available_with_gaps", as_of_time: "2026-10-03T00:00:00Z", input_sha256: "a".repeat(64), hypothesis: "机制待核验",
      coverage: [{ mechanism: "supply", label: "供应机制", automatic_scope: "供给事件", status: "no_material", stored_claims: 0, source_claims: 0, usable_episodes: 0 }],
      claims: [], current_support: [], current_counter: [], historical_support: [], historical_counter: [], historical_other: [],
      other_materials: Array.from({ length: 285 }, (_, i) => `pending-${i}`), mixed: [], current_support_episodes: 0, current_counter_episodes: 0,
      market_baseline: {}, gaps: [], source_gaps: {}, total_claims: 285, offset: 0, next_offset: null,
    } });
  });
  await open(page, "evidence");
  await expect(page.locator("#evb-gaps")).toContainText("285 条材料待核验，未通过机制规则不计票", { timeout: 30_000 });
  await expect(page.locator("#evb-gaps")).not.toContainText("暂无材料");
});

test("assistant hides answer evidence groups before the first answer", async ({ page }) => {
  await open(page, "assistant");
  const panel = page.locator(".assistant-evidence-panel");
  await expect(panel).toContainText("提问并获得回答后展示");
  for (const title of ["正式结论采用证据", "反证与冲突", "已排除材料"]) {
    await expect(panel.locator(".assistant-evidence-group-title").filter({ hasText: title })).toHaveCount(0);
  }
  await expect(panel).toContainText(/可供问答检索的来源类别|问答参考材料/);
});

test("slow successful event factors do not produce an endpoint degradation alarm", async ({ page }) => {
  await page.route("**/api/v1/prediction/event-factors**", async route => {
    const response = await route.fetch();
    await new Promise(resolve => setTimeout(resolve, 9500));
    await route.fulfill({ response });
  });
  const loaded = page.waitForResponse("**/api/v1/prediction/event-factors**");
  await open(page, "overview");
  await loaded;
  await expect(page.getByTestId("workbench-degraded-banner")).not.toContainText("事件依据卡", { timeout: 15_000 });
});

test("event factor HTTP failure retries and stays explicitly degraded", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/v1/prediction/event-factors**", route => {
    attempts += 1;
    return route.fulfill({ status: 503, json: { error: "event factors unavailable" } });
  });
  await open(page, "overview");
  await expect.poll(() => attempts, { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId("workbench-degraded-banner")).toContainText("事件依据卡", { timeout: 30_000 });
});

test("graph metrics render stage budgets without claiming a whole-chain ledger", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph**", async route => {
    const response = await route.fetch();
    const graph = await response.json();
    for (const node of graph.nodes) {
      if (node.id === "political_analysis") node.metrics = { calls: 16, fallback: 2, rejected: 0 };
    }
    await route.fulfill({ json: graph });
  });
  await open(page, "workflow");
  await expect(page.locator('.react-flow__node[data-id="political_analysis"]')).toContainText("调用 16/16 · 回退 2");
  await expect(page.getByTestId("pipeline-chain-budget")).toContainText("全链已用 —/60");
});
