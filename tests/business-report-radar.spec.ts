import {test,expect} from '@playwright/test';
test.use({viewport:{width:1710,height:922}});
const event = {event_id:'e1',event_revision_id:'r1',title:'原油供应与港口物流变化'.repeat(8),category:'energy',product_ids:['crude','pta','poy','dty'],last_seen_at:'2026-09-19T10:00:00Z',confidence:.8,relevance_score:80,evidence_count:1};

test('whole radar card opens details without squeezing long title or metadata',async({page})=>{
 await page.route('**/api/v1/intelligence/events?**',r=>r.fulfill({json:{items:[event,{...event,event_id:'e2',event_revision_id:'r2'}],has_more:false}}));
 await page.route('**/api/v1/intelligence/events/e1',r=>r.fulfill({json:{...event,facts:[{text:'供应公告事实',evidence_link_ids:[]}],inferences:[],horizon_impact:[],counterevidence:[],watch_items:[],gaps:[],revision_count:1}}));
 await page.route('**/api/v1/intelligence/events/e1/evidence?**',r=>r.fulfill({json:{items:[]}}));
 await page.goto('/?module=intelligence');
 const card=page.locator('.radar-event-card').first();
 await expect(card).toBeVisible();
 // Click metadata, which used to do nothing.
 await card.getByText('证据 1',{exact:true}).click();
 await expect(card).toHaveAttribute('aria-pressed','true');
 await expect(page.locator('.review-radar-detail')).toContainText('供应公告事实');
 for(const width of [1710,1280]){
  await page.setViewportSize({width,height:922});
  const boxes=await card.evaluate(el=>{const title=el.querySelector('strong')!.getBoundingClientRect();const meta=el.querySelector('.radar-event-meta')!.getBoundingClientRect();return {nonOverlap:title.bottom<=meta.top,inside:el.scrollWidth<=el.clientWidth+1};});
  expect(boxes).toEqual({nonOverlap:true,inside:true});
 }
 await card.focus();await page.keyboard.press('Enter');
 await expect(page.locator('.review-radar-detail')).toContainText('供应公告事实');
 await page.screenshot({path:'agent-context/interaction-contract-20260919/radar-after.png'});
 await page.getByRole('tab',{name:'运行与来源',exact:true}).click();
 await expect(page.locator('.ant-drawer')).toHaveCount(0);
});

test('source pages keep column widths and page size change works',async({page})=>{
 await page.route('**/api/v1/intelligence/sources?**',r=>r.fulfill({json:{items:Array.from({length:25},(_,i)=>({source_id:`s${i}`,display_name:i<10?'短名称':`较长来源名称${'产业新闻'.repeat(12)}`,tier:'A',source_type:'news',cadence:'daily',operational_status:'active',metadata_drift:false,credential_status:'not_required',quality_status:'ok'}))}}));
 await page.goto('/?module=intelligence&intelligenceView=runs');
 const table=page.locator('.source-catalog-table');
 await expect(table.locator('tbody tr[data-row-key]')).toHaveCount(10);
 const widths=()=>table.locator('th').evaluateAll(es=>es.map(e=>Math.round(e.getBoundingClientRect().width)));
 const before=await widths();
 await table.locator('.ant-pagination-item-2').click();
 expect(await widths()).toEqual(before);
 await table.locator('.ant-pagination-options-size-changer').click();
 await page.getByTitle('20 / page',{exact:true}).click();
 await expect(table.locator('tbody tr[data-row-key]')).toHaveCount(20);
});

test('business report renders headings independently and audit appendices collapsed',async({page})=>{
 const content='# 市场研判\n\n## 核心研判\n\nPTA多空分化，不能直接推定POY上涨。\n\n## 七品种传导判断\n\n| 品种 | 判断 |\n|---|---|\n| PTA | 多空并存 |\n\n## 关键驱动与传导依据\n\n### 供应变化\n事实：企业公告。\n\n## 附录：口径与核验\n\n生成时正式预测资格：0/21。';
 const item={id:'b1',kind:'日报',title:'市场研判',summary:'PTA多空分化',generated_at:'2026-09-19T10:00:00Z',sha256:'hash'};
 await page.route('**/api/v1/information-reports**',r=>r.fulfill({json:r.request().url().endsWith('/content')?{...item,content}:{items:[item]}}));
 await page.goto('/?module=reports');
 const body=page.getByTestId('information-report-content');
 await expect(body.getByRole('heading',{name:'核心研判',exact:true})).toBeVisible();
 await expect(body.getByRole('heading',{name:'供应变化',exact:true})).toBeVisible();
 await expect(body.getByText('事实：企业公告。',{exact:true})).toBeVisible();
 await expect(body.getByText('生成时正式预测资格：0/21。')).not.toBeVisible();
 await body.getByText('附录：口径与核验',{exact:true}).click();
 await expect(body.getByText('生成时正式预测资格：0/21。')).toBeVisible();
 await page.screenshot({path:'agent-context/interaction-contract-20260919/report-after.png'});
});

test('late search pagination cannot replace results for a new query',async({page})=>{
 let releaseOld: (()=>void)|undefined;
 let oldRequested=false;
 await page.route('**/api/v1/intelligence/events?**',r=>r.fulfill({json:{items:[],has_more:false}}));
 await page.route('**/api/v1/intelligence/search?**',async r=>{
  const url=new URL(r.request().url());
  if(url.searchParams.has('cursor')){oldRequested=true;await new Promise<void>(resolve=>{releaseOld=resolve;});}
  const old=url.searchParams.get('q')==='原油';
  await r.fulfill({json:{items:[{ref_type:'event',ref_id:old?'old':'new',title:old?'旧原油结果':'新聚酯结果',category:'energy'}],next_cursor:old&&!url.searchParams.has('cursor')?'next':null}});
 });
 await page.goto('/?module=intelligence');
 const search=page.getByLabel('搜索已采集事件');
 await search.fill('原油');await search.press('Enter');
 await page.getByRole('button',{name:'加载更多搜索结果'}).click();
 await expect.poll(()=>oldRequested).toBe(true);
 await search.fill('聚酯');await search.press('Enter');
 await expect(page.getByRole('button',{name:'新聚酯结果',exact:true})).toBeVisible();
 releaseOld!();
 await expect(page.getByLabel('事件搜索结果')).not.toContainText('旧原油结果');
});
