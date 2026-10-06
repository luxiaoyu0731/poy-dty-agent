import { expect, test, type Page } from "@playwright/test";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function openMarket(page: Page) {
  await page.goto("/?module=market");
  await expect(page.locator(".delivery-workbench")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".market-layout")).toBeVisible({ timeout: 60_000 });
}

test("PX uses a unified yuan unit without merging the futures proxy into the spot curve", async ({ page }) => {
  const generatedAt = new Date().toISOString();
  const items = [
    {
      instrument: "POY",
      label: "POY",
      freshness: "near_realtime",
      freshness_label: "最新观测",
      quote_type_label: "日报行情",
      latest: {
        observation_id: "poy-cny",
        observed_at: generatedAt,
        last: 8_368.75,
        unit: "CNY/mt"
      }
    },
    {
      instrument: "PX_CFR",
      label: "PX CFR 中国（历史参考）",
      freshness: "near_realtime",
      freshness_label: "历史参考",
      quote_type_label: "进口现货参考",
      latest: {
        observation_id: "px-usd",
        observed_at: "2026-07-10T08:00:00Z",
        last: 1_017,
        unit: "USD/mt"
      }
    },
    {
      instrument: "PX",
      label: "PX0",
      freshness: "near_realtime",
      freshness_label: "最新观测",
      quote_type_label: "期货行情",
      latest: {
        observation_id: "px-cny",
        observed_at: generatedAt,
        last: 8_514,
        unit: "CNY/mt"
      }
    },
    {
      instrument: "PTA",
      label: "PTA",
      freshness: "near_realtime",
      freshness_label: "最新观测",
      quote_type_label: "日报行情",
      latest: {
        observation_id: "pta-cny",
        observed_at: generatedAt,
        last: 5_920,
        unit: "CNY/mt"
      }
    }
  ];
  const patchPxView = (products: Array<Record<string, any>> | undefined) => {
    const pxView = products?.find((item) => item.key === "PX");
    if (!pxView) return;
    pxView.price_series = [
      {
        date: "2026-07-09",
        value: 6_790,
        unit: "CNY/mt",
        original_value: 1_000,
        original_unit: "USD/mt",
        fx_rate: 6.79,
        fx_date: "2026-07-09",
        trend_eligible: true,
        comparison_basis: {
          product: "PX",
          market: "CFR中国",
          spec: "PX CFR中国",
          quote_type: "daily_average",
          source_basis: "ccf_dom_daily",
          unit: "CNY/mt",
          original_unit: "USD/mt",
          conversion_method: "usd_cny_daily"
        }
      },
      {
        date: "2026-07-10",
        value: 6_893.04,
        unit: "CNY/mt",
        original_value: 1_017,
        original_unit: "USD/mt",
        fx_rate: 6.7788,
        fx_date: "2026-07-10",
        trend_eligible: true,
        comparison_basis: {
          product: "PX",
          market: "CFR中国",
          spec: "PX CFR中国",
          quote_type: "daily_average",
          source_basis: "ccf_dom_daily",
          unit: "CNY/mt",
          original_unit: "USD/mt",
          conversion_method: "usd_cny_daily"
        }
      }
    ];
    pxView.latest_price = {
      ...pxView.latest_price,
      status: "available",
      metric_label: "PX CFR 中国现货",
      date: "2026-07-10",
      value: 6_893.04,
      unit: "CNY/mt"
    };
    pxView.latest_display_price = {
      status: "available",
      metric_label: "PX 最新价格",
      quality_label: "最新观测",
      date: generatedAt.slice(0, 10),
      observed_at: generatedAt,
      value: 8_514,
      unit: "CNY/mt",
      points: 1,
      detail: "期货代理价与现货曲线分开展示。",
      price_type: "exchange_proxy",
      source_id: "sina_futures_realtime",
      observation_id: "px-cny",
      trend_eligible: false
    };
    pxView.data_coverage = { ...pxView.data_coverage, price_points: 2, price_days: 2 };
    pxView.inventory_summary = {
      ...pxView.inventory_summary,
      status: "available",
      metric_label: "下游库存天数",
      value: 22.3,
      unit: "天"
    };
    pxView.operating_summary = {
      ...pxView.operating_summary,
      status: "available",
      metric_label: "聚酯开工率",
      value: 80.1,
      unit: "%"
    };
    pxView.profit_summary = {
      ...pxView.profit_summary,
      status: "available",
      metric_label: "聚酯利润",
      value: 1510,
      unit: "CNY/mt"
    };
  };
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    if (payload?.workbench?.market?.latest_prices) {
      payload.workbench.market.latest_prices = {
        generated_at: generatedAt,
        policy_note: "PX contract regression",
        status_counts: { near_realtime: items.length },
        items
      };
    }
    patchPxView(payload?.workbench?.market?.chain?.products);
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/api/v1/workbench/market-chain**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    patchPxView(payload?.products);
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/api/v1/prices/latest**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: generatedAt,
        policy_note: "PX contract regression",
        status_counts: { near_realtime: items.length },
        items
      })
    });
  });

  await openMarket(page);
  await page.locator(".review-material-price").filter({ hasText: /^PX/ }).click();

  const trend = page.locator(".trend-panel");
  await expect(trend).toContainText("8,514 元/吨");
  await expect(trend).not.toContainText("8,368.75 元/吨");
  await expect(trend).not.toContainText("5,920 元/吨");
  await expect(page.locator(".review-material-price").filter({ hasText: /^PX/ })).toContainText("8,514 元/吨");
  // v34：副标题不再渲染；口径分离不变量 = 价格摘要同时展示当前代理价与
  // 历史现货曲线两个来源/截止，不合并成单一序列。
  const priceCard = page.locator(".market-summary-grid").filter({ hasText: "价格" }).first();
  await expect(priceCard).toContainText("期货代理价", { timeout: 15_000 });
  await expect(priceCard).toContainText("现货报价曲线来源");
  await expect(page.getByTestId("market-trend-chart")).toHaveAttribute("data-current-merged", "false");
  // v34：徽标退役；口径分离不变量 = 价格摘要卡标注历史曲线来源与截至 2026-07-10。
  await expect(priceCard).toContainText("截至 2026-07-10");
  // v34：副标题不渲染；口径分离由价格摘要卡与"不同报价基准分开观察"信号承担。
  await expect(trend).toContainText("不同报价基准分开观察");
  await expect(trend).not.toContainText("美元/吨历史现货");
  await expect(page.locator(".product-explain")).not.toContainText("同口径历史序列");
  // v34：下游口径移至加工差摘要卡（聚酯链加工差参考，不代表 PX 自身利润）。
  const profitCard = page.locator(".market-summary-grid .market-small-panel").filter({ hasText: "加工差" });
  await expect(profitCard).toContainText("聚酯链加工差参考");
  await expect(profitCard).toContainText("不代表 PX 自身利润");

  await page.locator(".review-material-price").filter({ hasText: /^石脑油/ }).click();
  await expect(page.locator(".trend-panel")).toContainText("石脑油 价格趋势（元/吨）", { timeout: 15_000 });
  // 本用例 mock 未提供石脑油价格：链路节点如实显示"未返回"（元/吨已在趋势标题断言）。
  await expect(page.locator(".review-material-price").filter({ hasText: /^石脑油/ })).toContainText(/元\/吨|未返回/);
});

test("stale market fields are presented as structured Chinese statuses", async ({ page }) => {
  const markProductStale = (products: Array<Record<string, any>> | undefined) => {
    if (!Array.isArray(products) || !products[0]) return;
    for (const key of ["latest_price", "inventory_summary", "operating_summary", "profit_summary"]) {
      if (products[0][key]) products[0][key].date = "2026-01-01";
    }
    const categories = products[0].data_freshness?.categories;
    if (categories) {
      for (const key of ["price", "inventory", "operating", "profit"]) {
        if (categories[key]) {
          categories[key].status = "stale";
          categories[key].latest_date = "2026-01-01";
        }
      }
    }
    if (products[0].latest_display_freshness) {
      products[0].latest_display_freshness.status = "stale";
      products[0].latest_display_freshness.latest_date = "2026-01-01";
    }
  };
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    markProductStale(payload?.workbench?.market?.chain?.products);
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/api/v1/workbench/market-chain**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    markProductStale(payload?.products);
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/api/v1/prices/latest**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const poy = payload?.items?.find((item: { instrument?: string }) => item.instrument === "POY");
    if (poy) {
      poy.freshness = "stale";
      poy.freshness_label = "数据滞后";
      if (poy.latest) poy.latest.observed_at = "2026-01-01T00:00:00Z";
    }
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await openMarket(page);

  const freshness = page.locator(".product-freshness-row");
  await expect(freshness).toBeVisible();
  await expect(freshness).toContainText(/价格|库存|开工|利润/);
  await expect(freshness).toContainText("数据滞后");
  await expect(freshness).not.toContainText(/\bstale\b/i);
  await expect(freshness).not.toContainText(/\b(?:price|inventory|operating|profit)\b/i);

  const explanation = page.locator(".product-explain");
  await expect(explanation).not.toContainText(/\(stale\)|price至|inventory至|operating至|profit至/i);

  const cards = page.locator(".market-summary-grid");
  await expect(cards.locator(".summary-card i")).toHaveCount(0);
  await expect(cards.getByText("数据滞后", { exact: true })).toHaveCount(2); // v34：价格 + 加工差
});

test("market chart and snapshot cards do not overlap or clip at the reported desktop size", async ({ page }) => {
  await page.setViewportSize({ width: 1728, height: 768 });
  await openMarket(page);

  const panel = page.locator(".trend-panel");
  const body = panel.locator(".delivery-panel-body");
  const chart = panel.locator(".chart-shell");
  const snapshots = panel.locator(".price-snapshot-grid");
  await expect(chart).toBeVisible();
  await expect(snapshots).toBeVisible();
  const lastDate = await page.getByTestId("market-trend-chart").getAttribute("data-last-date");
  if (lastDate) {
    await expect(chart.locator(".recharts-xAxis .recharts-cartesian-axis-tick-value").last())
      .toHaveText(lastDate.slice(5));
  }

  const boxes = await Promise.all([
    body.boundingBox(),
    chart.boundingBox(),
    snapshots.boundingBox()
  ]);
  const [bodyBox, chartBox, snapshotsBox] = boxes;
  expect(bodyBox).not.toBeNull();
  expect(chartBox).not.toBeNull();
  expect(snapshotsBox).not.toBeNull();
  if (!bodyBox || !chartBox || !snapshotsBox) return;

  expect(chartBox.y + chartBox.height).toBeLessThanOrEqual(snapshotsBox.y + 1);
  expect(snapshotsBox.y + snapshotsBox.height).toBeLessThanOrEqual(bodyBox.y + bodyBox.height + 1);
  expect(snapshotsBox.x).toBeGreaterThanOrEqual(bodyBox.x - 1);
  expect(snapshotsBox.x + snapshotsBox.width).toBeLessThanOrEqual(bodyBox.x + bodyBox.width + 1);
});

test("soft removed inventory and operating cards never claim current updates", async ({ page }) => {
  const patch = (products: any[]) => {
    const poy = products?.find((item) => item.product === "POY");
    if (!poy) return;
    for (const category of ["inventory", "operating"]) {
      poy.data_freshness.categories[category].status = "soft_removed";
    }
  };
  await page.route("**/api/v1/workbench/snapshot**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    patch(payload?.workbench?.market?.chain?.products);
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/api/v1/workbench/market-chain**", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    patch(payload?.products);
    await route.fulfill({ response, json: payload });
  });
  await openMarket(page);
  // v34：摘要卡为 价格/加工差 两张；软移除的库存/开工不再有专属卡片，
  // 不变量 = 残余卡片不得以库存/开口径宣称"已更新"。
  const cards = page.locator(".market-summary-grid .market-small-panel");
  await expect(cards).toHaveCount(2);
  for (const title of ["库存", "开工"]) {
    await expect(cards.filter({ has: page.locator(".delivery-panel-head strong", { hasText: title }) })).toHaveCount(0);
  }
});
