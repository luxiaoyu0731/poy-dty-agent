import type {
  SevenProductEvaluationBatch,
  SevenProductEvaluationCell,
  SevenProductForecastBatch,
  SevenProductForecastCell,
  SevenProductForecastLedgerBatch,
  SevenProductTarget
} from "../services/api";
import { EvidenceDossierButton } from "../features/evidence-system/EvidenceDossierButton";
import { formatDisplayTimestamp, formatTimestampsInText } from "../utils/displayFormatting";
import { StatusPill } from "./ui";

const TARGET_LABELS: Record<SevenProductTarget, string> = {
  crude: "原油",
  naphtha: "石脑油",
  px: "PX",
  pta: "PTA",
  meg: "MEG",
  poy: "POY",
  dty: "DTY"
};

const DIRECTION_LABELS: Record<SevenProductForecastCell["direction"], string> = {
  up: "上涨",
  down: "下跌",
  neutral: "中性",
  uncertain: "无法判断"
};

const STATUS_LABELS: Record<SevenProductForecastCell["formal_status"], string> = {
  formal: "正式",
  low_confidence: "低置信正式",
  reference: "参考",
  degraded: "降级参考",
  insufficient_data: "数据不足",
  model_unavailable: "不可用"
};

const DATA_STATUS_LABELS: Record<SevenProductForecastCell["data_status"], string> = {
  fresh: "数据新鲜",
  stale: "数据滞后",
  proxy: "代理来源",
  insufficient: "历史不足",
  missing: "数据缺失"
};

function formatNumber(value: number | null, unit: string) {
  if (value === null) return "—";
  const unitLabel = ({ "USD/bbl": "美元/桶", "USD/mt": "美元/吨", "CNY/mt": "元/吨" } as Record<string, string>)[unit] ?? unit;
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)} ${unitLabel}`;
}

function shanghaiTime(value: string) {
  const time = formatDisplayTimestamp(value);
  return time ? `${time}（上海）` : "时间未返回";
}

function forecastExplanation(cell: SevenProductForecastCell) {
  if (/[\u4e00-\u9fff]/.test(cell.status_reason)) return formatTimestampsInText(cell.status_reason);
  if (cell.data_status === "missing") return "当前预测基准缺少可用价格，不能形成可靠预测。";
  if (cell.data_status === "stale") return "所用价格已超出该基准的时效要求，当前仅保留历史参考。";
  if (cell.data_status === "proxy") return "当前使用代理基准，请与行情页的报价口径分别核对。";
  if (cell.history_points < 20) return `当前只有 ${cell.history_points} 个历史价格点，模型证据不足，仅作降级参考。`;
  if (cell.formal_status === "formal" || cell.formal_status === "low_confidence") return "该预测已通过当前正式证据门禁。";
  return "模型或样本外证据尚未通过正式门禁，当前预测仅供观察。";
}

function cellTone(cell: SevenProductForecastCell): "good" | "warn" | "bad" | "accent" {
  if (cell.formal_status === "formal") return "good";
  if (cell.formal_status === "model_unavailable") return "bad";
  if (cell.formal_status === "insufficient_data" || cell.formal_status === "degraded") return "warn";
  return "accent";
}

function cellFor(batch: SevenProductForecastBatch, target: SevenProductTarget, horizon: 1 | 7 | 30) {
  return batch.cells.find((cell) => cell.target === target && cell.horizon_days === horizon);
}

function ForecastCell({
  cell,
  evaluation,
  batchId
}: {
  cell: SevenProductForecastCell | undefined;
  batchId: string;
  evaluation: SevenProductEvaluationCell | undefined;
}) {
  if (!cell) return <span className="seven-product-missing-cell">合同缺格</span>;
  const evidence = cell.evidence[cell.evidence.length - 1];
  const latestOutcome = evaluation?.recent_outcomes?.[evaluation.recent_outcomes.length - 1];
  return (
    <div
      className="seven-product-cell"
      data-horizon={cell.horizon_days}
      data-target={cell.target}
      data-testid="seven-product-forecast-cell"
    >
      <div className="seven-product-cell-head">
        <strong>{formatNumber(cell.point_forecast, cell.unit)}</strong>
        <StatusPill tone={cellTone(cell)}>{STATUS_LABELS[cell.formal_status]}</StatusPill>
      </div>
      <span>
        {DIRECTION_LABELS[cell.direction]} · {cell.predicted_change_pct === null ? "—" : `${(cell.predicted_change_pct * 100).toFixed(2)}%`}
      </span>
      <small>区间 {formatNumber(cell.interval_low, cell.unit)} – {formatNumber(cell.interval_high, cell.unit)}</small>
      <small>参考评分 {Math.round(cell.confidence * 100)}%（非正确率） · {DATA_STATUS_LABELS[cell.data_status]} · 历史点 {cell.history_points}</small>
      <details>
        <summary>依据与缺口</summary>
        <p>{forecastExplanation(cell)}</p>
        <small>预测基准：{cell.label_series_id}。新鲜度只针对该基准，不代表行情页的其他报价或全部历史数据。</small>
        <details>
          <summary>技术诊断</summary>
          <p>{cell.status_reason}</p>
          {cell.key_drivers.map((driver) => <code key={driver}>{driver}</code>)}
          {cell.data_gaps.map((gap) => <code key={gap}>{gap}</code>)}
        </details>
        {evaluation ? (
          <div className="seven-product-evaluation-detail">
            <strong>样本外评测：{evaluation.promotion_eligible ? "通过" : "未通过"}</strong>
            <small>有效样本 {evaluation.effective_sample_count}</small>
            <small>误差改善 {evaluation.error_improvement === null ? "—" : `${(evaluation.error_improvement * 100).toFixed(1)}%`}</small>
            <small>方向准确率 {evaluation.direction_accuracy === null ? "—" : `${(evaluation.direction_accuracy * 100).toFixed(1)}%`}</small>
            {latestOutcome ? (
              <>
                <small data-testid="seven-product-latest-outcome">
                  最近样本外：预测 {formatNumber(latestOutcome.candidate, cell.unit)} · 实际 {formatNumber(latestOutcome.actual, cell.unit)} ·
                  绝对误差 {formatNumber(latestOutcome.absolute_error, cell.unit)} · {latestOutcome.direction_hit ? "方向命中" : "方向未命中"}
                </small>
                <small data-testid="seven-product-latest-outcome-times">
                  原点观测 <time dateTime={latestOutcome.origin_observed_at}>{shanghaiTime(latestOutcome.origin_observed_at)}</time> ·
                  原点可见 <time dateTime={latestOutcome.origin_visible_at}>{shanghaiTime(latestOutcome.origin_visible_at)}</time> ·
                  实际观测 <time dateTime={latestOutcome.actual_observed_at}>{shanghaiTime(latestOutcome.actual_observed_at)}</time> ·
                  实际可见 <time dateTime={latestOutcome.actual_visible_at}>{shanghaiTime(latestOutcome.actual_visible_at)}</time>
                </small>
              </>
            ) : <small>尚无可展示的历史样本外预测与实际结果。</small>}
            {evaluation.gate_reasons.length ? <details><summary>评测诊断</summary>{evaluation.gate_reasons.map((reason) => <code key={reason}>{reason}</code>)}</details> : null}
          </div>
        ) : <small>尚未读取样本外评测。</small>}
        <EvidenceDossierButton label="发行时完整证明" initialTarget={cell.target} initialHorizon={cell.horizon_days} initialView="issued" context={{batch_id: batchId}} />
        {evidence?.source_url ? (
          <>
            <a href={evidence.source_url} rel="noreferrer" target="_blank">
              最近证据：{evidence.source_id} · {evidence.observed_at.slice(0, 10)}
            </a>
            <small>首次可见 {shanghaiTime(evidence.visible_at)}</small>
            {evidence.raw_sha256 ? <code>原件 SHA-256 {evidence.raw_sha256}</code> : <small>该条证据未返回原件哈希。</small>}
          </>
        ) : <small>暂无可点击证据。</small>}
      </details>
    </div>
  );
}

export function SevenProductForecastGrid({
  batch,
  evaluation
}: {
  batch: SevenProductForecastBatch;
  evaluation: SevenProductEvaluationBatch | null;
}) {
  return (
    <div className="seven-product-forecast" data-testid="seven-product-forecast-grid">
      <div className="seven-product-summary" data-testid="seven-product-contract-summary" role="status">
        <span>合同 <strong>{batch.contract_complete ? "21/21 完整" : `${batch.cells.length}/21`}</strong></span>
        <span>正式 <strong>{batch.formal_count}/21</strong></span>
        <span>参考 <strong>{batch.reference_count}/21</strong></span>
        <span>不可用 <strong>{batch.unavailable_count}/21</strong></span>
      </div>
      <div className="seven-product-evaluation-summary">
        <StatusPill tone={evaluation?.overall_status === "passed" ? "good" : "warn"}>
          样本外门禁 {evaluation ? `${evaluation.passed_count}/21` : "待读取"}
        </StatusPill>
        <span>逐格要求：误差优于最佳朴素基线 ≥5%，方向准确率 ≥55%，且点时/样本量门禁通过。</span>
        {evaluation ? (
          <small data-testid="seven-product-evaluation-contract">
            评测合同 {evaluation.schema_version} · 策略 {evaluation.evaluation_policy_version}
          </small>
        ) : null}
      </div>
      <p className="seven-product-boundary">{batch.customer_boundary}</p>
      <div className="seven-product-grid-scroll">
        <table className="seven-product-grid">
          <thead>
            <tr>
              <th scope="col">产品</th>
              {batch.horizons.map((horizon) => <th key={horizon} scope="col">{horizon} 天</th>)}
            </tr>
          </thead>
          <tbody>
            {batch.targets.map((target) => (
              <tr data-target={target} data-testid="seven-product-row" key={target}>
                <th scope="row">
                  <strong>{TARGET_LABELS[target]}</strong>
                  <small>{cellFor(batch, target, 1)?.label_series_id ?? "合同缺格"}</small>
                </th>
                {batch.horizons.map((horizon) => (
                  <td key={`${target}-${horizon}`}>
                    <ForecastCell
                      batchId={batch.batch_id}
                      cell={cellFor(batch, target, horizon)}
                      evaluation={evaluation?.cells.find((cell) => cell.target === target && cell.horizon_days === horizon)}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <small className="muted-copy">截至 {shanghaiTime(batch.as_of_time)}</small>
      <details><summary>预测追溯信息</summary><small>批次 <code>{batch.batch_id}</code></small></details>
    </div>
  );
}

export function SevenProductIssuedHistory({
  history
}: {
  history: SevenProductForecastLedgerBatch[];
}) {
  const latest = history[0];
  if (!latest) {
    return <p className="muted-copy">尚无真实发布时间冻结的预测；每日生产任务首次成功后会在这里形成账本。</p>;
  }
  const scored = latest.cells.filter((cell) => cell.settlement_status === "scored").length;
  return (
    <div className="seven-product-issued-history" data-testid="seven-product-issued-history">
      <div className="seven-product-evaluation-summary">
        <StatusPill tone={scored ? "good" : "accent"}>已结算 {scored}/21</StatusPill>
        <span>
          已冻结 {history.length} 个生产批次；最新批次 {latest.business_date}，预测值、实际值和误差均来自不可变账本。
        </span>
      </div>
      <div className="seven-product-grid-scroll">
        <table className="seven-product-history-table">
          <thead>
            <tr>
              <th>产品</th>
              <th>周期</th>
              <th>发布预测</th>
              <th>实际结果</th>
              <th>绝对误差</th>
              <th>方向</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {latest.cells.map((cell) => (
              <tr key={cell.cell_id}>
                <td>{TARGET_LABELS[cell.forecast.target]}</td>
                <td>{cell.forecast.horizon_days} 天 <EvidenceDossierButton label="当时证明" initialTarget={cell.forecast.target} initialHorizon={cell.forecast.horizon_days} initialView="issued" context={{batch_id: latest.batch_id}} /></td>
                <td>{formatNumber(cell.forecast.point_forecast, cell.forecast.unit)}</td>
                <td>{formatNumber(cell.invalidation ? null : cell.outcome?.actual_value ?? null, cell.forecast.unit)}</td>
                <td>{formatNumber(cell.invalidation ? null : cell.outcome?.absolute_error ?? null, cell.forecast.unit)}</td>
                <td>
                  {DIRECTION_LABELS[cell.forecast.direction]}
                  {cell.outcome && !cell.invalidation ? ` / ${DIRECTION_LABELS[cell.outcome.actual_direction]}` : ""}
                </td>
                <td>
                  {cell.settlement_status === "scored"
                    ? cell.outcome?.direction_hit ? "已结算·命中" : "已结算·未命中"
                    : cell.settlement_status === "pending"
                      ? "等待到期"
                      : cell.settlement_status === "invalidated_contract_mismatch"
                        ? "已失效·标签合同不匹配"
                        : "发布时不可评分"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details><summary>账本追溯信息</summary><small>账本批次 <code>{latest.batch_id}</code> · SHA-256 <code>{latest.payload_sha256}</code></small></details>
    </div>
  );
}

export function PredictionWriteUnavailable() {
  return (
    <p className="muted-copy">
      正式预测批次只能由受治理的内部流程生成；当前页面不提供写入入口。
    </p>
  );
}
