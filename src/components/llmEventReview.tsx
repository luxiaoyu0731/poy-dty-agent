import { AlertTriangle, FileText, Gauge, Info, ShieldCheck } from "lucide-react";
import type { LlmBacktestEvent, LlmBacktestFactors, LlmBacktestTarget, LlmEventDirection } from "../services/api";
import { categoryLabel, evidenceLevelLabel, sourceNameLabel, statusLabel } from "./displayLabels";
import { toneForDirection, toneForTier } from "./domain";
import { EmptyState, StatusPill, cx, type Tone } from "./ui";

export type BacktestEvidenceStatus = "full_14d" | "partial_observed" | "pending";

type ExplanationFactor = {
  key: keyof LlmBacktestFactors;
  label: string;
  shortLabel: string;
  active?: boolean;
};

const explanationFactors: ExplanationFactor[] = [
  {
    key: "risk_premium_decay",
    label: "风险溢价消化",
    shortLabel: "风险溢价"
  },
  {
    key: "demand_weakness_offset",
    label: "需求抵消",
    shortLabel: "需求"
  },
  {
    key: "supply_recovery_offset",
    label: "供应恢复",
    shortLabel: "供应"
  },
  {
    key: "inventory_pressure",
    label: "库存压力",
    shortLabel: "库存"
  },
  {
    key: "dollar_rate_pressure",
    label: "美元/利率",
    shortLabel: "美元"
  },
  {
    key: "OPEC_supply_signal",
    label: "OPEC供给",
    shortLabel: "OPEC"
  },
  {
    key: "refinery_margin_signal",
    label: "炼厂利润",
    shortLabel: "炼厂"
  },
  {
    key: "shipping_disruption_signal",
    label: "航运扰动",
    shortLabel: "航运"
  }
];

export function formatPercent(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "--";
  return `${Math.round(value * 100)}%`;
}

export function verdictLabel(value?: LlmBacktestEvent["verdict"]) {
  if (value === "hit") return "命中";
  if (value === "miss") return "错判";
  return "待评分";
}

export function verdictTone(value?: LlmBacktestEvent["verdict"]): Tone {
  if (value === "hit") return "good";
  if (value === "miss") return "bad";
  return "warn";
}

export function evidenceStatusFromTargets(targets?: Record<string, LlmBacktestTarget>): BacktestEvidenceStatus {
  const values = Object.values(targets ?? {});
  if (!values.length) return "pending";
  const scoredLike = (target: LlmBacktestTarget) => (
    target.status === "scored" || target.posterior_status === "full_14d" || target.scoring_eligible === true
  );
  if (values.every(scoredLike)) return "full_14d";
  if (values.some((target) => (
    scoredLike(target) || target.posterior_status === "partial_observed" || target.status === "partial_observed"
  ))) {
    return "partial_observed";
  }
  return "pending";
}

export function evidenceStatusTone(status: BacktestEvidenceStatus): Tone {
  if (status === "full_14d") return "good";
  if (status === "partial_observed") return "warn";
  return "neutral";
}

export function evidenceStatusDescription(status: BacktestEvidenceStatus) {
  if (status === "full_14d") return "Brent/WTI/POY/DTY 均返回可评分后验点";
  if (status === "partial_observed") return "仅部分标的有可评分后验点";
  return "缺少完整未来真实价格，暂不计入 hit/miss";
}

export function findLlmJudgmentForBacktest(
  judgments: LlmEventDirection[],
  item: LlmBacktestEvent
): LlmEventDirection | undefined {
  const itemDate = item.as_of_time.slice(0, 10);
  return judgments.find((judgment) => (
    judgment.event_id === item.event_id && judgment.as_of_time.slice(0, 10) === itemDate
  )) ?? judgments.find((judgment) => judgment.event_id === item.event_id);
}

export function LlmDirectionCard({
  item,
  compact = false
}: {
  item: LlmEventDirection;
  compact?: boolean;
}) {
  const sourceStatus = item.fallback ? "fallback" : item.error ? "error" : item.should_enter_backtest ? "entered_backtest" : "not_scored";
  return (
    <article className={cx("llm-review-card", compact && "is-compact")}>
      <header>
        <span>{formatDate(item.as_of_time)} · {sourceNameLabel(item.source_id)}</span>
        <StatusPill tone={toneForDirection(item.llm_direction)}>{item.llm_direction}</StatusPill>
      </header>
      <strong>{item.title}</strong>
      <div className="llm-card-metrics">
        <StatusPill tone="accent">置信度 {formatPercent(item.confidence)}</StatusPill>
        <StatusPill tone={toneForTier(item.evidence_level)}>{evidenceLevelLabel(item.evidence_level)}</StatusPill>
        <StatusPill tone={item.cited_doc_ids.length ? "good" : "warn"}>
          {item.cited_doc_ids.length ? `引用证据 ${item.cited_doc_ids.length} 条` : "缺少引用证据"}
        </StatusPill>
        <StatusPill tone={item.should_enter_backtest ? "good" : "neutral"}>{statusLabel(sourceStatus)}</StatusPill>
      </div>
      <p>{item.reasoning || "暂无模型解释。"}</p>
      <small>{item.counter_evidence || "暂无反证说明。"}</small>
      <ExplanationFactors judgment={item} />
      <DocIdRow ids={item.cited_doc_ids} />
      <footer className="llm-card-footer">
        <span>{statusLabel(item.record_type)} · {categoryLabel(item.category)}</span>
        <span>模型判断已生成</span>
      </footer>
      {item.error ? <p className="error-copy"><AlertTriangle size={13} />{item.error}</p> : null}
    </article>
  );
}

export function LlmBacktestCard({
  item,
  judgment
}: {
  item: LlmBacktestEvent;
  judgment?: LlmEventDirection;
}) {
  const evidenceStatus = evidenceStatusFromTargets(item.targets);
  return (
    <article className="llm-review-card">
      <header>
        <span>{formatDate(item.as_of_time)} · {item.topic}</span>
        <StatusPill tone={verdictTone(item.verdict)}>{verdictLabel(item.verdict)}</StatusPill>
      </header>
      <strong>{item.title}</strong>
      <div className="llm-card-metrics">
        <StatusPill tone={toneForDirection(item.llm_direction)}>模型方向 {item.llm_direction}</StatusPill>
        <StatusPill tone="accent">置信度 {formatPercent(item.llm_confidence)}</StatusPill>
        {judgment ? <StatusPill tone={toneForTier(judgment.evidence_level)}>{evidenceLevelLabel(judgment.evidence_level)}</StatusPill> : null}
        <StatusPill tone={toneForDirection(item.actual_direction)}>后验 {item.actual_direction}</StatusPill>
        <StatusPill tone={evidenceStatusTone(evidenceStatus)}>后验价格 {statusLabel(evidenceStatus)}</StatusPill>
      </div>
      <p>{judgment?.reasoning || item.counter_evidence || "暂无模型解释；仅展示回测对照。"}</p>
      <small>{item.error_reason || (item.verdict === "hit" ? "命中项未返回错误原因。" : "暂无错误原因分类。")}</small>
      <ExplanationFactors errorReason={item.error_reason} factors={item.factors} judgment={judgment} />
      <TargetGrid targets={item.targets} />
      <DocIdRow ids={item.cited_doc_ids} />
      <footer className="llm-card-footer">
        <span>{evidenceStatusDescription(evidenceStatus)}</span>
        {judgment ? <span>模型判断已匹配</span> : null}
      </footer>
    </article>
  );
}

export function LlmEmptyState({ scope }: { scope: "review" | "news" }) {
  if (scope === "news") {
    return (
      <EmptyState title="未匹配到模型方向">当前事件还没有对应的模型判断，请先刷新模型方向或重新入库新闻。</EmptyState>
    );
  }
  return (
    <EmptyState title="暂无模型回测结果">需要先运行模型事件方向判断，并生成本地回测与价格曲线对照报告。</EmptyState>
  );
}

function ExplanationFactors({
  judgment,
  factors: backtestFactors,
  errorReason = ""
}: {
  judgment?: LlmEventDirection;
  factors?: LlmBacktestFactors;
  errorReason?: string;
}) {
  const judgmentFactors: LlmBacktestFactors = {
    risk_premium_decay: judgment?.risk_premium_decay,
    demand_weakness_offset: judgment?.demand_weakness_offset,
    supply_recovery_offset: judgment?.supply_recovery_offset
  };
  const factors = explanationFactors.map((factor) => ({
    ...factor,
    active: backtestFactors?.[factor.key] ?? judgmentFactors[factor.key] ?? errorReason.includes(factor.shortLabel)
  }));
  const hasKnownValue = Boolean(backtestFactors || judgment || errorReason);
  return (
    <div className="llm-factor-row" aria-label="解释因子">
      <span><Gauge size={13} />解释因子</span>
      {factors.map((factor) => (
        <i className={cx(factor.active && "is-active", !hasKnownValue && "is-unknown")} key={factor.key}>
          {factor.label}{hasKnownValue ? ` ${factor.active ? "是" : "否"}` : " 待补"}
        </i>
      ))}
    </div>
  );
}

function TargetGrid({ targets }: { targets: Record<string, LlmBacktestTarget> }) {
  const entries = Object.entries(targets);
  if (!entries.length) {
    return (
      <div className="llm-target-grid is-empty">
        <Info size={14} />
        <span>待评分：回测报告还没有返回可用标的结果。</span>
      </div>
    );
  }
  return (
    <div className="llm-target-grid">
      {entries.map(([key, target]) => (
        <div key={key}>
          <span>{target.label || key}</span>
          <StatusPill tone={target.scoring_eligible ? "good" : target.posterior_status === "pending_future_prices" ? "neutral" : "warn"}>
            {statusLabel(target.posterior_status ?? target.status)}
          </StatusPill>
          <strong>{target.actual_direction}</strong>
          <small>
            价格点 {target.points}
            {target.change_pct === undefined ? "" : ` · ${target.change_pct > 0 ? "+" : ""}${target.change_pct}%`}
          </small>
          {target.start && target.end ? <small>{target.start} 至 {target.end}</small> : null}
          {target.latest_available ? <small>最新 {target.latest_available}</small> : null}
          {target.evidence_gap ? <small>{target.evidence_gap}</small> : null}
        </div>
      ))}
    </div>
  );
}

function DocIdRow({ ids }: { ids: string[] }) {
  if (!ids.length) {
    return (
      <div className="doc-id-row is-empty">
        <FileText size={13} />
        <span>缺少引用证据</span>
      </div>
    );
  }
  return (
    <div className="doc-id-row">
      <ShieldCheck size={13} />
      <span>引用证据 {ids.length} 条</span>
    </div>
  );
}

function formatDate(value: string) {
  return value ? value.slice(0, 10) : "时间待补";
}
