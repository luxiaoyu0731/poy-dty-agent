import { expect, test } from "@playwright/test";

const chain = {
  chain_id: "context-1", claim_id: "event-1", source_target: "crude", target: "poy", relation: "upstream_context", mechanism: "logistics",
  event_label: "公开材料称运输受阻", quote: "2026年10月2日，港口运输受到限制。", source_url: "https://example.test/transport", source_title: "港口运输公告",
  event_date: "2026-10-02", event_date_source: "raw_sentence", published_at: "2026-10-02T02:00:00Z", known_at: "2026-10-02T02:05:00Z",
  state: "actual", semantic_status: "needs_review", conditions: ["需确认受限货物流向", "需确认向化工链的传导"],
  path: [{ target: "crude", label: "上游运输材料" }, { target: "px", label: "条件性原料传导" }, { target: "poy", label: "需有长丝端需求证据" }], counts_as_evidence: false, direction: "up"
};
const payload = () => ({
  schema_version: "business-evidence-view.v1", view: "current", target: "poy", horizon_days: 1, status: "available_with_gaps", batch_id: null,
  as_of_time: "2026-10-03T00:00:00Z", input_sha256: "a".repeat(64), hypothesis: "其他条件相同，该品种价格存在上行压力", model_effect: "context_only", source_rows: 2, capture_complete: true,
  coverage: [], market_baseline: {}, claims: [{ claim_id: "price-1", target: "poy", mechanism: "price", subject: "交易商", event_date: "2026-10-02", state: "unknown", semantic_status: "needs_review", expected_direction: null, quote: "POY报价9350元/吨", source_url: "https://example.test/price", source_title: "报价动态", source_tier: "B", published_at: "2026-10-02T02:00:00Z", known_at: "2026-10-02T02:05:00Z", gaps: [], inference_boundary: "价格背景" }],
  current_support: [], current_counter: [], historical_support: [], historical_counter: [], historical_other: [], other_materials: ["price-1"], mixed: [],
  current_support_episodes: 0, current_counter_episodes: 0, gaps: ["尚无通过机制核验的方向材料"], source_gaps: {}, total_claims: 1, offset: 0, next_offset: null
});

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

test("event graph presents upstream hypotheses without changing evidence counts and isolates quotations", async ({ page }) => {
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: { ...payload(), event_chains: [
    chain,
    { ...chain, chain_id: "direct-unverified", relation: "direct", target: "poy", source_target: "poy", source_url: "javascript:alert(1)", event_label: "装置计划材料", counts_as_evidence: true },
    { ...chain, chain_id: "context-checked", event_label: "已核验上游原句", semantic_status: "rule_checked", counts_as_evidence: true },
    { ...chain, chain_id: "price-excluded", mechanism: "price", event_label: "报价不应进入事件链" }
  ] } }));
  await page.goto("/?module=evidence");
  const graph = page.getByTestId("evidence-event-graph");
  await expect(graph.getByTestId("evidence-event-chain")).toHaveCount(3);
  const context = graph.getByTestId("evidence-event-chain").filter({ hasText: "公开材料称运输受阻" });
  await expect(context).toContainText("上游关联 · 条件性机制假说");
  await expect(context).toContainText("不计正反证 · 无计票权");
  await expect(context).toContainText("材料称已发生");
  await expect(context).toContainText("传导未获证实");
  await expect(context).toContainText("尚不判定方向");
  await expect(context).not.toContainText("偏强");
  await expect(context.getByRole("list", { name: "品种路径" })).toContainText("原油");
  await expect(context.getByRole("list", { name: "品种路径" })).toContainText("POY");
  await expect(context.getByRole("link", { name: "港口运输公告 ↗" }).first()).toHaveAttribute("href", "https://example.test/transport");
  await context.getByText("核对时间与原文出处", { exact: true }).click();
  await expect(context.locator(".eeg-source-details")).toContainText("系统获知时间");
  await expect(context.locator(".eeg-source-details")).toContainText("2026-10-02");
  await expect(context.locator("blockquote")).toHaveText(chain.quote);
  const unverified = graph.getByTestId("evidence-event-chain").filter({ hasText: "装置计划材料" });
  await expect(unverified).toContainText("机制方向待核验");
  await expect(unverified).toContainText("不计正反证 · 无计票权");
  await expect(unverified.locator("a")).toHaveCount(0);
  await expect(graph).not.toContainText("已通过机制规则 · 计入证据");
  await expect(graph).not.toContainText("报价不应进入事件链");
  await expect(page.locator("#evb-claims .evb-claim-column.is-support h4")).toContainText("0 个事件");
  await expect(page.locator("#evb-claims .evb-claim-column.is-counter h4")).toContainText("0 个事件");
  await graph.getByText("报价与市场背景 · 1 条（不进入事件链）", { exact: true }).click();
  await expect(graph.locator(".eeg-market-background")).toContainText("POY报价9350元/吨");
  await expect(page.locator("#evb-graph")).toContainText("已核验方向与证据关系图");
  await page.setViewportSize({ width: 375, height: 812 });
  const overflow = await graph.locator(".eeg-chain-flow").evaluateAll(els => els.filter(el => el.scrollWidth > el.clientWidth + 1).length);
  expect(overflow).toBe(0);
});

test("old dossier without event chains retains honest fallback and verified relationship audit", async ({ page }) => {
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: payload() }));
  await page.goto("/?module=evidence");
  await expect(page.getByTestId("evidence-event-graph")).toContainText("本档未提供事件机制链");
  await expect(page.getByTestId("evidence-event-chain")).toHaveCount(0);
  await expect(page.locator("#evb-graph")).toContainText("当前档案没有可成图的真实证据关系");
});

test("mechanism paths consume the same conditional atom and retain unreviewed prose without guessed routes", async ({ page }) => {
  const review = {
    review_id: "shared-transport", source_target: "crude", target: "poy", relation: "upstream_context",
    mechanism: "logistics", direction: "down", subject: "乙公司", action: "恢复",
    quote: "乙公司原油运输目前已恢复。", source_url: "https://example.test/restored", source_title: "运输恢复公告",
    published_at: "2026-10-02T02:00:00Z", reviewed_at: "2026-10-02T03:00:00Z", source_available_at: "2026-10-02T02:00:00Z",
    model: "test", time_kind: "current_state", time_anchor: "", period_start: null, period_end: null,
    rationale: "原油运输恢复可能缓解短缺，仍须核验下游成本和需求传导。", conditions: ["核验实际恢复运量及替代供给"],
    counts_as_evidence: false, assessment: "ai_semantic_review_not_verified_outcome"
  };
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: {
    ...payload(), semantic_reviews: [review], event_chains: [
      { ...chain, proof_kind: "semantic_review", semantic_review: review, chain_id: "shared-path", claim_id: review.review_id,
        quote: review.quote, source_url: review.source_url, source_title: review.source_title,
        event_label: "运输与交付变化 · 乙公司", event_date: null, event_date_source: null,
        conditions: review.conditions, direction: null },
      { ...chain, proof_kind: "unreviewed_material", chain_id: "raw-forecast", event_label: "分析师预测报道",
        quote: "Analysts raised crude price forecasts after shipping resumed.", state: "unknown", path: [] }
    ]
  } }));
  await page.goto("/?module=evidence");
  const graph = page.getByTestId("evidence-event-graph");
  await expect(graph.getByTestId("evidence-event-chain")).toHaveCount(1);
  await expect(graph.locator(".eeg-mechanism")).toContainText(review.rationale);
  await expect(graph.locator(".eeg-mechanism")).toContainText(review.conditions[0]);
  await expect(graph).toContainText("当前状态报道；发生日未确认");
  await expect(graph).not.toContainText("材料称计划实施");
  await expect(graph.getByRole("link", { name: "核对同一条支持／相反依据" })).toHaveAttribute("href", `#evb-semantic-${review.review_id}`);
  await expect(page.getByTestId("semantic-evidence-card")).toContainText(review.rationale);
  await graph.getByText("待复核原始材料 · 1 条（尚未建立传导路径）", { exact: true }).click();
  await expect(graph.locator(".eeg-market-background").filter({hasText: "Analysts"})).toBeVisible();
  await expect(graph.getByTestId("evidence-event-chain")).toHaveCount(1);
  await expect(page.getByRole("heading", {name: "当前支持 0 个事件", exact: true})).toBeVisible();
  await expect(page.getByRole("heading", {name: "当前相反驱动 0 个事件", exact: true})).toBeVisible();
  await expect(page.locator(".evb-pagination")).toContainText("规则抽取材料 1 条 · AI 条件材料 1 条（不计票）");
});

test("semantic support and counter show sourced conditions without granting votes and start before slow bootstrap", async ({ page }) => {
  let releaseBootstrap!: () => void;
  const bootstrap = new Promise<void>(resolve => { releaseBootstrap = resolve; });
  await page.route("**/api/v1/delivery/status**", async route => { await bootstrap; await route.continue(); });
  const review = {
    review_id: "review-up", source_target: "crude", target: "poy", relation: "upstream_context", mechanism: "logistics", direction: "up",
    subject: "甲公司", action: "关闭", quote: "甲公司原油运输管道目前已关闭。", source_url: "https://example.test/notice", source_title: "运输公告",
    published_at: "2026-10-02T02:00:00Z", reviewed_at: "2026-10-02T03:00:00Z", source_available_at: "2026-10-02T02:00:00Z", model: "test-model",
    time_kind: "current_state", time_anchor: "", period_start: null, period_end: null, rationale: "运量下降可能减少可交付供给，传导需核验。",
    binding_method: "ai_coreference", binding_quote: "甲公司原油运输管道目前已关闭。甲公司发布说明。", binding_reason: "同一主体的运输管道关联明确，实际影响运量仍需核验。", binding_model: "critic-test", conditions: ["核验下游成本传导及需求承接"], counts_as_evidence: false, assessment: "ai_semantic_review_not_verified_outcome"
  };
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: {
    ...payload(), semantic_reviews: [review, { ...review, review_id: "review-down", direction: "down", subject: "乙公司", action: "恢复", quote: "乙公司原油运输目前已恢复。", rationale: "运输恢复可能缓解短缺，但不等于终端价格下跌。" }, { ...review, review_id: "invalid-vote", counts_as_evidence: true }]
  } }));
  try {
    await page.goto("/?module=evidence");
    await expect(page.getByTestId("semantic-evidence-card")).toHaveCount(2, { timeout: 10_000 });
    await expect(page.locator("#evb-claims .ant-empty")).toHaveCount(0);
    await expect(page.getByTestId("evidence-event-graph")).toContainText("另有 2 条原文绑定");
    await expect(page.locator("#evb-graph .evb-flow-node.is-conditional")).toHaveCount(2);
    await expect(page.locator("#evb-graph .react-flow__edge.is-conditional")).toHaveCount(2);
    const graphCards = page.locator("#evb-graph .evb-flow-node");
    await expect(graphCards).toHaveCount(3);
    expect(await graphCards.evaluateAll(cards => {
      const boxes = cards.map(card => card.getBoundingClientRect());
      return boxes.every((a, i) => boxes.every((b, j) => i === j || a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
    })).toBe(true);
    await expect(page.locator("#evb-graph .react-flow__edgelabel-renderer")).toBeEmpty();
    await expect(page.locator(".evb-verdict-card.is-conclusion")).toContainText("正式状态暂不可核对");
    await expect(page.locator(".evb-verdict-card.is-conclusion")).not.toContainText("未通过");
    await page.getByTestId("semantic-evidence-card").first().getByText("查看原文与核验记录", { exact: true }).click();
    await expect(page.getByTestId("semantic-evidence-card").first()).toContainText("critic-test");
    await expect(page.getByTestId("semantic-evidence-card").first()).toContainText("不等于现实事实已经独立核实");
    await expect(page.locator("#evb-claims")).toContainText("条件支持依据");
    await expect(page.locator("#evb-claims")).toContainText("条件相反依据");
    await expect(page.locator("#evb-claims")).toContainText("0 个事件");
    await expect(page.getByTestId("semantic-evidence-card").first()).toContainText("不计票");
    await expect(page.getByTestId("semantic-evidence-card").first()).toContainText("发生日未确认");
    await expect(page.getByTestId("semantic-evidence-card").first().getByRole("link")).toHaveAttribute("href", "https://example.test/notice");
  } finally { releaseBootstrap(); }
});


test("dossier pending state uses content loading and clears after a real response", async ({ page }) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", async route => {
    await pending;
    await route.fulfill({ json: payload() });
  });
  try {
    await page.goto("/?module=evidence");
    const loading = page.locator(".evb-root .content-loading");
    await expect(page.getByRole("status").filter({ hasText: "正在读取证据档案" })).toBeVisible();
    await expect(page.getByText("正在读取证据档案", { exact: true })).toBeVisible();
    await expect(page.locator(".ant-spin-text").filter({ hasText: "正在读取证据档案" })).toHaveCount(0);
    release();
    await expect(page.locator("#evb-verdict")).toBeVisible();
    await expect(loading).toHaveCount(0);
  } finally { release(); }
});

test("asymmetric current and historical relations have separate ports and readable non-overlapping cards", async ({ page }) => {
  const base = payload();
  const claims = Array.from({ length: 6 }, (_, index) => ({
    ...base.claims[0], claim_id: `relation-${index}`, mechanism: "supply", state: "actual",
    semantic_status: "rule_checked", expected_direction: index === 5 ? "down" : "up",
    quote: `第${index + 1}条装置供给变化的原文材料`, gaps: []
  }));
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: {
    ...base, claims, total_claims: 6, other_materials: [],
    current_support: claims.slice(0, 3).map(claim => claim.claim_id),
    current_counter: [claims[5].claim_id], current_support_episodes: 3, current_counter_episodes: 1,
    historical_support: claims.slice(3, 5).map(claim => ({ claim_id: claim.claim_id, relation: "support",
      outcome: { state: "settled", direction: "up" }, use: "retrospective_context_not_historical_forecast_input", causality_proven: false }))
  } }));
  await page.goto("/?module=evidence");
  const cards = page.locator("#evb-graph .evb-flow-node");
  await expect(cards).toHaveCount(7);
  await expect(page.locator("#evb-graph .is-prop .react-flow__handle.target")).toHaveCount(6);
  await expect.poll(() => cards.evaluateAll(elements => {
    const boxes = elements.map(element => element.getBoundingClientRect());
    return boxes.every((a, i) => boxes.every((b, j) => i === j || a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
  })).toBe(true);
  expect(await cards.first().locator("strong").evaluate(element => parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(16);
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByTestId("evb-graph-canvas")).toBeVisible();
  await expect(page.locator("#evb-graph .react-flow__controls-zoomin")).toBeEnabled();
  await expect(page.locator("#evb-graph .react-flow__node.draggable")).toHaveCount(0);
});


test("relationship text stays on cards in inline and enlarged views at desktop and phone scale", async ({ page }) => {
  const base = payload();
  const review = {
    review_id: "conditional", source_target: "crude", target: "poy", relation: "upstream_context",
    mechanism: "logistics", direction: "up", subject: "运输公司", action: "关闭",
    quote: "运输公司原油管道目前已关闭。", source_url: "https://example.test/notice", source_title: "运输公告",
    published_at: "2026-10-02T02:00:00Z", reviewed_at: "2026-10-02T03:00:00Z",
    source_available_at: "2026-10-02T02:00:00Z", model: "test", time_kind: "current_state",
    time_anchor: "", period_start: null, period_end: null,
    rationale: "运输受阻可能影响交付；价格方向仍取决于下游需求与替代供给。",
    conditions: ["核验实际交付"], counts_as_evidence: false,
    assessment: "ai_semantic_review_not_verified_outcome"
  };
  const claims = Array.from({ length: 4 }, (_, index) => ({
    ...base.claims[0], claim_id: `direct-${index}`, mechanism: "supply", state: "actual",
    semantic_status: "rule_checked", expected_direction: index % 2 ? "down" : "up",
    quote: `第${index + 1}条已核验装置供给变化原文`, gaps: []
  }));
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => route.fulfill({ json: {
    ...base, claims, total_claims: 4, other_materials: [], current_support: [claims[0].claim_id],
    current_counter: [claims[1].claim_id], current_support_episodes: 1, current_counter_episodes: 1,
    historical_support: [{ claim_id: claims[2].claim_id, relation: "support", outcome: { state: "settled", direction: "up" } }],
    historical_counter: [{ claim_id: claims[3].claim_id, relation: "counter", outcome: { state: "settled", direction: "down" } }],
    semantic_reviews: [review, { ...review, review_id: "conditional-down", direction: "down" }]
  } }));
  for (const size of [{width:1440,height:900},{width:1280,height:720},{width:390,height:844}]) {
    await page.setViewportSize(size);
    await page.goto("/?module=evidence");
    const inline = page.getByTestId("evb-graph-canvas");
    await expect(inline.locator(".evb-flow-node")).toHaveCount(7);
    await expect(inline.locator(".react-flow__edge")).toHaveCount(6);
    await expect(inline.locator(".react-flow__edge-textwrapper, .react-flow__edge-text")).toHaveCount(0);
    await page.getByRole("button", {name: "放大查看"}).click();
    const modal = page.getByTestId("evb-graph-modal");
    await expect(modal.locator(".evb-flow-node")).toHaveCount(7);
    await expect(modal.locator(".react-flow__edge")).toHaveCount(6);
    await expect(modal.locator(".react-flow__edge-textwrapper, .react-flow__edge-text")).toHaveCount(0);
    await expect(modal.locator(".evb-flow-node.is-conditional")).toContainText(["不计票", "不计票"]);
    await expect.poll(() => modal.locator(".evb-flow-node").evaluateAll(elements => {
      const boxes = elements.map(element => element.getBoundingClientRect());
      return boxes.every((a,i) => boxes.every((b,j) => i === j || a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
    })).toBe(true);
    await page.getByRole("button", {name:"Close", exact:true}).click();
  }
});

 test("current evidence follows completed background cache without forcing a rebuild", async ({ page }) => {
  await page.clock.install();
  const queries: URL[] = [];
  let completed = false;
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => {
    queries.push(new URL(route.request().url()));
    return route.fulfill({ json: { ...payload(), revalidating: !completed, input_sha256: (!completed ? "a" : "b").repeat(64) } });
  });
  await page.goto("/?module=evidence");
  await expect(page.getByText(/正在检查后台更新/)).toBeVisible();
  const before = queries.length;
  completed = true;
  await page.clock.fastForward(10001);
  await expect.poll(() => queries.length).toBeGreaterThan(before);
  expect(queries.at(-1)!.searchParams.get("refresh")).not.toBe("true");
  expect(queries.at(-1)!.searchParams.get("input_sha256")).toBeNull();
  await expect(page.getByText(/正在检查后台更新/)).toHaveCount(0);
});

test("frozen issuance evidence never follows background current-cache updates", async ({ page }) => {
  await page.clock.install();
  let issuedCalls = 0;
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => {
    const view = new URL(route.request().url()).searchParams.get("view");
    if (view === "issued") issuedCalls += 1;
    return route.fulfill({ json: { ...payload(), view, revalidating: true } });
  });
  await page.goto("/?module=evidence");
  await expect(page.getByText(/正在检查后台更新/)).toBeVisible();
  await page.getByText("最近发行时证据", { exact: true }).click();
  await expect.poll(() => issuedCalls).toBeGreaterThan(0);
  await expect(page.getByText(/正在检查后台更新/)).toHaveCount(0);
  const before = issuedCalls;
  await page.clock.fastForward(70000);
  expect(issuedCalls).toBe(before);
});
