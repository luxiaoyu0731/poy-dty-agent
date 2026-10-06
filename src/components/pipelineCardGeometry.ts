/** Presentation geometry shared by cards, bands and routed edge gutters. */
export const pipelineRowGeometry = {
  preparation: { y: 0, height: 330 },
  prediction: { y: 430, height: 480 },
  review: { y: 1010, height: 330 },
} as const;
// A 100px row gap contains a 12px lower border, a 72px next-row header,
// and a separate 16px gutter. Route through the gutter's centre.
export const pipelineLaneGeometry = { header: 72, footer: 12, gutterOffset: 20 } as const;
const predictionIds = new Set(["event_signal", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion", "seven_product", "shadow_eval"]);
const reviewIds = new Set(["counter_scan", "daily_interpretation", "report_assembly", "unified_memory", "assistant"]);
export function pipelineCardGeometry(nodeId: string) {
  return predictionIds.has(nodeId) ? pipelineRowGeometry.prediction : reviewIds.has(nodeId) ? pipelineRowGeometry.review : pipelineRowGeometry.preparation;
}
