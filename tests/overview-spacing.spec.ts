import {test, expect} from '@playwright/test';

for (const viewport of [{width:1710,height:922},{width:1280,height:720}]) {
  test(`overview shares space and radar filters stay separated at ${viewport.width}`, async ({page}) => {
    await page.setViewportSize(viewport);
    await page.goto('/?module=overview');
    const decision=page.locator('.overview-decision');
    const risk=page.locator('.overview-risk');
    const feed=page.locator('.review-overview-feed');
    await expect(decision).toBeVisible();
    const [d,r,f]=await Promise.all([decision.boundingBox(),risk.boundingBox(),feed.boundingBox()]);
    expect(d!.width).toBeGreaterThan(r!.width+f!.width);
    expect(Math.abs(r!.y-f!.y)).toBeLessThan(2);
    expect(r!.y).toBeGreaterThanOrEqual(d!.y+d!.height);
    expect(r!.height).toBeGreaterThan(250);
    expect(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight+1)).toBe(true);
    await page.screenshot({path:`agent-context/overview-spacing-20260919/overview-${viewport.width}.png`});
    await page.route('**/api/v1/intelligence/events?**',route=>route.fulfill({json:{items:Array.from({length:20},(_,i)=>({event_id:`e${i}`,event_revision_id:`r${i}`,title:`原油与聚酯供应事件 ${i}`,category:'energy',product_ids:['crude'],last_seen_at:'2026-09-19T10:00:00Z',confidence:.8,relevance_score:80,evidence_count:1})),has_more:false}}));
    await page.goto('/?module=intelligence&intelligenceView=radar');
    const filters=page.locator('.radar-filters');
    await expect(page.locator('.radar-event-card')).toHaveCount(20);
    const before=await filters.boundingBox();
    const card=await page.locator('.radar-event-card').first().boundingBox();
    expect(card!.y-before!.y-before!.height).toBeGreaterThanOrEqual(10);
    await page.locator('.radar-results').evaluate(el=>{el.scrollTop=400;});
    expect(await page.locator('.radar-results').evaluate(el=>el.scrollTop)).toBeGreaterThan(0);
    // v34 雷达布局中筛选区与列表同区滚动；不变量 = 筛选区仍可见且未被滚出。
    expect((await filters.boundingBox())!.y).toBeGreaterThan(0);
    expect(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight+1)).toBe(true);
    await page.locator('.radar-results').evaluate(el=>{el.scrollTop=0;});
    await page.screenshot({path:`agent-context/overview-spacing-20260919/radar-${viewport.width}.png`});
  });
}
