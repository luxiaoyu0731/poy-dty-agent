import {expect, test} from '@playwright/test';

const response = (url: URL) => ({
  schema_version:'business-evidence-view.v1',view:url.searchParams.get('view'),target:url.searchParams.get('target'),horizon_days:Number(url.searchParams.get('horizon_days')),
  status:'available_with_gaps',scope:'product',scope_note:'对象绑定测试材料',as_of_time:'2026-09-27T16:00:00Z',input_sha256:'a'.repeat(64),
  hypothesis:'所选对象的条件性价格机制',market_baseline:{},coverage:[],claims:[],current_support:[],current_counter:[],historical_support:[],historical_counter:[],historical_other:[],other_materials:[],mixed:[],current_support_episodes:0,current_counter_episodes:0,gaps:['无可核验配对'],source_gaps:{},total_claims:0,offset:0,next_offset:null,
});
async function mockEvidence(page: import('@playwright/test').Page) {
  const calls: URL[]=[];
  await page.route('**/api/v1/forecasts/seven-product/evidence?**',route=>{
    const url=new URL(route.request().url()); calls.push(url);
    return route.fulfill({json:response(url)});
  });
  return calls;
}
for (const [module,button,view] of [['overview','查看判断证明','issued'],['market','品种正反证','current'],['workflow','分析证据档案','current']]) {
  test(`${module} exposes shared evidence with explicit scope`,async({page})=>{
    const calls=await mockEvidence(page);
    await page.goto(`/?module=${module}`);
    await page.getByRole('button',{name:button,exact:true}).click();
    await expect(page.getByRole('dialog').getByText('对象绑定测试材料')).toBeVisible();
    expect(calls[0].searchParams.get('view')).toBe(view);
    expect(calls[0].searchParams.get('target')).toBe('poy');
  });
}

test('radar binds event revision instead of all crude material',async({page})=>{
  const calls=await mockEvidence(page);
  const event={event_id:'event-scope',event_revision_id:'revision-scope',revision_no:1,status:'active',title:'原油库存核验事件',category:'energy',product_ids:['crude'],last_seen_at:'2026-09-27T12:00:00Z',as_of_time:'2026-09-27T12:00:00Z',relevance_score:75,confidence:.8,evidence_count:1,revision_count:1};
  await page.route('**/api/v1/intelligence/**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/event-scope')) return route.fulfill({json:{...event,facts:[],inferences:[],horizon_impact:[],gaps:[],counterevidence:[],semantic_review_as_of:'2026-10-03T09:00:00Z',semantic_reviews:[{review_id:'event-current-review',source_target:'crude',target:'crude',relation:'direct',mechanism:'logistics',direction:'up',subject:'甲公司',action:'关闭',quote:'甲公司原油运输管道目前已关闭。',source_url:'https://example.test/notice',source_title:'运输公告',published_at:'2026-10-02T02:00:00Z',reviewed_at:'2026-10-02T03:00:00Z',source_available_at:'2026-10-02T02:00:00Z',model:'test',time_kind:'current_state',time_anchor:'',period_start:null,period_end:null,rationale:'原油运量减少可能形成条件上行压力。',conditions:['需核验受影响运量'],counts_as_evidence:false,assessment:'ai_semantic_review_not_verified_outcome'}]}});
    return route.fulfill({json:{items:path.endsWith('/evidence')?[]:[event],has_more:false,next_cursor:null}});
  });
  await page.goto('/?module=intelligence&intelligenceView=radar');
  await page.getByRole('button',{name:/原油库存核验事件/}).click();
  await expect(page.getByLabel('事件元信息')).toContainText('原油');
  await expect(page.getByLabel('事件元信息')).not.toContainText('CRUDE');
  await expect(page.getByLabel('当前来源机制复核')).toContainText('不补写原事件修订');
  await expect(page.getByLabel('当前来源机制复核')).toContainText('需核验受影响运量');
  await expect(page.getByTestId('semantic-evidence-card')).toHaveCount(1);
  await page.getByRole('button',{name:'查看本事件完整证明'}).click();
  await expect.poll(()=>calls.length).toBe(1);
  expect(calls[0].searchParams.get('event_id')).toBe('event-scope');
  expect(calls[0].searchParams.get('event_revision_id')).toBe('revision-scope');
  expect(calls[0].searchParams.get('target')).toBe('crude');
  await expect(page.getByLabel('证据时点')).toHaveCount(0);
});

test('selected information report carries stable report identity',async({page})=>{
  const calls=await mockEvidence(page);
  const report={id:'00000000-0000-0000-0000-000000000001',kind:'日报',title:'冻结报告',summary:'报告摘要',generated_at:'2026-09-27T12:00:00Z',qualification:'information_only',sha256:'a'.repeat(64)};
  await page.route('**/api/v1/information-reports**',route=>route.fulfill({json:route.request().url().endsWith('/content')?{...report,content:'# 冻结报告'}:{items:[report]}}));
  await page.goto('/?module=reports');
  await page.getByRole('button',{name:'查看本报告证明'}).click();
  await expect.poll(()=>calls.length).toBe(1);
  expect(calls[0].searchParams.get('report_id')).toBe(report.id);
  await expect(page.getByLabel('证据时点')).toHaveCount(0);
});

test('answer proof carries returned context pack rather than current global view',async({page})=>{
  const calls=await mockEvidence(page);
  await page.route('**/api/v1/assistant/chat',route=>route.fulfill({json:{answer:'资料不足，先核验来源。',context_pack_id:'context-pack-proof',cited_source_ids:[],evidence_level:'D',confidence:0,warnings:[],status:'degraded',display_evidence:[]}}));
  await page.goto('/?module=assistant');
  await page.getByPlaceholder('输入关于今日研判、证据、风险或报告的问题…').fill('PTA有什么依据？');
  await page.getByRole('button',{name:/发\s*送/}).click();
  await page.getByRole('button',{name:'本次回答证明'}).click();
  await expect.poll(()=>calls.length).toBe(1);
  expect(calls[0].searchParams.get('context_pack_id')).toBe('context-pack-proof');
  expect(calls[0].searchParams.get('target')).toBe('pta');
});


const batch = {
  schema_version: "seven-product-forecast.v1", batch_id: "seven-authority-test",
  generated_at: "2026-09-26T00:01:00Z", as_of_time: "2026-09-26T00:00:00Z",
  targets: ["crude", "naphtha", "px", "pta", "meg", "poy", "dty"], horizons: [1, 7, 30],
  formal_count: 0, reference_count: 21, unavailable_count: 0, contract_complete: true,
  customer_boundary: "观察参考", cells: [] as Record<string, unknown>[]
};
batch.cells = batch.targets.flatMap(target => batch.horizons.map(horizon => ({
  target, horizon_days: horizon, direction: target === "dty" ? "down" : "up", confidence: .61,
  data_status: "fresh", source_matches_label: true, history_points: 40, formal_status: "reference",
  formal_eligible: false, data_gaps: [], evidence: [], key_drivers: [], point_forecast: 105,
  interval_low: 90, interval_high: 110, latest_value: 100, predicted_change_pct: .05,
  neutral_band_pct: .006, unit: "CNY/mt", as_of_time: batch.as_of_time,
  forecast_contract: "issue-calendar.v1", target_date: "2026-09-27", confidence_kind: "heuristic_score",
  latest_observation_at: "2026-09-25", latest_visible_at: "2026-09-25T10:00:00Z",
  evaluation_status: "not_evaluated", model_version: "robust-drift-reference.v1",
  label_series_id: `${target}.v5`, label_registry_version: "seven-product-labels.v5"
})));


test('forecast cell proof keeps selected batch and 30 day period',async({page})=>{
  const calls=await mockEvidence(page);
  // This contract fixture uses its mocked batch, not the daily snapshot's
  // early render. Bootstrap later supplies main_prediction and remounts the
  // ledger; expanding native details before that commit loses its open state.
  await page.route('**/api/v1/workbench/snapshot',route=>route.fulfill({status:404,json:{detail:'No daily snapshot in this fixture'}}));
  await page.route('**/api/v1/forecasts/seven-product',route=>route.fulfill({json:batch}));
  await page.route('**/api/v1/forecasts/seven-product/history**',route=>route.fulfill({json:[]}));
  await page.goto('/?module=reports&reportView=ledger');
  await expect(page.locator('.review-sidebar-state')).not.toContainText('读取中');
  const cell=page.locator('[data-testid="seven-product-forecast-grid"] [data-target="dty"][data-horizon="30"]');
  await expect(cell).toBeVisible();
  await cell.locator('summary').first().click();
  await expect(cell.locator('details').first()).toHaveAttribute('open','');
  await cell.getByRole('button',{name:'发行时完整证明'}).click();
  await expect.poll(()=>calls.length).toBe(1);
  expect(calls[0].searchParams.get('batch_id')).toBe(batch.batch_id);
  expect(calls[0].searchParams.get('horizon_days')).toBe('30');
  expect(calls[0].searchParams.get('target')).toBe('dty');
});

test('report proof displays frozen conditional explanations separately from direct votes', async ({page}) => {
  const report={id:'00000000-0000-0000-0000-000000000002',kind:'日报',title:'条件依据冻结报告',summary:'报告摘要',generated_at:'2026-10-03T12:00:00Z',qualification:'information_only',sha256:'b'.repeat(64)};
  const review={review_id:'frozen-condition',source_target:'crude',target:'poy',relation:'upstream_context',mechanism:'logistics',direction:'up',subject:'甲公司',action:'关闭',quote:'甲公司原油运输管道目前已关闭。',source_url:'https://example.test/notice',source_title:'运输公告',published_at:'2026-10-02T02:00:00Z',reviewed_at:'2026-10-02T03:00:00Z',source_available_at:'2026-10-02T02:00:00Z',model:'test-model',time_kind:'current_state',time_anchor:'',period_start:null,period_end:null,rationale:'运量下降可能减少可交付供给，传导需核验。',conditions:['核验下游成本传导及需求承接'],counts_as_evidence:false,assessment:'ai_semantic_review_not_verified_outcome'};
  await page.route('**/api/v1/information-reports**',route=>route.fulfill({json:route.request().url().endsWith('/content')?{...report,content:'# 条件依据冻结报告'}:{items:[report]}}));
  await page.route('**/api/v1/forecasts/seven-product/evidence?**',route=>route.fulfill({json:{...response(new URL(route.request().url())),scope:'report',context_id:report.id,semantic_reviews:[review]}}));
  await page.goto('/?module=reports');
  await page.getByRole('button',{name:'查看本报告证明'}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByTestId('semantic-evidence-card')).toHaveCount(1);
  await expect(dialog).toContainText('当前支持事件 0');
  await expect(dialog).toContainText('条件依据 · 不计票');
  await expect(dialog).toContainText(review.rationale);
  await expect(dialog).toContainText(review.conditions[0]);
  await dialog.getByRole('tab',{name:'当前反证',exact:true}).click();
  await expect(dialog.locator('.ant-tabs-tabpane-active').getByTestId('semantic-evidence-card')).toHaveCount(0);
  await expect(dialog.getByTestId('semantic-evidence-card')).not.toBeVisible();
});
