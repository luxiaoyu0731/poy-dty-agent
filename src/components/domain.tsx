import type { PredictionEventFactorRow } from "../services/api";
import { formatDisplayTimestamp } from "../utils/displayFormatting";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  MessageSquare
} from "lucide-react";
import type {
  EventImpact,
  FactorScore,
  FormalPredictionBatch,
  IndustryObservation,
  LatestPricesResponse,
  MarketObservation,
  NewsArticle,
  NewsEventCluster,
  NewsSource,
  PriceComparison,
  PredictionEventFactors,
  PredictionReview,
  RagEvidence,
  SourceReadiness,
  StoredPrediction
} from "../services/api";
import { chainLatestValue, type ChainNodeName } from "./chainLatest";
import { categoryLabel, evidenceLevelLabel, factorSymbolLabel, productListLabel } from "./displayLabels";
import { MetricTile } from "./managerUi";
import { articleChineseSummary, clusterChineseSummary, clusterSourceUrl } from "./newsSummary";
import { AuditDrawer, EventCard, displaySourceName, readinessLabel, readinessTone, shortText } from "./researchUi";
import { DataTable, EmptyState, Panel, StatusPill, cx } from "./ui";

export function formatNumber(value?: number | null, suffix = "") {
  if (value === undefined || value === null || Number.isNaN(value)) return "--";
  return `${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${suffix}`;
}

export function toneForDirection(direction?: string) {
  if (direction === "利多") return "good";
  if (direction === "利空") return "bad";
  return "neutral";
}

export function toneForTier(tier?: string) {
  if (tier === "A") return "good";
  if (tier === "B") return "accent";
  if (tier === "C") return "warn";
  return "neutral";
}

export function SourceHealthList({ readiness }: { readiness: SourceReadiness[] }) {
  const rows = readiness.slice(0, 9);
  if (!rows.length) {
    return <EmptyState title="来源登记不可用">来源登记完成后会显示每个来源的状态和下一步动作。</EmptyState>;
  }
  return (
    <div className="source-health-list">
      {rows.map((source) => (
        <article key={source.source_id}>
          <div>
            <strong>{displaySourceName(source.source_id)}</strong>
            <StatusPill tone={readinessTone(source.status)}>{readinessLabel(source.status)}</StatusPill>
          </div>
          <p>{source.status === "ready" ? "已可用于研究数据更新。" : sourceActionCopy(source.status)}</p>
          <small>核心数据源 · {evidenceLevelLabel(source.tier)}</small>
          {source.next_step ? (
            <AuditDrawer title="来源详情" summary="接入说明">
              <div className="audit-kv">
                <span>来源</span><code>{displaySourceName(source.source_id)}</code>
                <span>状态</span><code>{readinessLabel(source.status)}</code>
                <span>说明</span><code>{sourceActionCopy(source.status)}</code>
              </div>
            </AuditDrawer>
          ) : null}
        </article>
      ))}
    </div>
  );
}

export function FactorContribution({ factors }: { factors: FactorScore[] }) {
  if (!factors.length) {
    return <EmptyState title="暂无因子贡献">形成观测后会显示原油、PX、PTA、MEG 等因子的贡献。</EmptyState>;
  }
  const max = Math.max(1, ...factors.map((factor) => Math.abs(factor.contribution)));
  return (
    <div className="factor-list">
      {factors.slice(0, 8).map((factor) => (
        <article key={`${factor.name}-${factor.symbol}`}>
          <div className="factor-row-head">
            <span>{factorSymbolLabel(factor.symbol)}</span>
            <strong>{factor.name}</strong>
            <StatusPill tone={toneForDirection(factor.direction)}>{factor.direction}</StatusPill>
          </div>
          <div className="factor-bar">
            <i style={{ width: `${Math.min(100, Math.abs(factor.contribution / max) * 100)}%` }} />
          </div>
          <p>{shortText(factor.reason, 92)}</p>
          <small>{factor.route.replace(/->/g, "→")} · {factor.change} · {factor.strength}</small>
        </article>
      ))}
    </div>
  );
}

export function ChainFlow({
  factors,
  marketObservations = [],
  industryObservations = [],
  latestPrices
}: {
  factors: FactorScore[];
  marketObservations?: MarketObservation[];
  industryObservations?: IndustryObservation[];
  latestPrices?: LatestPricesResponse;
}) {
  const nodes: Array<{ id: ChainNodeName; label: ChainNodeName; hint: string }> = [
    { id: "原油", label: "原油", hint: "Brent / WTI" },
    { id: "石脑油", label: "石脑油", hint: "裂解成本" },
    { id: "PX", label: "PX", hint: "芳烃链" },
    { id: "PTA", label: "PTA", hint: "聚酯主原料" },
    { id: "MEG", label: "MEG", hint: "聚酯辅原料" },
    { id: "POY", label: "POY", hint: "长丝成本端" },
    { id: "DTY", label: "DTY", hint: "弹力丝传导" }
  ];
  const byName = new Map(factors.map((factor) => [factor.symbol.toLowerCase(), factor]));
  return (
    <div className="chain-flow">
      {nodes.map((node, index) => {
        const factor = byName.get(node.id.toLowerCase()) ?? byName.get(node.label.toLowerCase());
        const latest = chainLatestValue(node.label, marketObservations, industryObservations, latestPrices);
        return (
          <div className="chain-node-wrap" key={node.id}>
            <article className={cx("chain-node", factor ? `tone-${toneForDirection(factor.direction)}` : "")}>
              <span>{node.hint}</span>
              <strong>{node.label}</strong>
              <div className="chain-latest-block">
                <small className={cx("chain-latest-price", !latest && "is-empty")}>
                  {latest?.value ?? "暂无最新价"}
                </small>
                <small className="chain-latest-date">{latest?.date ?? "暂无日期"}</small>
                <small className={cx("chain-latest-change", latest?.change && `tone-${latest.change.tone}`)}>
                  {latest?.change?.label ?? "变化待补"}
                </small>
                <span className={cx("chain-freshness", latest?.isTransactionPrice ? "is-trade" : "is-valuation")}>
                  {latest ? `${latest.freshnessLabel} · ${latest.quoteTypeLabel}` : "等待采集"}
                </span>
              </div>
            </article>
            {index < nodes.length - 1 ? <ArrowRight className="chain-arrow" size={18} /> : null}
          </div>
        );
      })}
    </div>
  );
}

export function EventRadar({ events }: { events: EventImpact[] }) {
  if (!events.length) {
    return <EmptyState title="暂无结构化事件">导入事件或更新新闻后，会在这里形成事件判断。</EmptyState>;
  }
  return (
    <div className="event-radar">
      {events.slice(0, 6).map((event) => (
        <EventCard
          confidence={event.confidence}
          direction={event.nature}
          key={event.event_id}
          products={event.affected_products}
          status={event.horizon}
          summary={event.judgement}
          tier={event.evidence_level}
          title={event.title}
        />
      ))}
    </div>
  );
}

export function NewsClusterList({
  clusters,
  articles,
  onSelectCluster,
  onSelectArticle
}: {
  clusters: NewsEventCluster[];
  articles?: NewsArticle[];
  onSelectCluster?: (cluster: NewsEventCluster) => void;
  onSelectArticle?: (article: NewsArticle) => void;
}) {
  if (!clusters.length && !articles?.length) {
    return <EmptyState title="暂无新闻事件">更新公开页面或导入新闻 CSV 后，这里会展示聚类和原文线索。</EmptyState>;
  }
  return (
    <div className="news-cluster-list">
      {clusters.slice(0, 8).map((cluster) => {
        const sourceUrl = clusterSourceUrl(cluster, articles);
        return (
          <article key={cluster.cluster_id}>
            <div>
              <strong>{cluster.title}</strong>
              <StatusPill tone={toneForTier(cluster.evidence_level)}>{evidenceLevelLabel(cluster.evidence_level)}</StatusPill>
            </div>
            <p className="news-cn-summary">{clusterChineseSummary(cluster)}</p>
            <small>模型判断 {cluster.direction || "待判断"} · {clusterPriorityLabel(cluster.heat_score)} · {productListLabel(cluster.affected_products, 4)}</small>
            {onSelectCluster || sourceUrl ? (
              <div className="inline-actions">
                {onSelectCluster ? <button onClick={() => onSelectCluster(cluster)} type="button">查看证据</button> : null}
                {sourceUrl ? <a href={sourceUrl} rel="noreferrer" target="_blank">查看原文</a> : null}
              </div>
            ) : null}
          </article>
        );
      })}
      {!clusters.length ? articles?.slice(0, 6).map((article) => (
        <article key={article.article_id}>
          <div>
            <strong>{article.title}</strong>
            <StatusPill tone={toneForTier(article.tier)}>{evidenceLevelLabel(article.tier)}</StatusPill>
          </div>
          <p className="news-cn-summary">{articleChineseSummary(article)}</p>
          <small>{displaySourceName(article.source_id)} · {categoryLabel(article.category)}</small>
          {onSelectArticle || article.url ? (
            <div className="inline-actions">
              {onSelectArticle ? <button onClick={() => onSelectArticle(article)} type="button">查看证据</button> : null}
              {article.url ? <a href={article.url} rel="noreferrer" target="_blank">查看原文</a> : null}
            </div>
          ) : null}
        </article>
      )) : null}
    </div>
  );
}

export function ObservationTable({
  marketObservations,
  industryObservations
}: {
  marketObservations: MarketObservation[];
  industryObservations: IndustryObservation[];
}) {
  const rows: ReactNode[][] = [
    ...marketObservations.slice(0, 8).map((item) => [
      item.observed_at,
      observationProductLabel(item.product),
      observationIndicatorLabel(item.indicator),
      observationValueLabel(item.value, item.unit)
    ]),
    ...industryObservations.slice(0, 8).map((item) => [
      item.observed_at,
      observationProductLabel(item.product),
      observationIndicatorLabel(item.metric),
      observationValueLabel(item.value, item.unit)
    ])
  ];
  return (
    <DataTable
      columns={["时间", "品种", "指标", "值"]}
      empty={<EmptyState title="暂无观测数据">拉取 EIA/FRED 或导入 CSV 后会形成观测表。</EmptyState>}
      rows={rows}
    />
  );
}

function observationProductLabel(value: string) {
  const normalized = value.trim().toLowerCase();
  const labels: Record<string, string> = {
    macro: "宏观因子",
    crude_oil: "原油",
    "wti crude oil": "WTI 原油",
    "uk brent crude oil": "Brent 原油",
    brent: "Brent 原油",
    wti: "WTI 原油",
    pta: "PTA",
    px: "PX",
    meg: "MEG",
    poy: "POY",
    dty: "DTY"
  };
  return labels[normalized] ?? value;
}

function observationIndicatorLabel(value: string) {
  const normalized = value.trim().toLowerCase();
  if (normalized === "spot_quote") return "现货评估价";
  if (normalized.includes("trade weighted u.s. dollar")) return "美元指数";
  if (normalized.includes("10-year treasury")) return "美国10年期利率";
  if (normalized.includes("wti crude oil spot")) return "WTI 原油现货";
  if (normalized.includes("brent crude oil spot") || normalized.includes("europe brent spot")) return "Brent 原油现货";
  if (normalized.includes("cushing")) return "WTI 库欣现货";
  return value;
}

function observationValueLabel(value?: number | null, unit?: string | null) {
  const unitLabel = unitLabelText(unit);
  return formatNumber(value, unitLabel ? ` ${unitLabel}` : "");
}

function unitLabelText(unit?: string | null) {
  if (!unit) return "";
  const normalized = unit.trim().toLowerCase();
  const labels: Record<string, string> = {
    dollars_per_barrel: "$/bbl",
    "$/bbl": "$/bbl",
    percent: "%",
    index: "指数",
    "元/吨": "元/吨",
    "cny/mt": "元/t",
    "usd/bbl": "$/bbl"
  };
  return labels[normalized] ?? unit;
}

function clusterPriorityLabel(score: number) {
  if (score >= 85) return "关注优先级高";
  if (score >= 55) return "关注优先级中";
  return "关注优先级低";
}

function sourceActionCopy(status: SourceReadiness["status"]) {
  if (status === "requires_api_key" || status === "requires_license") return "需要补充授权信息。";
  if (status === "internal_only") return "需要人工录入或本地资料补充。";
  if (status === "manual_review") return "需要复核来源口径。";
  return "来源状态待确认。";
}

function formalDirectionLabel(direction?: string) {
  if (direction === "up") return "偏强";
  if (direction === "down") return "偏弱";
  if (direction === "neutral") return "震荡";
  return "方向不确定";
}

function formalPercent(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

// 定案规则口径（ADR-9 单主线）：R1-R5 是内部护栏规则的审计标签，
// 用户可见文案统一用「定案」语言，不出现旧双轨措辞。
const fusionRuleLabel: Record<string, string> = {
  R1: "同向确认",
  R2: "事件改写",
  R3: "分歧保留",
  R4: "弱信号忽略",
  R5: "冲突裁决",
};

export function EventEvidenceCard({ data }: { data: PredictionEventFactors }) {
  if (data.chain_status === "missing" && data.fusion_rows.length === 0) {
    return null;
  }
  const rowsByProduct = new Map<string, Map<number, PredictionEventFactorRow>>();
  for (const row of data.fusion_rows) {
    if (!rowsByProduct.has(row.target)) rowsByProduct.set(row.target, new Map());
    rowsByProduct.get(row.target)!.set(row.horizon_days, row);
  }
  const directionLabel = (direction: string | null | undefined) =>
    direction === "up" ? "偏强" : direction === "down" ? "偏弱" : "震荡";
  const switchCount = data.fusion_rows.filter((row) => row.fusion_rule === "R2").length;
  return (
    <div className="formal-prediction-batch" data-testid="event-evidence-card">
      <section className="ledger-boundary-note">
        <div>
          <strong>事件依据卡（预测定案）</strong>
          <span>
            {data.business_date || "待生成"} · Agent 链 {data.chain_status === "ok" ? "正常" : data.chain_status === "degraded" ? "降级" : "未运行"}
            {data.selected_count ? ` · 当日候选 ${data.selected_count}` : ""}
            {data.input_sha256 ? ` · 冻结 ${data.input_sha256}` : ""}
          </span>
        </div>
        <StatusPill tone={switchCount ? "warn" : "neutral"}>
          {switchCount ? `事件改写 ${switchCount} 格` : "安静日零改写 = 与纯价格基准一致"}
        </StatusPill>
      </section>
      <div className="event-evidence-rows">
        {[...rowsByProduct.entries()].map(([product, horizons]) => (
          <article key={product}>
            <strong>{product.toUpperCase()}</strong>
            <div className="event-evidence-horizons">
              {([1, 7, 30] as const).map((horizon) => {
                const row = horizons.get(horizon);
                if (!row) return <small key={horizon}>{horizon}天 待定案</small>;
                const switched = row.fusion_rule === "R2";
                return (
                  <small key={horizon}>
                    {horizon}天 纯价格 {directionLabel(row.baseline_direction)} → 定案{" "}
                    <strong>{directionLabel(row.event_adjusted_direction)}</strong>{" "}
                    ({fusionRuleLabel[row.fusion_rule] ?? row.fusion_rule})
                    {switched && row.switch_reason ? ` · ${shortText(row.switch_reason, 72)}` : ""}
                  </small>
                );
              })}
            </div>
          </article>
        ))}
      </div>
      {data.political.length > 0 && (
        <AuditDrawer title="政治分析摘要" summary={`${data.political.length} 个事件的逐案推理`}>
          <div className="audit-kv">
            {data.political.slice(0, 6).map((item) => (
              <span key={item.event_id}>
                {data.event_titles[item.event_id] || item.event_id}
              </span>
            ))}
          </div>
          <ul>
            {data.political.slice(0, 6).map((item) => (
              <li key={item.event_id}>
                <strong>{data.event_titles[item.event_id] || item.event_id}</strong>
                ：{shortText(item.reasoning, 140)}
                {item.execution_probability != null && `（执行概率 ${Math.round(item.execution_probability * 100)}%）`}
              </li>
            ))}
          </ul>
        </AuditDrawer>
      )}
      {data.analog.length > 0 && (
        <AuditDrawer title="历史类比数字" summary={`${data.analog.length} 个事件的先验`}>
          <ul>
            {data.analog.slice(0, 6).map((item) => (
              <li key={item.event_id}>
                <strong>{data.event_titles[item.event_id] || item.event_id}</strong>
                ：先验 {item.prior_direction ?? "无"} × {item.support_count ?? 0} 例
                {item.analogs[0]?.d7_pct != null && `，类比案例 D+7 实际 ${item.analogs[0].d7_pct}%`}
                {item.analog_validity === "no_prior" && "（无先例）"}
              </li>
            ))}
          </ul>
        </AuditDrawer>
      )}
    </div>
  );
}

export function FormalPredictionBatchView({ batches }: { batches: FormalPredictionBatch[] }) {
  const batch = batches[0];
  if (!batch) {
    return <EmptyState title="暂无正式预测批次">只有通过正式证据门禁并冻结到日度快照的批次才会显示在这里。</EmptyState>;
  }
  const schemaVersion = (batch.payload as { schema_version?: string } | undefined)?.schema_version;
  if (!batch.payload || schemaVersion !== "seven-product-forecast.v1") {
    return (
      <div className="formal-prediction-batch" data-testid="formal-prediction-batch-metadata-only">
        <section className="ledger-boundary-note is-pending-confirmation">
          <AlertTriangle aria-hidden="true" size={18} />
          <div>
            <strong>历史合同记录，不属于当前正式预测</strong>
            <span>旧标量或 Phase A 批次只作审计；只有 seven-product-forecast.v1 的完整 21 格才能显示为当前正式结果。</span>
          </div>
        </section>
      </div>
    );
  }
  const targetCells = new Map(
    batch.payload.cells
      .filter((cell) => cell.node_id === "poy_dty_upstream_cost_pressure")
      .map((cell) => [cell.horizon_days, cell])
  );
  return (
    <div className="formal-prediction-batch" data-testid="formal-prediction-batch">
      <section className="ledger-boundary-note">
        <SafetyCertificateOutlinedFallback />
        <div>
          <strong>正式批次已通过治理证明复核</strong>
          <span>{batch.business_date} · 数据快照已绑定 · 结果按 1 / 7 / 30 天分别冻结</span>
        </div>
        <StatusPill tone="good">证明已复核</StatusPill>
      </section>
      <div className="ledger-summary-strip">
        {([1, 7, 30] as const).map((horizon) => {
          const cell = targetCells.get(horizon);
          return (
            <article data-testid={`formal-horizon-${horizon}`} key={horizon}>
              <span>{horizon} 天正式判断</span>
              <strong>{formalDirectionLabel(cell?.direction)}</strong>
              <small>
                置信度 {formalPercent(cell?.confidence)} · 数据完整度 {formalPercent(cell?.data_completeness)}
              </small>
              <small>{cell?.scoreability === "scorable" ? "可进入到期复盘" : "当前不可评分"}</small>
            </article>
          );
        })}
      </div>
      <AuditDrawer title="正式批次治理信息" summary="查看批次、快照与子目标">
        <div className="audit-kv">
          <span>批次</span><code>{batch.prediction_batch_id}</code>
          <span>修订</span><code>{batch.revision_id}</code>
          <span>证据评估</span><code>{batch.assessment_id}</code>
          <span>数据快照</span><code>{batch.data_snapshot_id}</code>
          <span>内容指纹</span><code>{batch.payload_sha256.slice(0, 16)}…</code>
          <span>POY / DTY</span><code>{([1, 7, 30] as const).map((horizon) => {
            const subtargets = targetCells.get(horizon)?.subtarget_results ?? [];
            return `${horizon}天 ${subtargets.map((item) => `${item.target.toUpperCase()} ${formalDirectionLabel(item.direction)}`).join("、") || "待形成"}`;
          }).join("；")}</code>
        </div>
      </AuditDrawer>
    </div>
  );
}

function SafetyCertificateOutlinedFallback() {
  return <span aria-hidden="true">✓</span>;
}

export function PredictionList({
  predictions,
  onSelect
}: {
  predictions: StoredPrediction[];
  onSelect?: (prediction: StoredPrediction) => void;
}) {
  if (!predictions.length) {
    return <EmptyState title="暂无预测账本记录">写入预测后，系统会保留依据、反证和证据版本。</EmptyState>;
  }
  return (
    <div className="prediction-list">
      {predictions.slice(0, 8).map((prediction) => (
        <article key={prediction.prediction_id}>
          <div>
            <strong>{predictionTargetLabel(prediction.target)}</strong>
            <StatusPill tone="warn">历史记录 · 非正式</StatusPill>
          </div>
          <p>{prediction.direction || "等待确认"} · {prediction.horizon} · 置信度 {Math.round(prediction.confidence * 100)}%</p>
          <small>{shortText(predictionRationaleLabel(prediction.rationale), 96)}</small>
          {onSelect ? (
            <div className="inline-actions">
              <button onClick={() => onSelect(prediction)} type="button">查看详情</button>
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function predictionTargetLabel(value: string) {
  if (value.toLowerCase().includes("poy/dty upstream cost pressure")) return "POY/DTY 上游成本压力";
  return value;
}

function predictionRationaleLabel(value: string) {
  if (value.trim().toLowerCase() === "via vite proxy") return "通过本地前端写入";
  return value;
}

export function ReviewList({ reviews }: { reviews: PredictionReview[] }) {
  if (!reviews.length) {
    return <EmptyState title="暂无复盘结果">到期预测执行复盘后会显示方向验证和权重调整。</EmptyState>;
  }
  return (
    <div className="review-list">
      {reviews.slice(0, 8).map((review) => (
        <article key={review.prediction_id}>
          <div>
            <strong>{review.expected_direction}</strong>
            <StatusPill tone={review.verdict.includes("命中") ? "good" : "warn"}>{review.verdict}</StatusPill>
          </div>
          <p>{review.learning}</p>
          <small>偏差 {review.deviation ?? "待评分"} · 指数 {review.actual_index}</small>
        </article>
      ))}
    </div>
  );
}

export function PriceComparisonView({ priceComparison }: { priceComparison?: PriceComparison }) {
  if (!priceComparison?.summaries.length) {
    return <EmptyState title="暂无价格对照">拉取价格历史后会展示 Brent/WTI 的复盘基准。</EmptyState>;
  }
  return (
    <div className="price-grid">
      {priceComparison.summaries.map((series) => (
        <article key={series.series_id}>
          <div>
            <strong>{series.label}</strong>
            <StatusPill tone={series.change_pct && series.change_pct > 0 ? "good" : series.change_pct && series.change_pct < 0 ? "bad" : "neutral"}>
              {series.verdict}
            </StatusPill>
          </div>
          <b>{series.change_pct == null ? "--" : `${series.change_pct > 0 ? "+" : ""}${series.change_pct}%`}</b>
          <small>{series.first_value ?? "--"} {"→"} {series.last_value ?? "--"} · {series.points.length} 个价格点</small>
        </article>
      ))}
      <p className="price-conclusion">{priceComparison.conclusion}</p>
    </div>
  );
}

export function EvidenceList({
  items,
  onReview
}: {
  items: RagEvidence[];
  onReview?: (docId: string, status: "reviewed" | "rejected") => void;
}) {
  if (!items.length) {
    return <EmptyState title="暂无待审证据">检索结果、新闻、观测和预测记录会进入证据确认队列。</EmptyState>;
  }
  return (
    <div className="evidence-list">
      {items.slice(0, 10).map((item) => (
        <article key={item.doc_id}>
          <div>
            <strong>{item.title}</strong>
            <StatusPill tone={toneForTier(item.tier)}>{evidenceLevelLabel(item.tier)}</StatusPill>
          </div>
          <small>证据 · {displaySourceName(item.source_id)} · {formatDisplayTimestamp(item.observed_at) ?? "时间待补"}</small>
          <p>{item.snippet || item.summary}</p>
          {item.risk_flags.length ? <em><AlertTriangle size={13} />{item.risk_flags.join(" / ")}</em> : null}
          {onReview ? (
            <div className="inline-actions">
              <button onClick={() => onReview(item.doc_id, "reviewed")} type="button">采纳为依据</button>
              <button onClick={() => onReview(item.doc_id, "rejected")} type="button">排除本次判断</button>
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

export function WorkspaceSummary({
  overview,
  readiness,
  newsSources,
  predictions,
  events
}: {
  overview?: { cost_pressure_index: number; status: string; confidence?: number };
  readiness: SourceReadiness[];
  newsSources: NewsSource[];
  predictions: StoredPrediction[];
  events: EventImpact[];
}) {
  const ready = readiness.filter((source) => source.status === "ready").length;
  const totalSources = readiness.length + newsSources.length;
  return (
    <div className="metric-grid">
      <MetricTile
        caption={overview?.confidence !== undefined ? `证据置信 ${Math.round(overview.confidence * 100)}%` : "等待更多证据形成置信度"}
        label="成本压力指数（综合）"
        tone="accent"
        trend={overview?.status ?? "中性"}
        value={overview ? overview.cost_pressure_index : "--"}
      />
      <MetricTile
        caption={`核心 ${ready}/${readiness.length || "--"} 可用 · 新闻源 ${newsSources.length || "--"}`}
        label="来源总览"
        tone={totalSources ? "good" : "warn"}
        trend={totalSources ? "已登记" : "待接入"}
        value={totalSources || "--"}
      />
      <MetricTile
        caption={events.length ? "进入事件雷达" : "更新新闻后生成事件样本"}
        label="事件样本"
        tone={events.length ? "accent" : "warn"}
        trend={events.length ? "活跃" : "待处理"}
        value={events.length}
      />
      <MetricTile
        caption={predictions.length ? "可进入预测复盘" : "保存判断后形成复盘记录"}
        label="预测记录"
        tone={predictions.length ? "good" : "accent"}
        trend={predictions.length ? "活跃" : "待建立"}
        value={predictions.length}
      />
    </div>
  );
}

export function AssistantHint({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <Panel className={className} title="AI 助手建议" eyebrow="evidence first">
      <div className="assistant-hint">
        <MessageSquare size={18} />
        <p>{children}</p>
      </div>
    </Panel>
  );
}

export function TrustBar({ label, value }: { label: string; value?: number }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="trust-bar">
      <span>{label}</span>
      <i><b style={{ width: `${pct}%` }} /></i>
      <strong>{value == null ? "--" : `${pct}%`}</strong>
    </div>
  );
}

export function SourceBoundaryNote() {
  return (
    <div className="boundary-note">
      <span>来源边界</span>
      <strong>仅包含公开信息与授权数据源</strong>
    </div>
  );
}
