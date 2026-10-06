import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 1920, height: 1080 } });

test("relay cards separate contracts from real execution with readable text and bounded content", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph", async route => {
    const response = await route.fetch();
    const graph = await response.json();
    const stage = graph.nodes.find((node: { id: string }) => node.id === "political_analysis");
    stage.status = "ok";
    stage.status_detail = "实际执行记录：" + "长状态必须保留完整详情而不能压住下一段。".repeat(8);
    stage.metrics = { calls: 12, fallback: 1 };
    await route.fulfill({ json: graph });
  });
  await page.goto("/?module=workflow");
  const cards = page.locator(".pipeline-flow-node");
  await expect(cards).toHaveCount(19);
  await expect(page.locator(".pipeline-relay-steps")).toHaveCount(19);
  const node = page.locator('.react-flow__node[data-id="political_analysis"]');
  await expect(node).toContainText("实际执行记录", { timeout: 60_000 });
  await expect(node).toContainText("调用 12/16 · 回退 1");
  await expect(node).toContainText("输入约定");
  await expect(node).toContainText("实际状态");
  await expect(node).toContainText("输出约定");
  await expect(node).toContainText("冻结事件与原文证据");
  const geometry = await cards.evaluateAll(elements => elements.map(element => {
    const footer = element.querySelector("footer")!;
    const steps = element.querySelector(".pipeline-relay-steps")!;
    const paragraphs = Array.from(steps.querySelectorAll("p"));
    return { overlap: paragraphs.slice(0, -1).some((p, i) => p.getBoundingClientRect().bottom > steps.children[i + 1].getBoundingClientRect().top + 1), font: Math.min(...paragraphs.map(p => parseFloat(getComputedStyle(p).fontSize))), footer: footer.getBoundingClientRect().top, last: paragraphs.at(-1)!.getBoundingClientRect().bottom };
  }));
  for (const card of geometry) {
    expect(card.overlap).toBe(false);
    expect(card.font).toBeGreaterThanOrEqual(18);
    expect(card.last).toBeLessThanOrEqual(card.footer + 1);
  }
  const sizes = await cards.evaluateAll(elements => elements.map(element => ({
    mainline: element.classList.contains("is-lane-prediction"),
    height: parseFloat(getComputedStyle(element).height),
  })));
  expect(sizes.filter(card => card.mainline)).toHaveLength(8);
  expect(sizes.filter(card => card.mainline).every(card => card.height === 480)).toBe(true);
  expect(sizes.filter(card => !card.mainline).every(card => card.height === 330)).toBe(true);
  const memory = page.locator('.react-flow__node[data-id="unified_memory"]');
  await expect(memory).toContainText("规划中");
  await expect(memory).toContainText("不参与当前计票");
  await expect(memory).toContainText("互证才有计票权");
});

test("unavailable graph keeps execution unknown despite descriptive contracts", async ({ page }) => {
  await page.route("**/api/v1/pipeline/graph", route => route.fulfill({ status: 503, json: { detail: "unavailable" } }));
  await page.goto("/?module=workflow");
  await expect(page.locator(".pipeline-flow-node")).toHaveCount(19);
  await expect(page.locator('.pipeline-node-status-pill[data-status="unknown"]')).toHaveCount(18);
  await expect(page.locator('.pipeline-node-status-pill[data-status="ok"]')).toHaveCount(0);
  await expect(page.locator('.react-flow__node[data-id="seven_product"]')).toContainText("输出约定");
});
