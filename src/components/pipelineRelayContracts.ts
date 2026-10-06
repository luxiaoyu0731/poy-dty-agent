/** Workflow contracts, not claims about a particular run. Actual execution is graph status_detail. */
export const pipelineRelayContracts: Record<string, { input: string; output: string }> = {
  collect: { input: "公开新闻与价格来源", output: "原文、来源与发布时间" },
  clean: { input: "采集原文与来源记录", output: "通过质量检查的去重材料" },
  index: { input: "合格材料与原文出处", output: "可检索分块与引用定位" },
  event_summary: { input: "合格原文与证据引句", output: "主体、动作与发生时间" },
  event_overview: { input: "结构化事实与来源引用", output: "聚合事件与材料关联" },
  factor_score: { input: "事件事实与价格信息", output: "事件方向信号与因子分" },
  event_signal: { input: "近七日候选事件", output: "精选事件与冻结输入标识" },
  political_analysis: { input: "冻结事件与原文证据", output: "执行概率与传导路径" },
  historical_analog: { input: "事件解读与可比案例", output: "历史先验与案例引用" },
  product_synthesis: { input: "价格基准、事件与先验", output: "七品种分期限事件因子" },
  skeptic_review: { input: "事件因子与相反线索", output: "维持、降级或推翻意见" },
  event_fusion: { input: "价格基准与质证结果", output: "按定案规则生成最终方向" },
  seven_product: { input: "最终方向与输入快照", output: "二十一格预测冻结入账" },
  shadow_eval: { input: "到期预测与有效价格", output: "结算结果与定案审计" },
  counter_scan: { input: "当日判断与相反材料", output: "可追溯的反证线索" },
  daily_interpretation: { input: "当日发牌与结算结果", output: "日报解读与依据引用" },
  report_assembly: { input: "日报解读与主链工件", output: "研报与可核对的出处" },
  assistant: { input: "你的问题与主链工件", output: "只读回答与来源引用" },
  unified_memory: { input: "索引、案例与复盘教训", output: "同向 ≥3 条独立互证才有计票权" }
};
