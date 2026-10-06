import { Typography } from "antd";
import { formatDisplayTimestamp } from "../../utils/displayFormatting";
import type { EvidenceSemanticReview } from "./types";

export function semanticTimeLabel(review: EvidenceSemanticReview) {
  return review.time_kind === "current_state" ? "当前状态报道；发生日未确认"
    : review.time_kind === "reported_announcement" ? "决策报道；发生日未确认，效果未兑现"
    : `发生／报告期 ${review.period_start}～${review.period_end}`;
}

export function SemanticEvidenceCard({ review }: { review: EvidenceSemanticReview }) {
  const validUrl = /^https?:\/\//i.test(review.source_url);
  return <article id={`evb-semantic-${review.review_id}`} className="evb-claim-card" data-testid="semantic-evidence-card">
    <header>
      <span className="evb-claim-state">AI 条件分析 · 不计票</span>
      {review.binding_method === "ai_coreference" ? <span className="evb-claim-check">AI 跨句关联 · 条件依据</span> : null}
      <span className="evb-claim-check">{review.relation === "direct" ? "直接材料 · 条件依据" : "上游材料 · 条件依据"}</span>

      {review.fact_stage === "announced" ? <span className="evb-claim-state">已宣布决定 · 执行待核验</span> : null}
    </header>
    <p><strong>{review.direction === "up" ? "上行压力理由：" : "下行压力理由："}</strong>{review.rationale}</p>
    <p><strong>仍需成立的条件：</strong>{review.conditions.join("；")}</p>
    <footer>
      {validUrl ? <a href={review.source_url} target="_blank" rel="noreferrer noopener">{review.source_title}</a> : <span>{review.source_title}</span>}
      <span>发布 {formatDisplayTimestamp(review.published_at)}</span>
      <span>{semanticTimeLabel(review)}</span>
    </footer>
    <details><summary>查看原文与核验记录</summary>
      <Typography.Paragraph className="evb-claim-quote">{review.quote}</Typography.Paragraph>
      {review.binding_method === "ai_coreference" ? <><p>跨句连接原文：</p><blockquote>{review.binding_quote}</blockquote><p>{review.binding_reason}</p><p>关联复核模型 {review.binding_model}；这是 AI 对文本关联的复核，不等于现实事实已经独立核实。</p></> : null}
      {review.scope_quote ? <><p>原料范围原文（关联设施或地区：{review.scope_entity}）：</p><blockquote>{review.scope_quote}</blockquote></> : null}
      <p>复核于 {formatDisplayTimestamp(review.reviewed_at)} · 模型 {review.model}。引句与主体、动作已校验原文绑定；AI 的机制推理尚不证明现实真实性、因果效果或预测命中。</p>
    </details>
  </article>;
}
