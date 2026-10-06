import type { EvidenceDossier, EvidenceEventChain, EvidenceTarget } from "./types";
import { formatDisplayTimestamp } from "../../utils/displayFormatting";
import { semanticTimeLabel } from "./SemanticEvidenceCard";
import { conditionalReviews } from "./semanticReviews";
import "./EvidenceEventGraph.css";

const products: Record<EvidenceTarget, string> = { crude: "原油", naphtha: "石脑油", px: "PX", pta: "PTA", meg: "MEG", poy: "POY", dty: "DTY" };
const mechanisms: Record<string, string> = { supply: "供应与装置", demand: "需求与采购", inventory: "库存与去化", logistics: "运输与物流", transport: "运输与物流", policy: "政策与贸易", cost: "成本传导", price: "价格背景" };
const states: Record<EvidenceEventChain["state"], string> = { actual: "材料称已发生", planned: "材料称计划实施", unconfirmed: "材料陈述未确认", denied: "材料含否认或更正", in_progress: "材料称进行中", unknown: "材料状态未明确" };

function sourceUrl(value: string) {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}

function Source({ title, url }: { title: string; url: string }) {
  const href = sourceUrl(url);
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{title || "查看原始来源"} ↗</a> : <span>{title || "原始来源链接不可用"}</span>;
}

function EventChain({ chain }: { chain: EvidenceEventChain }) {
  const upstream = chain.relation === "upstream_context";
  const review = chain.proof_kind === "semantic_review" ? chain.semantic_review : null;
  // Capabilities and upstream hypotheses never grant evidence admission. Require
  // both the server admission flag and a checked direct mechanism to label it so.
  const admitted = !upstream && chain.counts_as_evidence && chain.semantic_status === "rule_checked" && chain.state === "actual" && Boolean(chain.event_date);
  return <article className={`eeg-chain ${upstream ? "is-context" : "is-direct"}`} data-testid="evidence-event-chain">
    <div className="eeg-chain-boundary">
      <span>{review ? "AI 复核 · 条件性机制假说" : upstream ? "上游关联 · 条件性机制假说" : "本品种事件材料"}</span>
      <b>{admitted ? "已通过机制规则 · 计入证据" : "不计正反证 · 无计票权"}</b>
    </div>
    <div className="eeg-chain-flow">
      <section className="eeg-node eeg-event">
        <small>事件材料</small><h4>{chain.event_label || chain.source_title || "事件陈述"}</h4>
        <span className="eeg-state">{review ? semanticTimeLabel(review) : states[chain.state] ?? "材料状态未明确"}</span>
        <p>{chain.quote}</p>
        <Source title={chain.source_title} url={chain.source_url} />
      </section>
      <span className="eeg-arrow" aria-hidden="true">→</span>
      <section className="eeg-node eeg-mechanism">
        <small>{upstream ? "条件性机制假说" : "机制核验"}</small><h4>{mechanisms[chain.mechanism] ?? "其他事件机制"}</h4>
        <span className="eeg-state">{review ? "AI 条件解释 · 传导未获证实" : upstream ? "传导未获证实" : chain.semantic_status === "rule_checked" ? "机制规则已核验" : "机制方向待核验"}</span>
        {review ? <p><strong>{review.direction === "up" ? "条件上行压力：" : "条件下行压力："}</strong>{review.rationale}</p> : null}
        <ul>{chain.conditions.length ? chain.conditions.map((condition, i) => <li key={i}>{condition}</li>) : <li>尚无充分条件，不推定传导成立</li>}</ul>
        {admitted && chain.direction ? <p>机制方向压力：{chain.direction === "up" ? "偏强" : "偏弱"}，非预测方向</p> : <p>尚不判定方向</p>}
      </section>
      <span className="eeg-arrow" aria-hidden="true">→</span>
      <section className="eeg-node eeg-products">
        <small>{upstream ? "可能传导路径" : "关联品种路径"}</small>
        <ol aria-label="品种路径">{chain.path.length ? chain.path.map((step, i) => <li key={`${step.target}-${i}`}><strong>{products[step.target] ?? "未知品种"}</strong>{step.label ? <span>{step.label}</span> : null}</li>) : <li>未建立可追溯的品种路径</li>}</ol>
        <p>资料关联，不替代已发行判断</p>
        {review ? <a href={`#evb-semantic-${review.review_id}`}>核对同一条支持／相反依据</a> : null}
      </section>
    </div>
    <details className="eeg-source-details"><summary>核对时间与原文出处</summary>
      <dl><dt>发生／报告期</dt><dd>{review ? semanticTimeLabel(review) : chain.event_date ?? "尚未明确"}{chain.event_date_source === "dated_price_report_title" ? "（报告标题日期）" : ""}</dd>
        <dt>发布时间</dt><dd>{formatDisplayTimestamp(chain.published_at)}</dd>
        <dt>{review ? "复核可用时间" : "系统获知时间"}</dt><dd>{formatDisplayTimestamp(chain.known_at)}</dd></dl>
      <blockquote>{chain.quote}</blockquote><Source title={chain.source_title} url={chain.source_url} />
    </details>
  </article>;
}

export function EvidenceEventGraph({ dossier }: { dossier: EvidenceDossier }) {
  const materials = (dossier.event_chains ?? []).filter(chain => chain.mechanism !== "price");
  const unreviewed = materials.filter(chain => chain.proof_kind === "unreviewed_material");
  const chains = materials.filter(chain => chain.proof_kind !== "unreviewed_material");
  const conditionalCount = conditionalReviews(dossier).length;
  const prices = dossier.claims.filter(claim => claim.mechanism === "price");
  return <div className="eeg-graph" data-testid="evidence-event-graph">
    <h4>事件材料 → 机制条件 → 品种路径</h4>
    <p className="eeg-boundary">待核验材料可展示其陈述；展示不代表事实已核实。上游传导是条件性机制假说，不计正反证或方向票数。</p>
    {chains.length ? chains.map(chain => <EventChain key={chain.chain_id} chain={chain} />) : conditionalCount ? <p className="eeg-empty">另有 {conditionalCount} 条原文绑定的 AI 条件材料，见下方支持依据、相反依据与虚线关系图；尚未转正为直接机制证据。</p> : <p className="eeg-empty">{dossier.event_chains === undefined ? "本档未提供事件机制链；仍可核对下方已核验证据关系。" : "本轮没有可展示的非报价事件材料；不据空图推定没有事件。"}</p>}
    {unreviewed.length ? <details className="eeg-market-background">
      <summary>待复核原始材料 · {unreviewed.length} 条（尚未建立传导路径）</summary>
      <p>以下保留来源原文；系统尚未完成主体、动作与机制对应关系，不按关键词生成事件状态或品种路径。</p>
      {unreviewed.map(chain => <article key={chain.chain_id}><p>{chain.quote}</p><Source title={chain.source_title} url={chain.source_url} /><small>发布 {formatDisplayTimestamp(chain.published_at)}</small></article>)}
    </details> : null}
    <details className="eeg-market-background"><summary>报价与市场背景 · {prices.length} 条（不进入事件链）</summary>
      <p>报价只作价格背景，不自动判定供应、需求或库存方向。</p>
      {prices.length ? prices.map(claim => <article key={claim.claim_id}><p>{claim.quote}</p><Source title={claim.source_title} url={claim.source_url} /><small>发生／报告期 {claim.event_date ?? "尚未明确"} · 发布 {formatDisplayTimestamp(claim.published_at)}</small></article>) : <p>本页没有报价材料。</p>}
    </details>
  </div>;
}
