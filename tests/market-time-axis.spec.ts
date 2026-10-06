import { expect, test } from '@playwright/test';
import { marketDay, marketTimeline, marketSeriesLabel, MARKET_DAY_MS } from '../src/utils/marketTimeline';

test('calendar gaps remain null and keep their real duration across DST', () => {
 const axis=marketTimeline([{date:'2026-03-07',value:10},{date:'2026-03-08',value:11},{date:'2026-03-16',value:12}], 'all')!;
 expect(axis.rows).toHaveLength(10);
 expect(axis.rows.filter(x=>x.value!=null)).toHaveLength(3);
 expect(axis.rows.filter(x=>x.value==null)).toHaveLength(7);
 expect(axis.rows[2]).toEqual({date:'2026-03-09',timestamp:marketDay('2026-03-09'),value:null});
 expect(axis.domain[1]-axis.domain[0]).toBe(9*MARKET_DAY_MS);
});

test('short history keeps the selected 90-day domain without inventing earlier prices',()=>{
 const data=[{date:'2026-09-01',value:100},{date:'2026-09-19',value:101}];
 const axis=marketTimeline(data,'90')!;
 expect(axis.domain).toEqual([marketDay('2026-06-21'),marketDay('2026-09-19')]);
 expect(axis.rows.filter(x=>x.value!=null)).toHaveLength(2);
 expect(axis.rows[0].value).toBeNull();
 expect(marketTimeline(data,'all')!.domain[0]).toBe(marketDay('2026-09-01'));
});

test('singleton and invalid observations do not create zero prices or a zero-width axis',()=>{
 const axis=marketTimeline([{date:'2026-09-10',value:5},{date:'invalid',value:3}], 'all')!;
 expect(axis.rows).toHaveLength(1);
 expect(axis.domain[0]).toBeLessThan(axis.domain[1]);
 expect(marketTimeline([], '30')).toBeNull();
});

test('price basis labels distinguish futures, spot and assessment',()=>{
 expect(marketSeriesLabel({series_id:'pta.czce.main_continuous.settlement.cny_mt'})).toBe('期货结算价');
 expect(marketSeriesLabel({series_id:'crude.brent.eia.spot.usd_bbl'})).toBe('现货报价');
 expect(marketSeriesLabel({series_id:'meg.sunsirs.china.spot_assessment.cny_mt'})).toBe('现货评估价');
 expect(marketSeriesLabel()).toBe('历史报价');
});

test('market chart uses proportional date spacing, honest short coverage and accurate futures label',async({page})=>{
 await page.route('**/api/v1/workbench/snapshot',r=>r.fulfill({status:404,json:{detail:'no snapshot'}}));
 await page.route('**/api/v1/prices/latest**',r=>r.fulfill({json:{items:[],status_counts:{}}}));
 await page.route('**/api/v1/workbench/market-chain**',async r=>{
  const response=await r.fetch();const body=await response.json();
  const item=body.products.find((x:any)=>x.key==='PTA');
  item.latest_display_price=null;item.intraday_observation=null;
  item.price_series=['2026-09-01','2026-09-02','2026-09-10'].map((date,i)=>({
   date,value:7100+i*10,unit:'CNY/mt',sample_count:1,source_id:'czce_pta_px',
   comparison_basis:{product:'PTA',series_id:'pta.czce.main_continuous.settlement.cny_mt',source_basis:'czce_pta_px',unit:'CNY/mt'},
  }));
  await r.fulfill({json:body});
 });
 await page.goto('/?module=market');
 const panel=page.locator('.trend-panel');
 await panel.locator('label.ant-segmented-item').filter({hasText:/^PTA$/}).click({timeout:45000});
 await panel.locator('label.ant-segmented-item').filter({hasText:/^全部$/}).click();
 const chart=page.getByTestId('market-trend-chart');
 await expect(chart).toHaveAttribute('data-point-count','3');
 await expect(chart).toHaveAttribute('data-last-date','2026-09-10');
 // v34：数据点不渲染圆点（dot=false）；比例间距不再以 cx 断言。
 await expect(panel).toContainText('曲线 · 期货结算价');
 await expect(panel).not.toContainText('历史现货曲线');
 await panel.locator('label.ant-segmented-item').filter({hasText:/^末90日数据$/}).click();
 await expect(chart).toHaveAttribute('data-axis-start','2026-06-12');
 await expect(chart).toHaveAttribute('data-axis-end','2026-09-10');
 await expect(panel).toContainText('历史不足90日 · 3个观测日');
 await expect(chart).toHaveAttribute('data-point-count','3');
 await expect(panel).toContainText('郑商所 · 2026-09-01 至 2026-09-10');
 await expect(panel).toContainText('休市及未发布日不画点'); // v34 文案
});
