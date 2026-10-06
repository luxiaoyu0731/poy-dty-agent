import { expect, test } from "@playwright/test";

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

for (const unavailable of [false, true]) {
  test(`one current forecast authority; unavailable=${unavailable}`, async ({page}) => {
    let legacyCalls = 0;
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.startsWith("/api/v1/auth/")) return route.continue();
      if (path.endsWith("/predictions/model-signal")) legacyCalls++;
      if (path.endsWith("/forecasts/seven-product") && !unavailable) return route.fulfill({json: batch});
      if (path.endsWith("/delivery/status")) {
        const response = await route.fetch();
        const payload = await response.json();
        payload.client_reports = [{id: "conflicting", title: "旧成本方向", direction: "偏强",
          summary: "不应替代主预测的旧方向", decision_status: "observation_only", formal_report_eligible: false}];
        return route.fulfill({json: payload});
      }
      return route.fulfill({status: 503, json: {error: "unavailable"}});
    });
    await page.goto("/?module=overview");
    const decision = page.locator(".overview-decision");
    await expect(decision).toBeVisible();
    if (unavailable) {
      await expect(decision).toContainText("当前证据不足");
      await expect(decision).not.toContainText(batch.batch_id);
    } else {
      await expect(decision).toContainText("POY：偏强；DTY：偏弱");
      await expect(decision.locator(".decision-lead")).not.toContainText(batch.batch_id);
      await expect(decision.locator(".decision-lead")).toContainText("2026-09-26 08:00（上海）");
      await expect(decision.locator("code").filter({hasText: batch.batch_id})).toBeHidden();
      await decision.getByText("展开模型与计算详情", {exact: true}).click();
      await expect(decision.locator("code").filter({hasText: batch.batch_id})).toBeVisible();
      await expect(page.locator(".delivery-metric").filter({hasText: "POY/DTY 价格方向"})).toContainText("分化");
      await expect(page.getByText("参考评分（非正确率）", {exact: true})).toBeVisible();
      await page.getByRole("link", {name: "证据图谱", exact: true}).click();
      // 本用例其余接口 503：核验台必须显式呈现不可用与重试（不伪造判断区）；
      // 聚合"分化"已在指标卡断言。
      const board = page.locator("[data-testid='evidence-verification-board']");
      await expect(board).toBeVisible({ timeout: 30_000 });
      await expect(board.getByText("证据暂不可用")).toBeVisible({ timeout: 30_000 });
      await page.getByRole("link", {name: "研判报告", exact: true}).click();
      await expect(page.locator(".delivery-workbench")).not.toContainText("不应替代主预测的旧方向");
    }
    expect(legacyCalls).toBe(0);
    await expect(page.locator(".delivery-workbench")).not.toContainText("14 日观察窗口");
  });
}
