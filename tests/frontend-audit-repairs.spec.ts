import {test, expect} from '@playwright/test';
test.use({viewport: {width:1710,height:922}});

test('independent radar remains usable while the global workbench fails',async({page})=>{
 await page.route('**/api/v1/delivery/status', r=>r.fulfill({status:503,json:{detail:'isolated failure'}}));
 await page.route('**/api/v1/workbench/snapshot', r=>r.fulfill({status:503,json:{detail:'isolated failure'}}));
 await page.route('**/api/v1/intelligence/events?**', r=>r.fulfill({json:{items:[],has_more:false}}));
 await page.goto('/?module=intelligence');
 await expect(page.getByRole('tab',{name:'全球雷达',exact:true})).toBeVisible({timeout:8000});
 await expect(page.getByLabel('搜索已采集事件')).toBeEnabled();
 await expect(page.locator('.error-surface')).toHaveCount(0);
});

test('recommendation tab has the buttons and is keyboard navigable',async({page})=>{
 await page.goto('/?module=assistant');
 const tab=page.getByRole('tab',{name:'推荐问题',exact:true});
 await tab.click({timeout:45000});
 await expect(page.locator('.assistant-question-panel:visible .question-chips button')).toHaveCount(4);
 await tab.focus();await page.keyboard.press('ArrowRight');
 await expect(page.getByRole('tab',{name:'当前观察',exact:true})).toBeFocused();
 await expect(page.locator('.assistant-question-panel')).not.toBeVisible();
});

test('slow same-origin basemap is not destroyed after 15 seconds',async({page})=>{
 let release: ()=>void=()=>{};
 const gate=new Promise<void>(resolve=>{release=resolve;});
 await page.route('**/geo/ne_110m_admin_0_countries.v5.1.2.geojson',async r=>{
   await gate;await r.continue();
 });
 await page.route('**/api/v1/intelligence/map?**',r=>r.fulfill({json:{type:'FeatureCollection',features:[],applied_filters:{candidate_count:'0'}}}));
 await page.goto('/?module=intelligence&intelligenceView=map');
 await expect(page.locator('.maplibregl-canvas')).toBeVisible({timeout:20000});
 await expect(page.getByText('底图加载较慢，仍在等待资源')).toBeVisible({timeout:22000});
 await expect(page.locator('.maplibregl-canvas')).toBeVisible();
 await expect(page.getByText('地图暂不可用',{exact:true})).not.toBeVisible();
 release();
 await expect(page.getByTestId('intelligence-map')).toHaveAttribute('data-basemap-ready','true',{timeout:20000});
});

test('naphtha trend preserves native USD series and recent points',async({page})=>{
 await page.route('**/api/v1/workbench/snapshot',r=>r.fulfill({status:404,json:{detail:'no snapshot'}}));
 await page.route('**/api/v1/workbench/market-chain**',async r=>{
   const response=await r.fetch();const body=await response.json();
   const item=body.products.find((x:any)=>x.key==='NAPHTHA');
   expect(item).toBeTruthy();
   const today=new Date().toISOString().slice(0,10);
   item.price_series=[{date:today,value:839.26,unit:'USD/mt',sample_count:1,source_id:'tradingeconomics',comparison_basis:{product:'NAPHTHA',series_id:'naphtha.usd',source_basis:'tradingeconomics',unit:'USD/mt'}}];
   await r.fulfill({json:body});
 });
 await page.goto('/?module=market');
 await page.locator('.trend-panel .ant-segmented-item').filter({hasText:/^石脑油$/}).click({timeout:45000});
 await expect(page.locator('.trend-panel')).toContainText('石脑油 价格趋势（美元/吨）',{timeout:45000});
 await expect(page.locator('.trend-panel .recharts-line-curve, .trend-panel .recharts-dot').first()).toBeVisible();
 // The chart can render before the module's sibling reads finish. Drain the
 // fixture callbacks before closing its page; this keeps assertions intact.
 await expect(page.locator('#main-content')).toHaveAttribute('aria-busy','false');
 await page.unrouteAll({behavior:'wait'});
});

test('late retrieval evidence retains its full text, identity, observation time and original link',async({page})=>{
 const text='已核验原料链证据。'.repeat(20)+'这里是完整材料末尾。';
 let finish: ()=>void=()=>{};
 const gate=new Promise<void>(resolve=>{finish=resolve;});
 await page.route('**/api/v1/knowledge/retrieval?**',async r=>{
  await gate;
  await r.fulfill({json:{documents:[{doc_id:'article-proof-20260919',doc_type:'news_article',source_id:'eia_petroleum_api',title:'原油与聚酯链公开资料',summary:text,snippet:text,tier:'A',observed_at:'2026-09-19T10:00:00Z',url:'https://www.eia.gov/petroleum/'}],query:'POY',count:1}});
 });
 await page.goto('/?module=assistant');
 await expect(page.getByRole('tab',{name:'引用证据',exact:true})).toBeVisible({timeout:45000});
 finish();
 const evidence=page.locator('.assistant-evidence-panel button').filter({hasText:'原油与聚酯链公开资料'});
 await evidence.click();
 const drawer=page.locator('.ant-drawer');
 await expect(drawer).toContainText('这里是完整材料末尾。');
 await expect(drawer).toContainText('article-proof-20260919');
 await expect(drawer).toContainText('观察时间：2026-09-19 18:00'); // 上海时区呈现
 await expect(drawer.getByRole('link',{name:'查看原始来源'})).toHaveAttribute('href','https://www.eia.gov/petroleum/');
});

test('map shows 48h source points and the same window in its empty state',async({page})=>{
 let empty=false;
 await page.route('**/api/v1/intelligence/map?**',r=>r.fulfill({json:{
  type:'FeatureCollection',
  features:empty?[]:[{type:'Feature',id:'map-48h-point',geometry:{type:'Point',coordinates:[131.754,32.1616]},properties:{event_id:'map-48h-event',title:'48h 内来源地震事件',category:'weather_disaster',location_precision:'source_point',cluster_count:1}}],
  applied_filters:{window_hours:'48',candidate_count:'16',mapped_count:empty?'0':'1'},
 }}));
 await page.goto('/?module=intelligence&intelligenceView=map');
 await expect(page.getByTestId('intelligence-map')).toHaveAttribute('data-basemap-ready','true',{timeout:30000});
 await expect(page.locator('.review-map-caption')).toContainText('近 48 小时');
 await expect(page.getByRole('button',{name:'48h 内来源地震事件',exact:true})).toBeVisible();
 await expect(page.getByText('当前没有带可信坐标的事件。',{exact:false})).toHaveCount(0);
 empty=true;
 await page.reload();
 await expect(page.getByText('近48小时有 16 条符合时间条件的候选事件',{exact:false})).toBeVisible();
 await expect(page.locator('.review-map-caption')).toContainText('近 48 小时');
});
