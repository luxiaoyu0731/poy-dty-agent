import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Database, FileText, Info, Layers3 } from "lucide-react";
import type {
  IndustryObservation,
  LlmBacktestEvent,
  LlmEventDirection,
  NewsSource,
  SourceReadiness
} from "../services/api";
import {
  categoryLabel,
  evidenceLevelLabel,
  productListLabel,
  sourceNameLabel
} from "./displayLabels";
import {
  evidenceStatusDescription,
  evidenceStatusFromTargets,
  formatPercent,
  verdictLabel,
  verdictTone
} from "./llmEventReview";
import { IconBox, StatusPill, cx, type Tone } from "./ui";

export function cleanText(value?: string | null, fallback = "暂无摘要") {
  if (!value?.trim()) return fallback;
  const cleaned = value
    .replace(/<[^>]*>/g, " ")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\bhref="\S+"/g, " ")
    .replace(/Cookies on .*?remember/gi, " ")
    .replace(/Skip to main content/gi, " ")
    .replace(/Here.?s how you know/gi, " ")
    .replace(/。?分类\s*[^，。]+[，。]\s*初步方向\s*[^，。]+[，。]\s*影响对象\s*[^。]+。?原文线索[:：]?/g, " ")
    .replace(/公开\/手工事件入库\s*[-→> ]+/g, " ")
    .replace(/影响对象[:：]\s*/g, "影响：")
    .replace(/初始方向[:：]\s*/g, "方向：")
    .replace(/等待价格、库存、开工率和贸易流交叉验证/g, "等待交叉验证")
    .replace(/dollars_per_barrel/gi, "$/bbl")
    .replace(/spot_quote/gi, "现货报价")
    .replace(/Load failed/gi, "本次更新未完成")
    .replace(/后端/g, "本地服务")
    .replace(/fred_macro_api/g, "FRED")
    .replace(/eia_petroleum_api/g, "EIA")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || fallback;
}

export function shortText(value?: string | null, limit = 112) {
  const text = cleanText(value);
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

export function displaySourceName(sourceId?: string | null) {
  return sourceNameLabel(sourceId);
}

export function readinessLabel(status: SourceReadiness["status"]) {
  if (status === "ready") return "可用";
  if (status === "requires_api_key") return "需授权";
  if (status === "requires_license") return "需授权";
  if (status === "internal_only") return "内部输入";
  return "待确认";
}

export function readinessTone(status: SourceReadiness["status"]): Tone {
  if (status === "ready") return "good";
  if (status === "requires_api_key" || status === "internal_only") return "warn";
  if (status === "requires_license") return "bad";
  return "neutral";
}

function directionTone(direction?: string): Tone {
  if (direction?.includes("利多")) return "good";
  if (direction?.includes("利空")) return "bad";
  if (direction?.includes("中性")) return "neutral";
  return "warn";
}

function directionLabel(direction?: string) {
  if (!direction) return "";
  if (direction.includes("利多")) return "利多";
  if (direction.includes("利空")) return "利空";
  if (direction.includes("中性")) return "中性";
  return cleanText(direction, "待判断");
}

function tierTone(tier?: string): Tone {
  if (tier === "A") return "good";
  if (tier === "B") return "accent";
  if (tier === "C") return "warn";
  return "neutral";
}

function eventStatusLabel(status?: ReactNode) {
  if (typeof status !== "string") return status;
  const match = status.match(/(?:(利多|利空|中性))?事件[，,]\s*强度\s*([0-9.]+)/);
  if (match) {
    const score = Math.round(Number(match[2]) * 100);
    const direction = match[1] ? `${match[1]} · ` : "";
    return `${direction}强度 ${Number.isFinite(score) ? `${score}%` : "待校验"}`;
  }
  return cleanText(status, "状态待校验");
}

export function SummaryMetric({
  label,
  value,
  caption,
  tone = "neutral",
  icon
}: {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <article className={cx("summary-metric", `tone-${tone}`)}>
      <IconBox tone={tone}>{icon ?? <Info size={17} />}</IconBox>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {caption ? <small>{caption}</small> : null}
      </div>
    </article>
  );
}

export function EvidenceBadge({
  tier,
  label
}: {
  tier?: string;
  label?: string;
}) {
  return <StatusPill tone={tierTone(tier)}>{label ?? evidenceLevelLabel(tier)}</StatusPill>;
}

export function EmptyDataNotice({
  title,
  children
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-data-notice">
      <Info size={18} />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  );
}

export function AuditDrawer({
  title = "高级详情",
  summary,
  defaultOpen = false,
  children
}: {
  title?: string;
  summary?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="audit-drawer" open={defaultOpen}>
      <summary>
        <span>{title}</span>
        {summary ? <small>{summary}</small> : null}
      </summary>
      <div>{children}</div>
    </details>
  );
}

export function SourceCoverageCard({
  readiness,
  newsSources,
  industryObservations
}: {
  readiness: SourceReadiness[];
  newsSources: NewsSource[];
  industryObservations: IndustryObservation[];
}) {
  const ready = readiness.filter((source) => source.status === "ready").length;
  const blocked = readiness.filter((source) => source.status !== "ready");
  return (
    <div className="source-coverage-card">
      <SummaryMetric
        caption="行情、宏观、交易所与手工输入"
        icon={<Database size={18} />}
        label="核心数据源"
        tone={ready === readiness.length ? "good" : "warn"}
        value={`${ready}/${readiness.length || "--"}`}
      />
      <SummaryMetric
        caption="公开新闻、机构公告与航运安全源"
        icon={<FileText size={18} />}
        label="新闻源"
        tone={newsSources.length ? "good" : "warn"}
        value={newsSources.length || "--"}
      />
      <SummaryMetric
        caption="POY/DTY/PX/PTA/MEG 与人工/慢队列输入"
        icon={<Layers3 size={18} />}
        label="行业观测"
        tone={industryObservations.length ? "accent" : "warn"}
        value={industryObservations.length || "--"}
      />
      <div className="source-gap-list">
        <strong>当前缺口</strong>
        {blocked.length ? (
          blocked.map((source) => (
            <span key={source.source_id}>
              <AlertTriangle size={13} />
          {displaySourceName(source.source_id)}：{readinessLabel(source.status)}
            </span>
          ))
        ) : (
          <span><CheckCircle2 size={13} />核心来源均可用</span>
        )}
        <span><AlertTriangle size={13} />部分 POY/DTY 后验价格窗口仍待真实价格补齐</span>
      </div>
    </div>
  );
}

export function EventCard({
  title,
  summary,
  direction,
  confidence,
  tier,
  products,
  status,
  meta,
  onSelect
}: {
  title: string;
  summary?: string;
  direction?: string;
  confidence?: number | null;
  tier?: string;
  products?: string[];
  status?: ReactNode;
  meta?: ReactNode;
  onSelect?: () => void;
}) {
  return (
    <article className="research-event-card">
      <header>
        <strong>{title}</strong>
        <div>
          <StatusPill tone={directionTone(direction)}>{direction ? `模型 ${directionLabel(direction)}` : "待判断"}</StatusPill>
          <EvidenceBadge tier={tier} />
        </div>
      </header>
      <p>{shortText(summary, 128)}</p>
      <footer>
        <span>{productListLabel(products)}</span>
        {confidence !== undefined && confidence !== null ? <span>置信度 {formatPercent(confidence)}</span> : null}
        {status ? <span>{eventStatusLabel(status)}</span> : null}
        {meta ? <span>{meta}</span> : null}
      </footer>
      {onSelect ? <button onClick={onSelect} type="button">查看证据</button> : null}
    </article>
  );
}

export function BacktestSummary({
  response
}: {
  response?: {
    items?: LlmEventDirection[];
    backtest?: {
      summary?: {
        scored_events?: number;
        hit?: number;
        miss?: number;
        neutral_or_unscored?: number;
        hit_rate?: number | null;
        pending_future_prices?: number;
      };
      guardrails?: {
        future_evidence_leaks?: number;
      };
    } | null;
    price_curve_comparison?: LlmBacktestEvent[] | null;
  } | null;
}) {
  const summary = response?.backtest?.summary;
  const events = response?.price_curve_comparison ?? [];
  const pending = summary?.pending_future_prices ?? events.filter((item) => item.posterior_status === "pending_future_prices").length;
  const misses = events.filter((item) => item.verdict === "miss");
  const reasons = countReasons(misses);
  return (
    <div className="backtest-summary" id="miss-reasons">
      <SummaryMetric label="方向有效性" value={formatPercent(summary?.hit_rate)} caption="历史复核" tone="accent" />
      <SummaryMetric label="已验证" value={summary?.scored_events ?? "--"} caption={`同向 ${summary?.hit ?? "--"} · 背离 ${summary?.miss ?? "--"}`} tone="good" />
      <SummaryMetric label="待补依据" value={summary?.neutral_or_unscored ?? "--"} caption={`缺后续价格 ${pending}`} tone={pending ? "warn" : "neutral"} />
      <SummaryMetric label="时点边界" value={response?.backtest?.guardrails?.future_evidence_leaks ?? "--"} caption="前推边界检查" tone={response?.backtest?.guardrails?.future_evidence_leaks ? "bad" : "good"} />
      <div className="miss-reason-list">
        <strong>方向背离原因前五</strong>
        {reasons.length ? reasons.map(([reason, count]) => <span key={reason}>{reason}<b>{count}</b></span>) : <span>暂无方向背离原因</span>}
      </div>
    </div>
  );
}

export function BacktestEventCard({
  item,
  judgment
}: {
  item: LlmBacktestEvent;
  judgment?: LlmEventDirection;
}) {
  const evidenceStatus = evidenceStatusFromTargets(item.targets);
  return (
    <article className="backtest-event-card">
      <header>
        <span>{item.as_of_time.slice(0, 10)} · {categoryLabel(item.topic)}</span>
        <StatusPill tone={verdictTone(item.verdict)}>{verdictLabel(item.verdict)}</StatusPill>
      </header>
      <strong>{item.title}</strong>
      <div className="backtest-event-meta">
        <StatusPill tone={directionTone(item.llm_direction)}>模型 {item.llm_direction}</StatusPill>
        <StatusPill tone={directionTone(item.actual_direction)}>后验 {item.actual_direction}</StatusPill>
        <StatusPill tone={evidenceStatus === "full_14d" ? "good" : evidenceStatus === "partial_observed" ? "warn" : "neutral"}>
          {evidenceStatus === "full_14d" ? "完整14天" : evidenceStatus === "partial_observed" ? "部分后验" : "待真实价格"}
        </StatusPill>
      </div>
      <p>{shortText(judgment?.reasoning || item.counter_evidence || item.error_reason, 132)}</p>
      <small>{item.error_reason || evidenceStatusDescription(evidenceStatus)}</small>
      <AuditDrawer title="高级详情" summary="证据数量与后验说明">
        <div className="audit-kv">
          <span>事件记录</span><code>已记录</code>
          <span>引用证据</span><code>{item.cited_doc_ids.length ? `${item.cited_doc_ids.length} 条` : "无"}</code>
          <span>判断来源</span><code>{judgment ? "模型判断已匹配" : "待匹配"}</code>
          <span>后验说明</span><code>{evidenceStatusDescription(evidenceStatus)}</code>
        </div>
      </AuditDrawer>
    </article>
  );
}

function countReasons(items: LlmBacktestEvent[]) {
  const counts = new Map<string, number>();
  items.forEach((item) => {
    const reasons = item.miss_reasons?.length ? item.miss_reasons : [item.miss_reason || item.error_reason || "未分类"];
    reasons.forEach((reason) => {
      const label = reasonLabel(cleanText(reason, "未分类")).slice(0, 26);
      counts.set(label, (counts.get(label) ?? 0) + 1);
    });
  });
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 5);
}

function reasonLabel(value: string) {
  const labels: Record<string, string> = {
    priced_in: "风险已计价",
    demand_offset: "需求走弱抵消",
    demand_weakness_offset: "需求走弱抵消",
    supply_recovery: "供应恢复",
    supply_recovery_offset: "供应恢复",
    macro_offset: "宏观压力抵消",
    low_evidence: "证据不足",
    wrong_direction: "方向判断偏差",
    pending_future_price: "缺未来真实价格"
  };
  return labels[value] ?? value.replace(/_/g, " ");
}
