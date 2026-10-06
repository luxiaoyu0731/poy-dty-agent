import {test, expect} from '@playwright/test';
test.use({ viewport: {width:1710,height:922} });
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
  await canvas.click({ position: { x: bounds!.width / 2, y: bounds!.height / 2 + 35 } });
  await expect(page.getByRole('button', {name: /当前聚合 3 条/})).toBeVisible();
  await page.getByTestId('intelligence-map').screenshot({ path: 'agent-context/layout-public-release-20260919/map-clusters.png' });
  await page.getByRole('button', {name: '地图测试事件1', exact: true}).click();
  await expect(page.locator('.ant-drawer')).toContainText('暂无来源直接支持的事实');
  expect(errors).toEqual([]);
});
