import { expect, test } from '@playwright/test';

for (const module of ['market', 'workflow', 'assistant', 'reports', 'evidence']) {
  test(`${module}: module reads start before the auxiliary workbench is ready`, async ({ page }) => {
    let release!: () => void;
    const auxiliaryGate = new Promise<void>(resolve => { release = resolve; });
    let fullChainCalls = 0;
    let auxiliaryCalls = 0;
    // Missing daily snapshot is a supported cold-boot path. It resolves first,
    // while the independent delivery bundle remains blocked below.
    await page.route('**/api/v1/workbench/snapshot', route =>
      route.fulfill({ status: 404, json: { detail: "No daily snapshot" } }));
    await page.route('**/api/v1/delivery/status', async route => {
      auxiliaryCalls += 1;
      await auxiliaryGate;
      await route.continue();
    });
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/v1/full-chain/summary') fullChainCalls += 1;
    });
    try {
      await page.goto(`/?module=${module}`);
      await expect.poll(() => auxiliaryCalls).toBeGreaterThan(0);
      // Deterministic ordering assertion: no artificial sleep or production
      // latency claim. The delivery response is still held by our gate.
      await expect.poll(() => fullChainCalls, { timeout: 3000 }).toBe(1);
    } finally {
      release();
      await page.unrouteAll({ behavior: 'wait' });
    }
  });
}

test('market: canonical snapshot resolves before live module reads', async ({ page }) => {
  let release!: () => void;
  const snapshotGate = new Promise<void>(resolve => { release = resolve; });
  let snapshotCalls = 0;
  let fullChainCalls = 0;
  await page.route('**/api/v1/workbench/snapshot', async route => {
    snapshotCalls += 1;
    await snapshotGate;
    await route.fulfill({ status: 404, json: { detail: "No daily snapshot" } });
  });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/v1/full-chain/summary') fullChainCalls += 1;
  });
  try {
    await page.goto('/?module=market');
    await expect.poll(() => snapshotCalls).toBe(1);
    expect(fullChainCalls).toBe(0);
    release();
    await expect.poll(() => fullChainCalls).toBeGreaterThan(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: 'wait' });
  }
});
