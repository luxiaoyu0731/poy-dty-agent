import { test, expect } from '@playwright/test';

test('approved redesign renders all modules at desktop and narrow widths', async ({ page }) => {
  for (const width of [1440, 1280, 760]) {
    await page.setViewportSize({ width, height: Math.round(width * 0.625) });
    for (const module of ['overview', 'market', 'workflow', 'assistant', 'reports', 'intelligence']) {
      await page.goto(`/?module=${module}`);
      await expect(page.locator(module === 'intelligence' ? '.intelligence-center' : '.delivery-page')).toBeVisible();
      await expect(page.locator(module === 'intelligence' ? '.intelligence-center' : '.delivery-page')).not.toHaveText('');
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.waitForLoadState('networkidle');
      if (module === 'workflow') {
        // 解剖画布（2026-10-02）：后端 18 节点 + 前端规划节点「统一记忆舱」= 19。
        await expect(page.locator('.pipeline-flow-node')).toHaveCount(19);
        await expect(page.locator('.pipeline-flow-node').first()).toBeVisible();
        await expect.poll(() => page.locator('.agent-flow-canvas').evaluate(root => {
          const frame = root.getBoundingClientRect();
          return [...root.querySelectorAll('.react-flow__node')].every(node => node.getBoundingClientRect().bottom <= frame.bottom + 1);
        })).toBe(true);
      }
      if (module === 'overview') {
        // v34：总览链路简图（ChainMini，含 material-1 雪碧图）已退役，
        // 链路可视化移至行情页 review-material-flow。此处改为总览判断区截图；
        // 资产完整性锚点 = 品牌油滴图实际加载（非空占位）。
        await page.locator('.overview-decision').scrollIntoViewIfNeeded();
        await page.locator('.overview-decision').screenshot({ path: `agent-context/frontend-redesign-20260919/decision-${width}.png` });
        const brandLoaded = await page.locator('.review-symbol-mark img').first().evaluate((img) => img.complete && img.naturalWidth > 0);
        expect(brandLoaded).toBeTruthy();
      }
      await page.screenshot({ path: `agent-context/frontend-redesign-20260919/${module}-${width}.png`, fullPage: true });
    }
  }
});

test('price freshness does not inherit operational warnings', async ({ page }) => {
  await page.route('**/api/v1/workbench/market-chain**', async route => {
    const response = await route.fetch();
    const data = await response.json();
    for (const product of data.products) product.latest_display_freshness = { status: 'fresh' };
    await route.fulfill({ response, json: data });
  });
  await page.goto('/?module=overview');
  await expect(page.getByText('价格时效正常', { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.overview-risk')).not.toContainText('soft_removed');
  // Drain in-flight route.fetch handlers before Playwright disposes responses.
  await page.unrouteAll({ behavior: 'wait' });
});


test('equal-aspect desktop layouts preserve relative panel placement', async ({ page }) => {
  const samples: number[][] = [];
  for (const width of [1440, 1280]) {
    await page.setViewportSize({ width, height: width * 0.625 });
    await page.goto('/?module=market');
    await page.waitForLoadState('networkidle');
    samples.push(await page.locator('.delivery-sidebar,.market-chain-panel,.trend-panel').evaluateAll(elements =>
      elements.flatMap(el => { const r = el.getBoundingClientRect(); return [r.x / innerWidth, r.width / innerWidth]; })));
  }
  samples[0].forEach((value, index) => expect(Math.abs(value - samples[1][index])).toBeLessThan(0.025));
});

test('map loads lazily, covers multiple categories, and opens shared detail', async ({ page }) => {
  let mapCalls = 0;
  await page.route('**/api/v1/intelligence/map?**', route => {
    mapCalls += 1;
    return route.fulfill({ json: { type: 'FeatureCollection', applied_filters: {}, features:
      ['shipping_ports', 'energy', 'weather_disaster'].map((category, index) => ({
        type: 'Feature', id: `map-${index}`, geometry: { type: 'Point', coordinates: [0, 0] },
        properties: { event_id: `map-${index}`, title: `地图测试事件${index}`, category,
          product_ids: ['crude'], relevance_score: 85, location_precision: 'country_area', cluster_count: 1 }
      })) } });
  });
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/intelligence/events/map-*', route => route.fulfill({ json: {
    event_id: 'map-1', event_revision_id: 'revision-map-1', title: '地图测试事件1', category: 'energy',
    facts: [], inferences: [], counterevidence: [], watch_items: [], gaps: [], horizon_impact: [], product_ids: ['crude'],
    confidence: 0.8, relevance_score: 85, evidence_count: 0, revision_count: 1, as_of_time: '2026-09-19T00:00:00Z'
  } }));
  await page.route('**/api/v1/intelligence/events/map-*/evidence?**', route => route.fulfill({json: {items: [], has_more: false}}));
  await page.goto('/?module=intelligence');
  await expect(page.getByRole('tab', { name: /全球雷达/ })).toBeVisible();
  expect(mapCalls).toBe(0);
  await page.getByRole('tab', { name: /全球态势地图/ }).click();
  await expect(page.getByTestId('intelligence-map')).toHaveAttribute('data-basemap-ready', 'true', {timeout: 20000});
  await expect(page.getByRole('button', {name: '地图测试事件1', exact: true})).toBeVisible();
  await expect(page.getByText('国家示意点', {exact:false}).first()).toBeVisible();
  const canvas = page.locator('.maplibregl-canvas');
  const bounds = await canvas.boundingBox();
  expect(bounds).not.toBeNull();
  // At this fixed world view the three co-located fixtures form one visible cluster.
  await canvas.click({ position: { x: bounds!.width / 2, y: 286 } });
  await expect(page.getByRole('button', {name: /当前聚合 3 条/})).toBeVisible();
  await page.getByTestId('intelligence-map').screenshot({ path: 'agent-context/frontend-redesign-20260919/map-clusters.png' });
  await page.getByRole('button', {name: '地图测试事件1', exact: true}).click();
  await expect(page.locator('.ant-drawer')).toContainText('暂无来源直接支持的事实');
  expect(errors).toEqual([]);
});
