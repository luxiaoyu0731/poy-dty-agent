export const MARKET_DAY_MS = 86_400_000;
export type MarketWindow = "30" | "90" | "all";

// Dates are observation days, not browser-local instants. UTC keeps a day a
// constant width across time zones and daylight-saving transitions.
export function marketDay(date: string): number {
  return Date.parse(`${date.slice(0, 10)}T00:00:00Z`);
}

export function marketTimeline(data: { date: string; value: number }[], window: MarketWindow) {
  const values = new Map(data.filter(p => Number.isFinite(p.value) && Number.isFinite(marketDay(p.date)))
    .map(p => [marketDay(p.date), p.value]));
  const dates = [...values.keys()].sort((a, b) => a - b);
  if (!dates.length) return null;
  const last = dates[dates.length - 1];
  const first = window === "all" ? dates[0] : last - Number(window) * MARKET_DAY_MS;
  const rows: { date: string; timestamp: number; value: number | null }[] = [];
  for (let day = first; day <= last; day += MARKET_DAY_MS) {
    rows.push({ date: new Date(day).toISOString().slice(0, 10), timestamp: day, value: values.get(day) ?? null });
  }
  // A singleton still needs a visible point and a non-zero axis domain.
  const domain: [number, number] = first === last
    ? [first - MARKET_DAY_MS, last + MARKET_DAY_MS] : [first, last];
  const steps = Math.min(6, Math.round((domain[1] - domain[0]) / MARKET_DAY_MS));
  const ticks = [...new Set(Array.from({ length: steps + 1 }, (_, i) =>
    domain[0] + Math.round((domain[1] - domain[0]) / MARKET_DAY_MS * i / steps) * MARKET_DAY_MS))];
  return { rows, domain, ticks };
}

export function marketSeriesLabel(basis?: { series_id?: string; quote_type?: string }) {
  const identity = `${basis?.series_id ?? ""} ${basis?.quote_type ?? ""}`.toLowerCase();
  if (/settlement|futures_settlement/.test(identity)) return "期货结算价";
  if (/futures|exchange_proxy/.test(identity)) return "期货代理价";
  if (/assessment|valuation/.test(identity)) return "现货评估价";
  if (/spot|daily_average/.test(identity)) return "现货报价";
  return "历史报价";
}
