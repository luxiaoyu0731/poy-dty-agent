import { expect, test } from "@playwright/test";

const payload = (target = "poy", view = "current") => ({
  schema_version: "business-evidence-view.v1", view, target, horizon_days: 1,
  status: "available_with_gaps", batch_id: null, as_of_time: "2026-09-27T16:00:00Z",
  input_sha256: "a".repeat(64), hypothesis: "其他条件相同，该品种价格存在上行压力",
  model_effect: "context_only", source_rows: 3, capture_complete: true,
  coverage: [{mechanism:"supply",label:"供应与装置",automatic_scope:"实际装置动作",status:"available",source_claims:1,stored_claims:1,usable_episodes:1}],
  claims: [{claim_id:"claim1",target,mechanism:"supply",subject:"甲公司",event_date:"2026-09-27",state:"actual",semantic_status:"rule_checked",expected_direction:"up",quote:"2026年9月27日，甲公司POY装置已停产。",source_url:"https://example.test/article",source_title:"装置公告",source_tier:"B",published_at:"2026-09-27T08:00:00Z",known_at:"2026-09-27T16:00:00Z",gaps:[],inference_boundary:"机制推断，非价格必然变化"}],
  current_support:["claim1"],current_counter:[],historical_support:[],historical_counter:[],historical_other:[],other_materials:[],mixed:[],current_support_episodes:1,current_counter_episodes:0,
  market_baseline:{value:8500,unit:"CNY/mt",observed_at:"2026-09-25",status:"fresh"},
  gaps:["没有满足条件的历史案例"],source_gaps:{},total_claims:51,offset:0,next_offset:50,
});

test("price context is neutral while genuine material gaps remain highlighted", async ({page}) => {
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", route => {
    const data = payload();
    const quote = {...data.claims[0], mechanism: "price", expected_direction: null, state: "unknown", semantic_status: "needs_review", quote: "交易商涤纶POY报价9350元/吨。", source_title: "涤纶POY商品报价动态（2026-09-27）", event_date_source: "dated_price_report_title", gaps: ["该机制需要语义复核，未自动判断方向"]};
    return route.fulfill({json: {...data, claims: [quote, {...quote, claim_id: "missing-date", event_date: null, event_date_source: null, source_title: "无日期报价", gaps: ["该机制需要语义复核，未自动判断方向", "缺少明确且唯一的发生日期或报告期"]}], current_support: [], other_materials: ["claim1", "missing-date"], next_offset: null}});
  });
  await page.goto("/?module=evidence");
  const dated = page.locator("#evb-claim-claim1");
  await expect(dated).toContainText("报告标题日期 2026-09-27");
  await expect(dated.locator(".evb-claim-scope")).toContainText("报价材料作为价格背景");
  await expect(dated.locator(".evb-claim-gaps")).toHaveCount(0);
  await expect(dated).toContainText("待核验");
  await expect(dated).not.toContainText("通过机制规则");
  await expect(page.locator("#evb-claim-missing-date .evb-claim-gaps")).toHaveText("该材料自身缺口：缺少明确且唯一的发生日期或报告期");
});

test("evidence dossier reads only, groups evidence, paginates and preserves issued boundary", async ({page}, testInfo) => {
  const requests: URL[] = [];
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", async route => {
    expect(route.request().method()).toBe("GET");
    const url = new URL(route.request().url()); requests.push(url);
    const view = url.searchParams.get("view")!;
    const data = payload(url.searchParams.get("target")!,view);
    if (view === "issued") return route.fulfill({json:{...data,status:"legacy_input",coverage:[],claims:[],current_support:[],gaps:["该批次早于统一证据档案，不回写历史"],total_claims:0,next_offset:null}});
    if (url.searchParams.get("offset") === "50") return route.fulfill({json:{...data,offset:50,next_offset:null,claims:[],current_support:[]}});
    return route.fulfill({json:data});
  });
  await page.goto("/?module=market");
  await page.getByRole("button",{name:"品种正反证"}).first().click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByText("当前资料用于分析核验，不改写已发行预测")).toBeVisible();
  await expect(drawer.getByText("2026年9月27日，甲公司POY装置已停产。",{exact:true})).toBeVisible();
  await expect(drawer.getByRole("link",{name:"装置公告"})).toHaveAttribute("href","https://example.test/article");
  await expect.poll(async () => {
    const box = await drawer.boundingBox();
    return Boolean(box && box.x >= 0 && box.x + box.width <= page.viewportSize()!.width + 1);
  }).toBe(true);
  await page.screenshot({path:testInfo.outputPath("evidence-dossier.png")});
  await drawer.getByRole("tab",{name:"历史类似反证",exact:true}).click();
  await expect(drawer.getByText("本页没有符合条件的材料；不表示不存在此类证据")).toBeVisible();
  await drawer.getByRole("button",{name:"下一页证据"}).click();
  await expect(drawer.getByRole("button",{name:"下一页证据"})).toBeDisabled();
  expect(requests.at(-1)?.searchParams.get("input_sha256")).toBe("a".repeat(64));
  await drawer.getByTitle("POY",{exact:true}).click();
  // 市场页自身的品种分段控件也叫 PTA：下拉选项必须限定在下拉浮层内。
  await page.locator(".ant-select-dropdown").getByTitle("PTA",{exact:true}).click();
  await expect.poll(()=>requests.at(-1)?.searchParams.get("target")).toBe("pta");
  await drawer.getByTitle("当前资料",{exact:true}).click();
  await page.locator(".ant-select-dropdown").getByTitle("最近发行时证据",{exact:true}).click();
  await expect(drawer.getByText("该批次早于统一证据档案，不回写历史")).toBeVisible();
  await drawer.getByRole("button",{name:"Close",exact:true}).click();
  await expect(drawer).toBeHidden();
});

test("evidence endpoint failure is not rendered as zero healthy evidence",async ({page})=>{
  await page.route("**/api/v1/forecasts/seven-product/evidence?**",route=>route.fulfill({status:503,json:{detail:{code:"evidence_dossier_unavailable"}}}));
  await page.goto("/?module=market");
  await page.getByRole("button",{name:"品种正反证"}).first().click();
  await expect(page.getByRole("dialog").getByText("证据暂不可用",{exact:true})).toBeVisible();
});

test("stale pagination pin returns to page 1 of the new snapshot with a notice", async ({page}) => {
  let calls = 0;
  await page.route("**/api/v1/forecasts/seven-product/evidence?**", async route => {
    const url = new URL(route.request().url());
    calls += 1;
    if (calls === 1) return route.fulfill({json: payload(url.searchParams.get("target")!)});
    // 第二页：pin 已过期 → 422 + 机器可读 code
    if (url.searchParams.get("offset") === "50") {
      return route.fulfill({status: 422, json: {error: {code: "evidence_view_changed_reload_first_page", message: "stale"}}});
    }
    // 自动恢复：无 pin 的第 1 页新快照（新 sha）
    const data = payload(url.searchParams.get("target")!);
    return route.fulfill({json: {...data, input_sha256: "b".repeat(64), as_of_time: "2026-09-28T18:00:00Z"}});
  });
  await page.goto("/?module=market");
  await page.getByRole("button", {name: "品种正反证"}).first().click();
  const drawer = page.getByRole("dialog");
  await drawer.getByRole("button", {name: "下一页证据"}).click();
  await expect(drawer.getByText(/资料已更新.*回到第 1 页/)).toBeVisible({timeout: 10_000});
  // 恢复请求必须不带旧 pin 且回到 offset 0
  await expect.poll(() => calls).toBe(3);
});
