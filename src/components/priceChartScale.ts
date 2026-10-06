/** Detail scale: follows the observed price range, with explicit headroom and regular ticks. */
export function priceChartScale(values: number[]) {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return { domain: [0, 1] as [number, number], ticks: [0, .2, .4, .6, .8, 1] };
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = Math.max(max - min, Math.abs(max) * .05, .01);
  const lower = min >= 0 ? Math.max(0, min - span * .15) : min - span * .15;
  const upper = max + span * .15;
  const rough = (upper - lower) / 10;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map(n => n * power).reduce((best, n) => Math.abs(n - rough) < Math.abs(best - rough) ? n : best);
  const start = Math.floor(lower / step) * step;
  const end = Math.ceil(upper / step) * step;
  const ticks = Array.from({ length: Math.round((end - start) / step) + 1 }, (_, i) => Number((start + i * step).toPrecision(12)));
  return { domain: [ticks[0], ticks[ticks.length - 1]] as [number, number], ticks };
}
