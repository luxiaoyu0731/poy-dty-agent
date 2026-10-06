const DEFAULT_TIME_ZONE = "Asia/Shanghai";

export function formatDisplayTimestamp(value?: string | null): string | undefined {
  if (!value) return undefined;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value.trim())) return value.trim();
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: DEFAULT_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  }).format(parsed).replace(/\//g, "-");
}

/** Display prose only. Preserve URLs and code verbatim for links and traceability. */
export function formatTimestampsInText(value?: string | null): string {
  if (!value) return "";
  return value.replace(
    /https?:\/\/[^\s<>）]+|`[^`]*`|\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))(\s*[（(]UTC[）)])?/g,
    (match, timestamp: string | undefined, utcLabel: string | undefined) => {
      if (!timestamp) return match;
      const formatted = formatDisplayTimestamp(timestamp);
      return formatted ? `${formatted}${utcLabel ? "（上海）" : ""}` : match;
    }
  );
}

const metricLabels = {
  price: "价格",
  inventory: "库存",
  operating: "开工率",
  profit: "利润"
} as const;

const freshnessLabels = {
  realtime: "实时",
  near_realtime: "近实时",
  delayed: "延迟",
  stale: "数据滞后",
  valuation: "估值",
  missing: "暂无数据"
} as const;

export type MetricField = keyof typeof metricLabels;
export type FreshnessStatus = keyof typeof freshnessLabels;

function validDate(value: string | Date) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

/**
 * Labels a known metric field without attempting to rewrite arbitrary
 * business copy. Unknown values are preserved so newly added API fields do
 * not silently acquire the wrong meaning.
 */
export function formatMetricLabel(value: string) {
  return metricLabels[value.trim().toLowerCase() as MetricField] ?? value;
}

/**
 * Converts a structured freshness enum to customer-facing Chinese. This is
 * deliberately an exact enum lookup, not a global "(stale)" replacement.
 */
export function formatFreshnessLabel(value?: string | null, fallback = "等待更新") {
  if (!value?.trim()) return fallback;
  const normalized = value.trim().toLowerCase();
  return freshnessLabels[normalized as FreshnessStatus] ?? value;
}

/**
 * Formats a timestamp as a calendar date in the product's reporting timezone.
 * Calendar-only ISO values stay calendar-only and are not shifted by parsing.
 */
export function formatDisplayDate(
  value?: string | Date | null,
  options: { fallback?: string; timeZone?: string } = {}
) {
  const fallback = options.fallback ?? "暂无日期";
  if (!value) return fallback;
  if (typeof value === "string") {
    const calendarDate = value.trim().match(/^(\d{4})-(\d{2})-(\d{2})(?:$|T)/);
    if (calendarDate && !value.includes("T")) {
      const [, year, month, day] = calendarDate;
      return `${year}-${month}-${day}`;
    }
  }
  const date = validDate(value);
  if (!date) return fallback;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: options.timeZone ?? DEFAULT_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(date);
}

export function formatDisplayDateRange(
  start?: string | Date | null,
  end?: string | Date | null,
  options: { fallback?: string; timeZone?: string } = {}
) {
  const fallback = options.fallback ?? "暂无区间";
  if (!start || !end) return fallback;
  const startLabel = formatDisplayDate(start, { ...options, fallback: "" });
  const endLabel = formatDisplayDate(end, { ...options, fallback: "" });
  return startLabel && endLabel ? `${startLabel} 至 ${endLabel}` : fallback;
}

/**
 * Normalizes only machine-shaped date ranges in explanatory copy. It does not
 * translate arbitrary words or alter figures, product names, or conclusions.
 */
export function formatDateRangesInText(value?: string | null) {
  if (!value) return "";
  return formatTimestampsInText(value)
    .replace(
      /(\d{4}-\d{2}-\d{2})\s*(?:\.\.|~|～)\s*(\d{4}-\d{2}-\d{2})/g,
      (_match, start: string, end: string) => formatDisplayDateRange(start, end)
    )
    .replace(/([。！？])\s*[.。]+/g, "$1")
    .replace(/\.{2,}(?=\s|$)/g, "。")
    .trim();
}

export interface MetricFreshnessItem {
  field: string;
  date?: string | Date | null;
  freshness?: string | null;
}

export function formatMetricFreshness(item: MetricFreshnessItem) {
  return {
    label: formatMetricLabel(item.field),
    date: formatDisplayDate(item.date),
    status: formatFreshnessLabel(item.freshness)
  };
}


export function formatMarketFreshnessSummary(
  freshness?: { status: string; categories?: Record<string, { status: string }> },
  displayedPriceStale?: boolean
) {
  if (!freshness?.categories) return freshness?.status === "stale" ? "部分数据滞后" : "数据状态待确认";
  const labels: Record<string, string> = { price: "价格", inventory: "库存", operating: "开工率", profit: "加工差" };
  const stale = Object.entries(freshness.categories)
    .filter(([key, item]) => key === "price" && displayedPriceStale !== undefined
      ? displayedPriceStale : item.status === "stale")
    .map(([key]) => labels[key] ?? key);
  return stale.length ? `${stale.join("、")}数据滞后` : "核心指标已更新";
}
