import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 1710, height: 922 } });

test('accepted modules fit viewport with no review-only artifacts', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  for (const module of ['overview', 'market', 'evidence', 'workflow', 'assistant', 'reports', 'intelligence']) {
    await page.goto(`/?module=${module}`);
    await expect(page.locator('.delivery-sidebar')).toBeVisible();
    await expect(page.locator(module === 'intelligence' ? '.intelligence-center' : '.delivery-page')).toBeVisible();
    await expect(page.locator('#layout-review-badge,.delivery-header')).toHaveCount(0);
    expect(await page.evaluate(() => ({height: document.documentElement.scrollHeight <= innerHeight + 1, width: document.documentElement.scrollWidth <= innerWidth + 1}))).toEqual({height:true,width:true});
    await page.screenshot({ path: `agent-context/layout-public-release-20260919/${module}.png` });
  }
  expect(errors).toEqual([]);
});

test('market uses live product selection, contained tabs and reversible chart expansion', async ({page}) => {
  await page.goto('/?module=market');
  const chain=page.locator('.review-material-flow');
  await expect(chain.getByRole('button')).toHaveCount(7);
  await chain.getByRole('button',{name:/MEG/}).click();
  await expect(page.locator('.trend-panel .delivery-panel-head')).toContainText('MEG 价格趋势');
  await page.locator('.trend-panel').getByRole('button',{name:'放大',exact:true}).click();
  await expect(page.locator('.trend-panel')).toHaveClass(/review-focus/);
  await page.keyboard.press('Escape');
  await expect(page.locator('.trend-panel')).not.toHaveClass(/review-focus/);
  await page.getByRole('tab',{name:'品种解读',exact:true}).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'近期报价',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.locator('.public-quote-history')).toBeVisible();
});

test('evidence summary stays fully visible and workflow controls share header', async ({page}) => {
  await page.goto('/?module=evidence');
  const summary=page.locator('#evb-verdict');
  await expect(summary).toBeVisible();
  const panels=summary.locator(':scope > article,:scope > div,:scope > aside');
  expect(await panels.evaluateAll(elements=>elements.every(e=>e.scrollHeight <= e.clientHeight+1))).toBe(true);
  await page.goto('/?module=workflow');
  await expect(page.locator('.workflow-status-strip')).toHaveCount(0);
  const head=page.locator('.workflow-board-panel>.delivery-panel-head');
  await expect(head).toContainText('节点总数');
  await expect(head.getByRole('button',{name:'刷新状态'})).toBeVisible();
  await head.getByRole('button',{name:'刷新状态'}).click();
  // 解剖画布（2026-10-02）：后端 18 节点 + 前端规划节点「统一记忆舱」= 19。
  await expect(page.locator('.pipeline-flow-node')).toHaveCount(19);
  await expect(head.getByRole('button',{name:'放大'})).toBeVisible();
});

test('report controls remain in title bar and ledger retains return route',async ({page})=>{
  await page.goto('/?module=reports');
  const head=page.locator('[data-testid=information-reports]>.ant-card-head');
  await expect(head.locator('.report-workspace-switch')).toBeVisible();
  await head.getByText('周报',{exact:true}).click();
  await expect(head).toContainText('市场与产业研判周报');
  await expect(page.getByText('先看结论，再核对七品种传导、风险反证与来源。报告中的观察判断与正式预测分别核验。',{exact:true})).toHaveCount(0);
  await head.getByText('预测账本与到期复盘',{exact:true}).click();
  await expect(page).toHaveURL(/reportView=ledger/);
  await page.locator('.report-workspace-switch').getByText('研判报告',{exact:true}).click();
  await expect(head).toBeVisible();
  await expect(page.getByRole('button',{name:'生成信息周报',exact:true})).toBeEnabled();
});

test('compact desktop footer keeps the live refresh control labelled',async ({page})=>{
  await page.setViewportSize({width:1280,height:720});
  await page.goto('/?module=market');
  await expect(page.locator('.review-sidebar-footer').getByRole('button',{name:'刷新数据'})).toBeVisible();
});
