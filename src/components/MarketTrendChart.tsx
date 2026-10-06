import { priceChartScale } from "./priceChartScale";
import { marketTimeline, type MarketWindow } from "../utils/marketTimeline";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

// Loaded lazily from the workbench page so the overview first paint does not
// pay for the recharts chunk; this module is the only recharts import site.
export interface MarketTrendChartProps {
  data: { date: string; value: number }[];
  window: MarketWindow;
  unitLabel: string;
  currentDate?: string;
  formatPrice: (value: number, unit: string) => string;
  formatDisplayDate: (value?: string) => string;
  firstDate?: string;
  lastDate?: string;
  pointCount: number;
}

export function MarketTrendChart({
  data,
  window,
  unitLabel,
  currentDate,
  formatPrice,
  formatDisplayDate,
  firstDate,
  lastDate,
  pointCount
}: MarketTrendChartProps) {
  const timeline = marketTimeline(data, window);
  if (!timeline) return null;
  const scale = priceChartScale(data.map(point => point.value));
  const tickStep = scale.ticks[1] - scale.ticks[0];
  const fractionDigits = Number.isInteger(tickStep) ? 0 : Math.min(8, Math.max(1, 1 - Math.floor(Math.log10(Math.abs(tickStep)))));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart
        data={timeline.rows}
        data-testid="market-trend-chart"
        data-point-count={pointCount}
        data-first-date={firstDate ?? ""}
        data-last-date={lastDate ?? ""}
        data-current-merged={currentDate ? "true" : "false"}
        data-axis-start={new Date(timeline.domain[0]).toISOString().slice(0, 10)}
        data-axis-end={new Date(timeline.domain[1]).toISOString().slice(0, 10)}
        // The last X tick ("09-14") and the final point sit on the plot's right
        // edge; without an explicit right margin recharts clips both.
        margin={{ top: 8, right: 28, bottom: 0, left: 0 }}
      >
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="timestamp"
          type="number"
          scale="utc"
          domain={timeline.domain}
          ticks={timeline.ticks}
          interval={0}
          tickFormatter={(value) => new Date(Number(value)).toISOString().slice(
            firstDate?.slice(0, 4) !== lastDate?.slice(0, 4) ? 2 : 5, 10)}
          tickMargin={8}
        />
        <YAxis
          width={64}
          domain={scale.domain}
          ticks={scale.ticks}
          interval={0}
          tick={{ fontSize: 11 }}
          tickFormatter={(value) => Number(value).toLocaleString("zh-CN", { maximumFractionDigits: fractionDigits })}
          label={{ value: unitLabel, angle: -90, position: "insideLeft" }}
        />
        <Tooltip
          formatter={(value, _name, item) => [
            formatPrice(Number(value), unitLabel),
            (item?.payload as { date?: string } | undefined)?.date === currentDate ? "价格（当前报价）" : "价格"
          ]}
          labelFormatter={(value) => formatDisplayDate(new Date(Number(value)).toISOString().slice(0, 10))}
        />
        {/* 日历轴上非观测日（休市/未发布）为 null；相邻观测日直接连线，
            避免每个周末/假日把趋势线打断成碎段。外口径报价点不会进入本序列
            （页面层 basis mismatch 时不合并），连线不产生跨口径视觉拼接。 */}
        <Area dataKey="value" stroke="#2563eb" fill="#dbeafe" connectNulls
          dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export default MarketTrendChart;
