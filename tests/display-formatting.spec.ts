import { expect, test } from "@playwright/test";
import {
  formatMarketFreshnessSummary,
  formatDisplayDate,
  formatDisplayTimestamp,
  formatTimestampsInText,
  formatDisplayDateRange,
  formatDateRangesInText,
  formatFreshnessLabel,
  formatMetricFreshness,
  formatMetricLabel
} from "../src/utils/displayFormatting";

test("known metric fields use consistent Chinese labels", () => {
  expect(formatMetricLabel("price")).toBe("价格");
  expect(formatMetricLabel("inventory")).toBe("库存");
  expect(formatMetricLabel("operating")).toBe("开工率");
  expect(formatMetricLabel("profit")).toBe("利润");
  expect(formatMetricLabel("operating_margin")).toBe("operating_margin");
});

test("freshness labels only translate known structured statuses", () => {
  expect(formatFreshnessLabel("stale")).toBe("数据滞后");
  expect(formatFreshnessLabel(" near_realtime ")).toBe("近实时");
  expect(formatFreshnessLabel("price is stale")).toBe("price is stale");
  expect(formatFreshnessLabel(undefined)).toBe("等待更新");
});

test("dates and ranges use stable ISO calendar labels", () => {
  expect(formatDisplayTimestamp("2026-09-04")).toBe("2026-09-04");
  expect(formatDisplayTimestamp("2026-09-04T08:15:00Z")).toBe("2026-09-04 16:15");
  expect(formatDisplayDate("2026-07-10")).toBe("2026-07-10");
  expect(formatDisplayDate("2026-07-09T16:30:00.000Z")).toBe("2026-07-10");
  expect(formatDisplayDate("not-a-date")).toBe("暂无日期");
  expect(formatDisplayDateRange("2026-07-10", "2026-07-24")).toBe(
    "2026-07-10 至 2026-07-24"
  );
  expect(formatDisplayDateRange("2026-07-10", null)).toBe("暂无区间");
});

test("machine-shaped date ranges are normalized without changing business figures", () => {
  expect(formatDateRangesInText("Brent +32.60%（2026-07-10..2026-07-24）。。")).toBe(
    "Brent +32.60%（2026-07-10 至 2026-07-24）。"
  );
});

test("prose timestamps use Shanghai minutes across offsets and midnight", () => {
  expect(formatTimestampsInText("生成时间：2026-09-20T10:05:13.936321+00:00（UTC）"))
    .toBe("生成时间：2026-09-20 18:05（上海）");
  expect(formatTimestampsInText("窗口 2026-09-19T10:05:13.936321Z 至 2026-09-20T10:05:13.936321Z（UTC）"))
    .toBe("窗口 2026-09-19 18:05 至 2026-09-20 18:05（上海）");
  expect(formatTimestampsInText("截至 2026-09-26T18:05:55.749835+00:00"))
    .toBe("截至 2026-09-27 02:05");
  expect(formatDisplayTimestamp("2026-09-25T16:00:59Z")).toBe("2026-09-26 00:00");
  expect(formatDisplayTimestamp("2026-09-26T08:05:55.123456+08:00")).toBe("2026-09-26 08:05");
  expect(formatDisplayTimestamp("2026-09-25T20:05:55-04:00")).toBe("2026-09-26 08:05");
});

test("date cleanup preserves evidence links, identifiers, figures and unknown dates", () => {
  const unchanged = "https://example.com/?as_of=2026-09-26T00:05:55.123456Z `2026-09-26T00:05:55.123456Z` seven-be101171394915d295ef060e 9,342.50元 49.0% 2026-09-26 2026-13-26T00:00:00Z";
  expect(formatTimestampsInText(unchanged)).toBe(unchanged);
  expect(formatTimestampsInText(undefined)).toBe("");
  expect(formatDisplayTimestamp("待发布")).toBeUndefined();
});

test("metric freshness remains structured for compact status rows", () => {
  expect(formatMetricFreshness({
    field: "inventory",
    date: "2026-07-03",
    freshness: "stale"
  })).toEqual({
    label: "库存",
    date: "2026-07-03",
    status: "数据滞后"
  });
});


test("current quote freshness overrides historical price only", () => {
  const state = { status: "stale", categories: {
    price: { status: "stale" }, inventory: { status: "stale" },
    operating: { status: "stale" }, profit: { status: "stale" }
  } };
  expect(formatMarketFreshnessSummary(state, false)).toBe("库存、开工率、加工差数据滞后");
  expect(formatMarketFreshnessSummary(state, true)).toBe("价格、库存、开工率、加工差数据滞后");
  expect(formatMarketFreshnessSummary({status: "fresh", categories: {price: {status: "fresh"}}}, true)).toBe("价格数据滞后");
});
