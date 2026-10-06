import { Typography } from "antd";
import { formatDisplayTimestamp } from "../../utils/displayFormatting";
import type { EvidenceClaim, EvidenceHistory } from "./types";

export const claimStateLabels: Record<EvidenceClaim["state"], string> = {
  actual: "来源报告",
  planned: "计划／预期",
  unconfirmed: "传闻／未确认",
  denied: "事实否认",
  in_progress: "进行中",
  unknown: "事实状态待核验"
};

export const historyOutcomeLabels: Record<string, string> = {
  pending: "尚未到期",
  no_actual: "缺少实际价格",
  historical_base_unavailable: "当时价格不可用",
  historical_base_stale: "当时价格过期",
  overlapping_outcome_excluded: "与其他案例结果区间重叠",
  scored: "已有成熟价格结果"
};

function directionText(direction: EvidenceClaim["expected_direction"]) {
  if (direction === "up") return "机制预期：价格偏强（推断，非结果）";
  if (direction === "down") return "机制预期：价格偏弱（推断，非结果）";
  return "机制方向待核验";
}

/**
 * One piece of source material with its own boundary.
 * 原文引用（quote）、系统推断（机制预期）与已验证结果（history.outcome）分层呈现，
 * 三者不混写；否认状态的材料显式标红。
 */
export function EvidenceClaimCard({ claim, history, selected, onBackToVerdict }: {
  claim: EvidenceClaim;
  history?: EvidenceHistory;
  selected?: boolean;
  onBackToVerdict?: () => void;
}) {
  const denied = claim.state === "denied";
  const reviewRequired = claim.gaps.includes("该机制需要语义复核，未自动判断方向");
  const materialGaps = claim.gaps.filter(gap => gap !== "该机制需要语义复核，未自动判断方向");
  return (
    <article
      id={`evb-claim-${claim.claim_id}`}
      className={`evb-claim-card ${selected ? "is-selected" : ""} ${denied ? "is-denied" : ""}`}
      data-testid="evidence-claim-card"
    >
      <header>
        <span className={`evb-claim-state ${denied ? "is-denied" : ""}`}>{claimStateLabels[claim.state]}</span>
        <span className={`evb-claim-check ${claim.semantic_status === "rule_checked" ? "is-passed" : ""}`}>
          {claim.semantic_status === "rule_checked" ? "通过机制规则" : "待核验"}
        </span>
        <span className="evb-claim-direction">{directionText(claim.expected_direction)}</span>
        {onBackToVerdict ? (
          <button type="button" className="evb-back-verdict" onClick={onBackToVerdict}>返回判断</button>
        ) : null}
      </header>
      <Typography.Paragraph
        className="evb-claim-quote"
        ellipsis={{ rows: 4, expandable: true, symbol: "展开原文" }}
      >
        {claim.quote}
      </Typography.Paragraph>
      <footer>
        <a href={claim.source_url} target="_blank" rel="noreferrer noopener">{claim.source_title || "查看原始来源"}</a>
        <span>发布 {formatDisplayTimestamp(claim.published_at)}{claim.event_date ? ` · ${claim.event_date_source === "dated_price_report_title" ? "报告标题日期" : "发生／报告期"} ${claim.event_date}` : ""}</span>
      </footer>
      {history ? (
        <div className="evb-claim-history">
          <strong>已验证结果（与推断分开理解）</strong>
          <p>
            {historyOutcomeLabels[history.outcome.state] ?? "结果待核验"}
            {typeof history.outcome.change === "number" ? ` · 事后实际价格变化 ${(history.outcome.change * 100).toFixed(1)}%` : ""}
            。{history.relation === "support"
              ? "实际反应与该事件机制预期同向。"
              : history.relation === "counter"
                ? "实际反应背离该事件机制预期。"
                : "该案例尚未形成支持或反证结论。"}
            历史类比不证明因果，也不代表历史预测成绩。
          </p>
        </div>
      ) : null}
      {reviewRequired ? (
        <p className="evb-claim-scope">核验范围：{claim.mechanism === "price" ? "报价材料作为价格背景，不自动判定供应、需求或库存方向" : "该机制需要语义复核，未自动判断方向"}</p>
      ) : null}
      {materialGaps.length ? (
        <p className="evb-claim-gaps">该材料自身缺口：{materialGaps.join("；")}</p>
      ) : null}
      {claim.inference_boundary ? <p className="evb-claim-boundary">{claim.inference_boundary}</p> : null}
    </article>
  );
}
