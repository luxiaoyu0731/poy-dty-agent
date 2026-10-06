import { expect, test, type Page } from "@playwright/test";

async function routingViolations(page: Page) {
  return page.locator(".pipeline-graph-canvas").evaluate((canvas) => {
    const nodes = [...canvas.querySelectorAll<HTMLElement>(".react-flow__node")]
      .map(node => ({ id: node.dataset.id!, rect: node.getBoundingClientRect() }));
    const titles = [...canvas.querySelectorAll<HTMLElement>(
      ".pipeline-lane-title, .pipeline-lane-keyword, .pipeline-lane-budget"
    )].map(element => ({ text: element.textContent, rect: element.getBoundingClientRect() }));
    const pane = canvas.querySelector(".react-flow")!.getBoundingClientRect();
    const issues = new Set<string>();
    const interactionTracks = new Map<string, string>();
    const inside = (x: number, y: number, rect: DOMRect) =>
      x > rect.left + 1 && x < rect.right - 1 && y > rect.top + 1 && y < rect.bottom - 1;
    for (const edge of canvas.querySelectorAll<SVGGElement>(".react-flow__edge")) {
      const id = edge.dataset.id!;
      const path = edge.querySelector<SVGPathElement>(".react-flow__edge-path")!;
      const matrix = path.getScreenCTM()!;
      const length = path.getTotalLength();
      for (let distance = 0; distance <= length; distance += 3) {
        const point = path.getPointAtLength(distance).matrixTransform(matrix);
        if (edge.classList.contains("is-pipeline-interaction")) {
          const cell = `${Math.floor(point.x)}:${Math.floor(point.y)}`;
          const other = interactionTracks.get(cell);
          if (other && other !== id) issues.add(`${id} overlaps ${other}`);
          interactionTracks.set(cell, id);
        }
        for (const node of nodes) {
          if (!id.includes(node.id) && inside(point.x, point.y, node.rect)) issues.add(`${id} crosses ${node.id}`);
        }
        for (const title of titles) {
          if (inside(point.x, point.y, title.rect)) issues.add(`${id} crosses heading ${title.text}`);
        }
        if (!canvas.closest(".has-node-drawer") && (point.x < pane.left || point.x > pane.right || point.y < pane.top || point.y > pane.bottom)) {
          issues.add(`${id} clipped by viewport`);
        }
      }
    }
    return [...issues];
  });
}

for (const width of [1920, 1440]) {
  test(`pipeline routes avoid unrelated cards and lane headings at ${width}px, including the drawer`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1080 });
    await page.goto("/?module=workflow");
    await expect(page.locator("#main-content")).toHaveAttribute("aria-busy", "false", { timeout: 60_000 });
    await expect(page.locator(".pipeline-flow-node")).toHaveCount(19);
    await expect(page.getByRole("button", { name: "刷新状态", exact: true })).not.toHaveClass(/ant-btn-loading/);
    await expect(page.locator(".is-pipeline-feedback").filter({ has: page.locator("path") })).toHaveCount(2);
    await expect.poll(() => routingViolations(page), { timeout: 15_000 }).toEqual([]);
    const card = page.locator('.react-flow__node[data-id="political_analysis"]');
    const before = await card.boundingBox();
    const zoomBefore = await page.locator(".react-flow__viewport").evaluate(el => new DOMMatrixReadOnly(getComputedStyle(el).transform).a);
    await card.click();
    await expect(page.locator(".workflow-page")).toHaveClass(/has-node-drawer/);
    await expect.poll(async () => {
      const after = await card.boundingBox();
      return Math.abs(after!.width - before!.width);
    }).toBeLessThan(1);
    const zoomAfter = await page.locator(".react-flow__viewport").evaluate(el => new DOMMatrixReadOnly(getComputedStyle(el).transform).a);
    expect(zoomAfter).toBeCloseTo(zoomBefore, 4);
    await expect.poll(() => routingViolations(page), { timeout: 15_000 }).toEqual([]);
  });
}
