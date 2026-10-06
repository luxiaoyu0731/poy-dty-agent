import type { IndustryObservation, LatestPriceItem, LatestPricesResponse, MarketObservation } from "../services/api";

export type ChainNodeName = "原油" | "石脑油" | "PX" | "PTA" | "MEG" | "POY" | "DTY";

export type ChainLatestValue = {
  value: string;
  date: string;
  detail: string;
  freshnessLabel?: string;
  quoteTypeLabel?: string;
  isTransactionPrice?: boolean;
  change?: {
    label: string;
    tone: "up" | "down" | "flat";
  };
};

function formatUnit(unit?: string) {
  if (!unit) return "";
  if (unit === "dollars_per_barrel" || unit === "$/BBL") return "$/bbl";
  if (unit === "USD/bbl") return "$/bbl";
  if (unit === "CNY/mt") return "元/t";
  if (unit === "元/吨") return "元/t";
  if (unit === "美元/吨") return "$/t";
  return unit;
}

function formatValue(value?: number | null, unit?: string) {
  if (value === undefined || value === null || Number.isNaN(value)) return "";
  const formatted = Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const readableUnit = formatUnit(unit);
  return readableUnit ? `${formatted} ${readableUnit}` : formatted;
}

function latestBy<T extends { observed_at: string; value?: number | null }>(items: T[], predicate: (item: T) => boolean) {
  return latestItems(items, predicate)[0];
}

function latestItems<T extends { observed_at: string; value?: number | null }>(items: T[], predicate: (item: T) => boolean) {
  return items
    .filter((item) => predicate(item) && item.value !== undefined && item.value !== null)
    .sort((left, right) => right.observed_at.localeCompare(left.observed_at));
}

function latestIndustryForNode(node: ChainNodeName, items: IndustryObservation[]) {
  const productAliases: Record<ChainNodeName, string[]> = {
    原油: [],
    石脑油: ["石脑油", "NAPHTHA"],
    PX: ["PX"],
    PTA: ["PTA"],
    MEG: ["MEG"],
    POY: ["POY"],
    DTY: ["DTY"]
  };
  const aliases = productAliases[node].map((item) => item.toUpperCase());
  const matchesNode = (item: IndustryObservation) => aliases.includes(item.product.toUpperCase());
  return (
    latestBy(items, (item) => matchesNode(item) && item.metric === "spot_quote") ??
    latestBy(items, (item) => matchesNode(item))
  );
}

function latestIndustrySeriesForNode(node: ChainNodeName, items: IndustryObservation[]) {
  const productAliases: Record<ChainNodeName, string[]> = {
    原油: [],
    石脑油: ["石脑油", "NAPHTHA"],
    PX: ["PX"],
    PTA: ["PTA"],
    MEG: ["MEG"],
    POY: ["POY"],
    DTY: ["DTY"]
  };
  const aliases = productAliases[node].map((item) => item.toUpperCase());
  const matchesNode = (item: IndustryObservation) => aliases.includes(item.product.toUpperCase());
  const spot = latestItems(items, (item) => matchesNode(item) && item.metric === "spot_quote");
  return spot.length ? spot : latestItems(items, (item) => matchesNode(item));
}

function percentChange(
  latest?: { value?: number | null },
  previous?: { value?: number | null }
): ChainLatestValue["change"] | undefined {
  if (
    latest?.value === undefined ||
    latest.value === null ||
    previous?.value === undefined ||
    previous.value === null ||
    previous.value === 0
  ) {
    return undefined;
  }
  const change = ((latest.value - previous.value) / Math.abs(previous.value)) * 100;
  const abs = Math.abs(change);
  if (abs < 0.005) {
    return { label: "0.00%", tone: "flat" };
  }
  return {
    label: `${change > 0 ? "+" : "-"}${abs.toFixed(2)}%`,
    tone: change > 0 ? "up" : "down"
  };
}

function percentChangeFromNumber(value?: number | null): ChainLatestValue["change"] | undefined {
  if (value === undefined || value === null || Number.isNaN(value)) return undefined;
  const abs = Math.abs(value);
  if (abs < 0.005) return { label: "0.00%", tone: "flat" };
  return {
    label: `${value > 0 ? "+" : "-"}${abs.toFixed(2)}%`,
    tone: value > 0 ? "up" : "down"
  };
}

function latestPriceMap(latestPrices?: LatestPricesResponse) {
  return new Map((latestPrices?.items ?? []).map((item) => [item.instrument, item]));
}

function latestIntradayForNode(node: ChainNodeName, latestPrices?: LatestPricesResponse) {
  const items = latestPriceMap(latestPrices);
  if (node === "原油") return items.get("Brent") ?? items.get("WTI");
  if (node === "石脑油") return items.get("NAPHTHA");
  return items.get(node);
}

function chainValueFromIntraday(item?: LatestPriceItem): ChainLatestValue | undefined {
  const latest = item?.latest;
  if (!item || !latest || latest.last === undefined || latest.last === null) return undefined;
  return {
    value: formatValue(latest.last, latest.unit),
    date: latest.observed_at.slice(0, 10),
    detail: item.is_transaction_price ? latest.symbol : item.quote_type_label,
    freshnessLabel: item.freshness_label,
    quoteTypeLabel: item.quote_type_label,
    isTransactionPrice: item.is_transaction_price,
    change: percentChangeFromNumber(latest.change_pct)
  };
}

export function chainLatestValue(
  node: ChainNodeName,
  marketObservations: MarketObservation[],
  industryObservations: IndustryObservation[],
  latestPrices?: LatestPricesResponse
): ChainLatestValue | undefined {
  const intraday = chainValueFromIntraday(latestIntradayForNode(node, latestPrices));
  if (intraday) return intraday;

  if (node === "原油") {
    const brent = latestBy(
      marketObservations,
      (item) => /brent/i.test(`${item.product} ${item.indicator}`)
    );
    const wti = latestBy(
      marketObservations,
      (item) => /wti/i.test(`${item.product} ${item.indicator}`)
    );
    const primary = brent ?? wti;
    if (!primary) return undefined;
    const primarySeries = latestItems(
      marketObservations,
      (item) =>
        brent
          ? /brent/i.test(`${item.product} ${item.indicator}`)
          : /wti/i.test(`${item.product} ${item.indicator}`)
    );
    const secondary = brent && wti ? `WTI ${formatValue(wti.value, wti.unit)}` : primary.indicator;
    return {
      value: formatValue(primary.value, primary.unit),
      date: primary.observed_at.slice(0, 10),
      detail: secondary,
      freshnessLabel: "日频参考",
      quoteTypeLabel: "日频历史价格",
      isTransactionPrice: false,
      change: percentChange(primarySeries[0], primarySeries[1])
    };
  }

  const industrySeries = latestIndustrySeriesForNode(node, industryObservations);
  const industry = industrySeries[0] ?? latestIndustryForNode(node, industryObservations);
  if (!industry) return undefined;
  return {
    value: formatValue(industry.value, industry.unit),
    date: industry.observed_at.slice(0, 10),
    detail: node === "POY" || node === "DTY" ? "公开现货评估" : industry.metric,
    freshnessLabel: industry.frequency === "manual" ? "人工/慢队列" : "现货更新",
    quoteTypeLabel: node === "POY" || node === "DTY" ? "非成交型现货评估价" : "公开现货/期货参考",
    isTransactionPrice: false,
    change: percentChange(industrySeries[0], industrySeries[1])
  };
}
