import { test, expect } from '@playwright/test';

const modules = ['overview', 'market', 'evidence', 'workflow', 'assistant', 'reports', 'intelligence'];

test.describe('phone desktop replica', () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 844 } });

  test('all modules retain desktop dimensions and native pinch zoom', async ({ page, context }) => {
    for (const module of modules) {
      await page.goto(`/?module=${module}`);
      await expect(page.locator('.delivery-workbench')).toBeVisible();
      await expect(page.locator('html')).toHaveClass(/mobile-desktop-view/);
      const frame = await page.locator('.delivery-workbench').boundingBox();
      expect(frame?.width).toBeCloseTo(1440, 1);
      expect(frame?.height).toBeCloseTo(900, 1);
      expect(await page.evaluate(() => window.visualViewport!.scale)).toBeCloseTo(390 / 1440, 2);
      await expect(page.getByRole('button', { name: '恢复全图' })).toHaveCount(0);
      if (module === 'overview') {
        await expect(page.locator('#main-content')).toHaveAttribute('aria-busy', 'false', { timeout: 60_000 });
        await expect(page.locator('.metric-grid')).toBeVisible({ timeout: 60_000 });
        await page.screenshot({ path: test.info().outputPath('phone-overview.png') });
      }
    }
    await page.goto('/?module=overview');
    await expect(page.locator('.delivery-workbench')).toBeVisible();
    const session = await context.newCDPSession(page);
    const before = await page.evaluate(() => window.visualViewport!.scale);
    await session.send('Input.synthesizePinchGesture', { x: 190, y: 400, scaleFactor: 2, gestureSourceType: 'touch' });
    await expect.poll(() => page.evaluate(() => window.visualViewport!.scale)).toBeGreaterThan(before * 1.5);
    expect((await page.locator('.delivery-workbench').boundingBox())?.width).toBeCloseTo(1440, 1);
    await page.locator('.delivery-sidebar').getByText('Agent 系统', { exact: true }).click();
    await expect(page.locator('.pipeline-flow-node')).toHaveCount(19);
    await page.locator('.pipeline-flow-node').filter({ hasText: '政局解读' }).click();
    await expect(page.locator('.ant-drawer-open .ant-drawer-content')).toBeVisible();
    const drawer = await page.locator('.ant-drawer-open .ant-drawer-content').boundingBox();
    expect(drawer!.y + drawer!.height).toBeLessThanOrEqual(900);
  });
});

test('desktop viewport and layout remain unchanged', async ({ page }) => {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1920, height: 1080 }]) {
    await page.setViewportSize(viewport);
    await page.goto('/?module=overview');
    await expect(page.locator('.delivery-workbench')).toBeVisible();
    await expect(page.locator('html')).not.toHaveClass(/mobile-desktop-view/);
    await expect(page.locator('meta[name="viewport"]')).toHaveAttribute('content', 'width=device-width, initial-scale=1.0');
    const frame = await page.locator('.delivery-workbench').boundingBox();
    expect(frame?.width).toBe(viewport.width);
    expect(frame?.height).toBe(viewport.height);
  }
});
