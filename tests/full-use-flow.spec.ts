import { expect, test } from "@playwright/test";

test("empty intelligence guides to runs, preserves subview on refresh and restores history", async ({ page }) => {
  await page.route("**/api/v1/intelligence/brief**", route => route.fulfill({ json: { availability_status: "data_not_ready", brief: null } }));
  await page.route("**/api/v1/intelligence/runs**", route => route.fulfill({ json: { items: [{
    run_id: "failed-run", run_type: "projection", provider_id: "legacy_news_articles", business_date: "2026-09-07",
    status: "failed", duration_ms: 10, counts: { input: 0, inserted: 0, existing: 0 },
    degraded_reasons: [], error_code: "intelligence_timestamp_invalid", error_detail_safe: "IntelligenceStorageError"
  }] } }));
  await page.goto("/?module=intelligence");
  await page.getByRole("button", { name: "查看运行与来源", exact: true }).click();
  await expect(page).toHaveURL(/intelligenceView=runs/);
  await expect(page.getByRole("tab", {name: "运行与来源"})).toBeFocused();
  await expect(page.getByRole("tab", { name: "运行与来源" })).toHaveAttribute("aria-selected", "true");
  // v34：运行视图内部还有 来源目录/运行审计 二级页签。
  await page.getByRole("tab", { name: "运行审计", exact: true }).click();
  const auditLog = page.getByLabel("运行审计").first();
  await expect(auditLog).toContainText("intelligence_timestamp_invalid");
  await page.reload();
  await expect(page.getByRole("tab", { name: "运行与来源" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "运行审计", exact: true }).click();
  await page.getByRole("button", { name: "刷新运行记录" }).click();
  await expect(auditLog).toContainText("IntelligenceStorageError");
  await page.getByRole("tab", { name: "全球雷达" }).click();
  await page.goBack();
  await expect(page.getByRole("tab", { name: "运行与来源" })).toHaveAttribute("aria-selected", "true");
  await page.goForward();
  await expect(page.getByRole("tab", { name: "全球雷达" })).toHaveAttribute("aria-selected", "true");
});

test("evidence graph draws only real dossier relations and shows a concrete empty state", async ({ page }) => {
  // v34：检索步骤 UI 已退役；等价不变量 = 图谱节点只来自真实档案分类，
  // 无材料时显示具体空态（不伪造流程示意节点）。
  await page.goto("/?module=evidence");
  const board = page.locator("[data-testid='evidence-verification-board']");
  await expect(board).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#evb-verdict")).toBeVisible();
  const graphNodes = board.locator("[data-testid='evb-graph-canvas'] .evb-flow-node");
  if ((await graphNodes.count()) > 1) {
    // 每个证据节点必须能在页面中定位到对应证据卡（真实关系，非占位）。
    const claimNodes = graphNodes.filter({ hasText: /当前支持材料|当前相反驱动|历史类似/ });
    for (let i = 0; i < await claimNodes.count(); i++) {
      await claimNodes.nth(i).click();
      await expect(board.locator(".evb-claim-card.is-selected").first()).toBeVisible({ timeout: 10_000 });
    }
  } else {
    await expect(board.getByText(/当前档案没有可成图的真实证据关系/)).toBeVisible({ timeout: 20_000 });
  }
});

test("chain branches cannot overlap at narrow effective viewport", async ({ page }) => {
  await page.setViewportSize({ width: 650, height: 700 });
  // v34：链路为 review-material-flow 六段（含 PTA/MEG 与 POY/DTY 分支对）。
  await page.goto("/?module=market");
  await expect(page.locator(".review-material-flow")).toBeVisible();
  const stages = page.locator(".review-material-flow > .review-material-stage");
  await expect(stages).toHaveCount(6);
  const rects = await stages.evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()));
  for (let i = 1; i < rects.length; i++) expect(rects[i].left).toBeGreaterThanOrEqual(rects[i - 1].right - 1);
  const chainBody = page.locator(".market-chain-panel .delivery-panel-body");
  const overflow = await chainBody.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  expect(overflow.scroll).toBeGreaterThanOrEqual(overflow.client);
});

test("overview uses the same Chinese reading overview as the event page", async ({ page }) => {
  await page.route("**/api/v1/workbench/event-library**", async route => {
    const response = await route.fetch(); const body = await response.json();
    expect(body.events.length).toBeGreaterThan(0);
    body.events[0].overview_text = "隔离验证：原油供应变化的中文概述。";
    body.events[0].overview_basis = "title";
    await route.fulfill({ json: body });
  });
  await page.goto("/?module=overview");
  await expect(page.locator(".overview-event-list")).toContainText("隔离验证：原油供应变化的中文概述。", { timeout: 45_000 });
  // 等待仍在进行的拦截回调结束，避免页面销毁截断 route.fetch。
  await page.unrouteAll({ behavior: "wait" });
});

test("assistant evidence opens traceable details and restores focus", async ({ page }) => {
  await page.route('**/api/v1/assistant/chat', route => route.fulfill({ json: {
    answer: '隔离问答结果', status: 'degraded', cited_source_ids: ['fixture-source'], warnings: [],
    display_evidence: [{ id: 'fixture-evidence', title: '可追溯测试证据', category: '原油',
      summary: '完整证据正文用于验证详情，不应只弹标题。', observed_label: '2026-09-07 10:00 +08:00',
      tone: 'info', url: 'https://example.test/source' }]
  } }));
  await page.goto('/?module=assistant');
  await page.getByRole('tab', { name: '推荐问题' }).click();
  await page.getByRole('button', { name: '哪些证据支持当前结论？', exact: true }).click();
  await page.getByRole('tab', { name: '引用证据' }).click();
  const reference = page.getByRole('button', { name: /可追溯测试证据/ });
  await reference.click();
  const drawer = page.getByRole('dialog', { name: '问答证据详情' });
  await expect(drawer).toContainText('完整证据正文用于验证详情');
  await expect(drawer).toContainText('fixture-evidence');
  await expect(drawer.getByRole('link', { name: '查看原始来源' })).toHaveAttribute('href', 'https://example.test/source');
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible();
  await expect(reference).toBeFocused();
});

test('assistant answer renders per-gate quality badges instead of review wording', async ({ page }) => {
  await page.route('**/api/v1/assistant/chat', route => route.fulfill({ json: {
    answer: '结论：示例回答。\n可信边界：观察级。', status: 'degraded', cited_source_ids: [], warnings: [],
    answer_sections: {
      conclusion: '结论：示例回答。', evidence_points: [], counter_evidence: [], risks: [],
      next_steps: [], confidence_boundary: '观察级。'
    },
    quality: {
      freshness_status: '按当前可见证据生成', evidence_count: 2, missing_evidence: [], needs_review: true,
      overall: 'passed_with_flags',
      gates: [
        { name: 'formal_evidence_gate', label: '引用门禁', passed: true, reason: '' },
        { name: 'claim_entailment_gate', label: '逐句核验', passed: false, reason: '结论或依据含未获逐句引用支持的表述（如多日期综合数值）' },
        { name: 'evidence_conflict', label: '冲突检查', passed: true, reason: '' }
      ]
    }
  } }));
  await page.goto('/?module=assistant');
  await page.getByRole('tab', { name: '推荐问题' }).click();
  await page.getByRole('button', { name: '今天上游成本压力怎么看？', exact: true }).click();
  const gates = page.locator('[data-testid="assistant-quality-gates"]');
  await expect(gates).toBeVisible({ timeout: 30_000 });
  await expect(gates).toContainText('引用门禁通过');
  await expect(gates).toContainText('逐句核验未过');
  await expect(gates).toContainText('冲突检查通过');
  await expect(page.locator('[data-testid="assistant-quality-reasons"]')).toContainText('多日期综合数值');
  await expect(page.locator('.assistant-chat-panel')).toContainText('已回答');
  // The retired single-operator review wording must never appear.
  await expect(page.locator('.assistant-chat-panel')).not.toContainText('人工复核');
  await expect(page.locator('.assistant-chat-panel')).not.toContainText('需关注');
});

for (const viewport of [{width:1042,height:614},{width:651,height:384},{width:850,height:768}]) {
  test(`chain last branch reachable at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/?module=market');
    const panel = page.locator('.market-chain-panel');
    await panel.scrollIntoViewIfNeeded();
    const scroller = panel.locator('.delivery-panel-body');
    const last = panel.locator('.review-material-price').last();
    await last.scrollIntoViewIfNeeded();
    await expect(last).toBeVisible();
    const box = await last.boundingBox(); const region = await scroller.boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(region!.x + region!.width + 1);
    expect(box!.x).toBeGreaterThanOrEqual(region!.x - 1);
  });
}

test('narrow evidence workspace keeps summary, graph, reasoning and sources reachable', async ({ page }) => {
  await page.setViewportSize({ width: 850, height: 768 });
  // v34：证据页为核验台（五区块纵排，页主体内部滚动）。
  await page.goto('/?module=evidence');
  const board = page.locator('[data-testid="evidence-verification-board"]');
  await expect(board).toBeVisible();
  for (const selector of ['#evb-verdict', '#evb-claims', '#evb-history', '#evb-gaps', '#evb-graph']) {
    const panel = board.locator(selector);
    await panel.scrollIntoViewIfNeeded();
    const bounds = await panel.boundingBox();
    expect(bounds).toBeTruthy();
    expect(bounds!.x).toBeGreaterThanOrEqual(-1);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(851);
  }
  const body = page.locator('.evidence-page .delivery-page-body');
  expect(await body.evaluate(el => el.scrollHeight > el.clientHeight)).toBeTruthy();
});

test('interactive answer remains pending beyond the ordinary read timeout', async ({ page }) => {
  await page.route('**/api/v1/assistant/chat', async route => {
    await new Promise(resolve => setTimeout(resolve,13_000));
    await route.fulfill({json:{answer:'十三秒后返回的真实响应形状',status:'degraded',display_evidence:[],warnings:[]}});
  });
  await page.goto('/?module=assistant');
  await page.getByLabel('输入研判问题').fill('检查较慢的问答');
  await page.getByRole('button',{name:/发\s*送/}).click();
  await expect(page.locator('.chat-messages')).toContainText('十三秒后返回',{timeout:20_000});
  await expect(page.getByRole('button',{name:'重试',exact:true})).toHaveCount(0);
});

test('slow observation generation stays single-flight and its saved record survives reload', async ({ page }) => {
  let writes = 0;
  let saved = false;
  const record = { observation_id: 'obs-acceptance-slow', created_at: '2026-09-07T14:22:10+00:00',
    as_of_time: '2026-09-07T14:00:00+00:00', direction: '中性', confidence: 0.5,
    rationale: '隔离环境已保存的观察依据', formal_report_eligible: false };
  await page.route('**/api/v1/predictions/observations', async route => {
    if (route.request().method() === 'POST') {
      writes++;
      await new Promise(resolve => setTimeout(resolve, 13_000));
      saved = true;
      await route.fulfill({ json: record });
    } else await route.fulfill({ json: { items: saved ? [record] : [] } });
  });
  await page.goto('/?module=reports&reportView=ledger');
  await page.getByRole('tab', { name: '观察与到期复盘' }).click();
  const generate = page.getByRole('button', { name: '生成观察级材料' });
  await generate.click();
  await expect(generate).toBeDisabled();
  await expect(page.getByText(/已生成非正式观察记录 obs-acceptance-slow/)).toBeVisible({ timeout: 20_000 });
  await expect(generate).toBeEnabled();
  expect(writes).toBe(1);
  await page.reload();
  await page.getByRole('tab', { name: '观察与到期复盘' }).click();
  await expect(page.getByText('隔离环境已保存的观察依据', { exact: true })).toBeVisible();
  expect(writes).toBe(1);
});

test('uncertain observation response reads saved records without automatically repeating the write', async ({ page }) => {
  let writes = 0;
  await page.route('**/api/v1/predictions/observations', async route => {
    if (route.request().method() === 'POST') {
      writes++;
      await route.fulfill({ status: 502, json: { detail: 'isolated uncertain response' } });
    } else await route.fulfill({ json: { items: writes ? [{ observation_id: 'obs-acceptance-uncertain',
      created_at: '2026-09-07T14:22:10+00:00', as_of_time: '2026-09-07T14:00:00+00:00',
      direction: '中性', confidence: 0.5, rationale: '响应中断但已保存的记录' }] : [] } });
  });
  await page.goto('/?module=reports&reportView=ledger');
  await page.getByRole('tab', { name: '观察与到期复盘' }).click();
  await page.getByRole('button', { name: '生成观察级材料' }).click();
  await expect(page.getByText(/尚未确认生成结果/)).toBeVisible();
  await expect(page.getByText('响应中断但已保存的记录', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重新读取观察记录' }).click();
  expect(writes).toBe(1);
});

test('daily brief exposes remaining events and retains dates beside Chinese overviews', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-09-07T14:00:00Z'));
  const events = Array.from({ length: 6 }, (_, index) => ({ event_revision_id: `revision-${index}`,
    title: `中文概述 ${index}`, overview_text: `Original source ${index}`, category: 'energy',
    confidence: 0.5, evidence_count: 1, last_seen_at: '2026-08-04T01:00:00Z' }));
  await page.route('**/api/v1/intelligence/brief**', route => route.fulfill({ json: {
    availability_status: 'available', brief: { business_date: '2026-09-07', status: 'ready',
      cutoff_at: '2026-09-07T00:20:00Z', scheduled_publish_at: '2026-09-07T01:30:00Z',
      released_at: '2026-09-07T01:30:00Z', selected_events: events,
      selected_event_revision_ids: events.map(event => event.event_revision_id),
      coverage: {}, gaps: [], horizon_impact: [], watch_items: [] } } }));
  // v34：每日摘要页签已退役（由 09:31 简报发布承担）；中文概述与收录日期
  // 的呈现等价物在全球雷达列表（服务端游标分页，标题+收录时间逐条标注）。
  await page.route('**/api/v1/intelligence/events?**', route => route.fulfill({ json: {
    schema_version: 'industrial-intelligence.v1', snapshot_at: '2026-09-07T14:00:00Z', snapshot_id: 'fx',
    items: events.map(e => ({ ...e, event_id: e.event_revision_id, revision_no: 1, status: 'open',
      region_codes: [], product_ids: ['crude'],
      as_of_time: '2026-08-04T01:00:00Z', relevance_score: 60, severity_score: 50, urgency_score: 40,
      location_precision: null, gap_count: 0, payload_sha256: '0'.repeat(64) })),
    has_more: false, next_cursor: null, applied_filters: {} } }));
  await page.goto('/?module=intelligence');
  await expect(page.getByRole('button', { name: /中文概述/ })).toHaveCount(6);
  await expect(page.getByLabel('雷达事件列表')).toContainText('2026/8/4');
});

test('pipeline graph statuses and agent drawer render honestly from the aggregated API', async ({ page }) => {
  const now = new Date().toISOString();
  const graphNode = (id: string, kind: 'code' | 'agent', name: string, status: string, detail: string, edgeGroup: string) => ({
    id, kind, name, status, status_detail: detail, timestamp: now, edge_group: edgeGroup
  });
  const graph = {
    schema_version: 'pipeline_graph.v1',
    business_date: '2026-09-16',
    generated_at: now,
    nodes: [
      graphNode('collect', 'code', '采集', 'degraded', '今日 46 次源异常、23 次超时。', 'data'),
      graphNode('clean', 'code', '清洗去重门禁', 'ok', '质量门正常。', 'data'),
      graphNode('index', 'code', '语义索引', 'ok', '索引就绪。', 'data'),
      graphNode('event_summary', 'agent', '事件摘要', 'ok', '摘要队列已清空。', 'judgement'),
      graphNode('event_overview', 'agent', '事件总览', 'ok', '覆盖率达标。', 'judgement'),
      graphNode('factor_score', 'code', '因子打分', 'ok', '因子分已计算。', 'judgement'),
      graphNode('seven_product', 'code', '七产品判断与OOS', 'waiting', '链尚未运行到该环节。', 'judgement'),
      graphNode('counter_scan', 'agent', '反证扫描', 'ok', '扫描完成，2 条反证发现。', 'judgement'),
      graphNode('daily_interpretation', 'agent', '日报解读', 'ok', '四节叙述已生成。', 'judgement'),
      graphNode('report_assembly', 'code', '研报组装', 'ok', '晨报已发布。', 'judgement'),
      graphNode('assistant', 'agent', '研判助手', 'idle', '空闲，可随时提问。', 'assistant')
    ],
    edges: [
      { from: 'collect', to: 'clean', kind: 'flow' },
      { from: 'clean', to: 'index', kind: 'flow' },
      { from: 'index', to: 'event_summary', kind: 'flow' },
      { from: 'event_summary', to: 'event_overview', kind: 'flow' },
      { from: 'event_overview', to: 'factor_score', kind: 'flow' },
      { from: 'factor_score', to: 'seven_product', kind: 'flow' },
      { from: 'seven_product', to: 'counter_scan', kind: 'flow' },
      { from: 'counter_scan', to: 'daily_interpretation', kind: 'flow' },
      { from: 'daily_interpretation', to: 'report_assembly', kind: 'flow' },
      { from: 'event_summary', to: 'assistant', kind: 'dashed' },
      { from: 'daily_interpretation', to: 'assistant', kind: 'dashed' }
    ]
  };
  const nodeDetail = {
    schema_version: 'pipeline_graph.v1',
    business_date: '2026-09-16',
    node_id: 'counter_scan',
    kind: 'agent',
    name: '反证扫描',
    status_block: { status: 'ok', status_detail: '扫描完成，2 条反证发现。', timestamp: now, metrics: {}, sources: ['counter-scan artifact'] },
    input_summary: { snapshot_id: 'snap-2026-09-16', events: 12, evidence_documents: 24 },
    output_summary: { scan_outcome: 'counter_evidence_found', findings: 2, stripped: 1 },
    evidence_entries: [
      { kind: 'doc', id: 'doc-4451', note: '发改委公告引文' },
      { kind: 'state_source', id: 'counter-scan/2026-09-16.json', note: '' }
    ],
    agent_extra: {
      recent_runs: [
        { run_id: 'trace-1', started_at: now, latency_ms: 9100, status: 'ok', gate_result: '' }
      ],
      daily_cost: { calls: 1, amount_micros: 9572, currency: 'CNY' }
    }
  };
  await page.route('**/api/v1/pipeline/graph**', route => route.fulfill({ json: graph }));
  await page.route('**/api/v1/pipeline/nodes/counter_scan**', route => route.fulfill({ json: nodeDetail }));
  const usdNodeDetail = {
    ...nodeDetail,
    node_id: 'event_overview',
    name: '事件总览',
    input_summary: { limit_microusd: 10_000_000, used_microusd: 2_000, failures_today: 0 },
    output_summary: { settled_count: 1, failures_today: 0 },
    agent_extra: {
      recent_runs: [],
      daily_cost: {
        calls: 1, native_amount_micros: 2_000, native_currency: 'USD',
        display_amount_micros: 14_500, display_currency: 'CNY',
        fx_rate: 7.25, fx_version: 'usd-cny-static-2026-09@7.25', note: ''
      }
    }
  };
  await page.route('**/api/v1/pipeline/nodes/event_overview**', route => route.fulfill({ json: usdNodeDetail }));
  await page.goto('/?module=workflow');

  const degradedNode = page.locator('.react-flow__node[data-id="collect"]');
  await expect(degradedNode).toContainText('降级', { timeout: 30_000 });
  await expect(degradedNode).toContainText('源异常');
  await expect(page.locator('.react-flow__node[data-id="assistant"]')).toContainText('空闲');
  await expect(page.locator('.react-flow__node[data-id="seven_product"]')).toContainText('外部等待');
  // v34：状态计数在管线面板头部（"降级 N"）。
  await expect(page.locator('.delivery-content')).toContainText('降级 1', { timeout: 30_000 });

  await page.locator('.react-flow__node[data-id="counter_scan"]').click();
  const drawer = page.locator('[data-testid="pipeline-node-drawer"]');
  await expect(drawer).toBeVisible({ timeout: 30_000 });
  await expect(drawer).toContainText('反证发现2');
  await expect(drawer).toContainText('¥0.0096 · 1 次调用');
  await expect(drawer).toContainText('9.1 秒');
  await expect(drawer).toContainText('发现反证');
  await expect(drawer).toContainText('文档');

  // USD-native node: the drawer folds the cost to a CNY display value and
  // annotates the native amount plus the versioned static FX anchor.
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible({ timeout: 10_000 });
  await page.locator('.react-flow__node[data-id="event_overview"]').click();
  await expect(drawer).toContainText('¥0.0145 · 1 次调用', { timeout: 30_000 });
  await expect(drawer.locator('[data-testid="pipeline-cost-block"]')).toContainText('原生 $0.002');
  await expect(drawer.locator('[data-testid="pipeline-cost-block"]')).toContainText('汇率 7.25');
  await expect(drawer.locator('[data-testid="pipeline-cost-block"]')).toContainText('usd-cny-static-2026-09@7.25');
});
