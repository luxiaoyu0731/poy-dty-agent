import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { conditionalReviews } from "./semanticReviews";
import { Alert, Button, Empty, Modal, Segmented, Spin, Typography } from "antd";
import { ExpandAltOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps
} from "@xyflow/react";
import { ContentLoading } from "../../components/ContentLoading";
import { api } from "../../services/api";
import type {
  AgentWorkbenchData,
  SevenProductForecastBatch,
  SevenProductForecastCell,
  SevenProductTarget,
  WorkbenchRagVisualResponse
} from "../../services/api";
import { formatDisplayTimestamp } from "../../utils/displayFormatting";
import { EvidenceClaimCard } from "./EvidenceClaimCard";
import { EvidenceEventGraph } from "./EvidenceEventGraph";
import { SemanticEvidenceCard } from "./SemanticEvidenceCard";
import type { EvidenceClaim, EvidenceDossier, EvidenceSemanticReview, EvidenceHistory, EvidenceTarget, EvidenceView } from "./types";

const productOrder: EvidenceTarget[] = ["crude", "naphtha", "px", "pta", "meg", "poy", "dty"];
const productLabels: Record<EvidenceTarget, string> = {
  crude: "原油", naphtha: "石脑油", px: "PX", pta: "PTA", meg: "MEG", poy: "POY", dty: "DTY"
};

const directionLabels: Record<SevenProductForecastCell["direction"], string> = {
  up: "上涨", down: "下跌", neutral: "中性", uncertain: "无法判断"
};
const formalStatusLabels: Record<SevenProductForecastCell["formal_status"], string> = {
  formal: "正式", low_confidence: "低置信正式", reference: "参考", degraded: "降级参考",
  insufficient_data: "数据不足", model_unavailable: "不可用"
};
const dataStatusLabels: Record<SevenProductForecastCell["data_status"], string> = {
  fresh: "数据新鲜", stale: "数据滞后", proxy: "代理来源", insufficient: "历史不足", missing: "数据缺失"
};
const sourceGapLabels: Record<string, string> = {
  body_summary_version_mismatch: "正文与摘要版本不一致",
  quote_not_exact_raw_span: "引文不能逐字对应原文",
  quote_requires_segmentation: "过长引文需要分段核验",
  after_cutoff: "晚于资料截止",
  malformed_source_excluded: "来源结构不可用",
  evidence_capture_failed: "来源读取失败"
};

// 模块卸载后仍在同一次会话内返回时，恢复用户上次的核验对象。
const boardMemory: { target: EvidenceTarget; horizon: 1 | 7 | 30; view: EvidenceView } = {
  target: "poy", horizon: 1, view: "current"
};

// 失败类别分开表达：排队/超时、对象不存在、网络中断、输入无效各自给出恢复方式。
function evidenceErrorMessage(failure: { status: number; code: string | null; message: string }) {
  if (failure.status === 0) return "网络连接失败或请求超时；请检查网络后点击重试。";
  if (failure.code === "evidence_view_busy" || failure.code === "single_flight_join_timeout") {
    return "证据读取排队超时（可能正有一次完整重建在进行）；请稍候点击重试，或使用“当前资料”视图。";
  }
  if (failure.code === "evidence_context_not_found") return "该对象不存在或已被撤回；请返回列表重新选择。";
  if (failure.code === "refresh_only_for_current_product_view") return "重新读取仅适用于“当前资料”视图。";
  return failure.message;
}

function forecastExplanation(cell: SevenProductForecastCell) {
  if (/[\u4e00-\u9fff]/.test(cell.status_reason)) return cell.status_reason;
  if (cell.data_status === "missing") return "当前预测基准缺少可用价格，不能形成可靠预测。";
  if (cell.data_status === "stale") return "所用价格已超出该基准的时效要求，当前仅保留历史参考。";
  if (cell.data_status === "proxy") return "当前使用代理基准，请与行情页的报价口径分别核对。";
  if (cell.history_points < 20) return `当前只有 ${cell.history_points} 个历史价格点，模型证据不足，仅作降级参考。`;
  return "模型或样本外证据尚未通过正式门禁，当前预测仅供观察。";
}

function forecastCell(batch: SevenProductForecastBatch | undefined, target: EvidenceTarget, horizon: 1 | 7 | 30) {
  return batch?.cells.find(cell => cell.target === (target as SevenProductTarget) && cell.horizon_days === horizon);
}

function formatBaselineNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

// ---------------------------------------------------------------------------
// 图谱节点：只有真实档案关系才生成节点与连线。
// 命题 ← 当前支持／相反材料 ← 历史类似案例；没有任何材料时不画图。
// ---------------------------------------------------------------------------

type EvbNodeData = {
  label: string;
  sub: string;
  tone: "prop" | "support" | "counter" | "history";
  badge: string;
  claimId?: string;
  semanticReviewId?: string;
  conditional?: boolean;
  ports?: { id: string; side: "left" | "right"; top: number }[];
  [key: string]: unknown;
};

function EvbFlowNode({ data, selected }: NodeProps<Node<EvbNodeData>>) {
  return (
    <div className={`evb-flow-node is-${data.tone} ${data.conditional ? "is-conditional" : ""} ${selected ? "is-selected" : ""}`}>
      {data.ports?.map(port => <Handle key={port.id} type="target" position={port.side === "left" ? Position.Left : Position.Right} id={port.id} style={{ top: `${port.top}%` }} />)}
      {!data.ports && <Handle type="target" position={Position.Left} id="left" />}
      <Handle type="source" position={Position.Right} id="right" />
      <Handle type="source" position={Position.Left} id="left-source" />
      {!data.ports && <Handle type="target" position={Position.Right} id="right-target" />}
      <strong title={data.label}>{data.label}</strong>
      <span className="evb-node-role" title={data.sub}>{data.sub}</span>
      <em>{data.badge}</em>
    </div>
  );
}

const nodeTypes = { evb: EvbFlowNode };

function compactClaimTitle(claim: EvidenceClaim) {
  const source = claim.quote.trim().replace(/\s+/g, " ");
  return source.length > 46 ? `${source.slice(0, 46)}…` : source || claim.source_title;
}

function buildGraph(data: EvidenceDossier | null, conditional: EvidenceSemanticReview[] = []): { nodes: Node<EvbNodeData>[]; edges: Edge[] } {
  if (!data) return { nodes: [], edges: [] };
  const claims = new Map(data.claims.map(claim => [claim.claim_id, claim]));
  const nodes: Node<EvbNodeData>[] = [];
  // Keep relation descriptions in cards/legend. Floating edge text collides with
  // the central proposition when several relations converge, including in the modal.
  const edges: Edge[] = [];
  nodes.push({
    id: "proposition",
    type: "evb",
    position: { x: 350, y: 160 },
    draggable: false,
    data: {
      label: `${productLabels[data.target]} · ${data.horizon_days}天 · 分析命题`,
      sub: data.hypothesis || "（本档无命题）",
      tone: "prop",
      badge: `核验支持 ${data.current_support_episodes} · 相反 ${data.current_counter_episodes}${conditional.length ? ` · 条件 ${conditional.length}条` : ""}`
    }
  });
  const supportIds = data.current_support.filter(id => claims.has(id)).slice(0, 3);
  const counterIds = data.current_counter.filter(id => claims.has(id)).slice(0, 3);
  supportIds.forEach((id, index) => {
    const claim = claims.get(id)!;
    const nodeId = `support-${id}`;
    nodes.push({
      id: nodeId, type: "evb", position: { x: 0, y: index * 130 }, draggable: false,
      data: { label: compactClaimTitle(claim), sub: "当前支持材料", tone: "support", badge: claim.semantic_status === "rule_checked" ? "通过机制规则" : "待核验", claimId: id }
    });
    edges.push({
      id: `edge-support-${id}`, source: nodeId, sourceHandle: "right", target: "proposition", targetHandle: "left", type: "default",
      className: "evb-edge is-support"
    });
  });
  counterIds.forEach((id, index) => {
    const claim = claims.get(id)!;
    const nodeId = `counter-${id}`;
    nodes.push({
      id: nodeId, type: "evb", position: { x: 730, y: index * 130 }, draggable: false,
      data: { label: compactClaimTitle(claim), sub: "当前相反驱动", tone: "counter", badge: claim.semantic_status === "rule_checked" ? "通过机制规则" : "待核验", claimId: id }
    });
    edges.push({
      id: `edge-counter-${id}`, source: nodeId, sourceHandle: "left-source", target: "proposition", targetHandle: "right-target", type: "default",
      className: "evb-edge is-counter"
    });
  });
  const mechanismNames: Record<string, string> = {supply:"供应变化", inventory:"库存变化", demand:"需求变化", logistics:"运输通道", policy:"政策决定"};
  for (const direction of ["up", "down"] as const) {
    const directCount = direction === "up" ? supportIds.length : counterIds.length;
    conditional.filter(review => review.direction === direction).slice(0, 3 - directCount).forEach((review, index) => {
      const nodeId = `conditional-${review.review_id}`;
      nodes.push({id:nodeId, type:"evb", draggable:false,
        position:{x:direction === "up" ? 0 : 730, y:(directCount + index) * 130},
        data:{label:`${mechanismNames[review.mechanism] ?? "来源材料"} · ${direction === "up" ? "条件支持" : "条件相反"}`,
          sub:review.rationale.length > 50 ? `${review.rationale.slice(0,50)}…` : review.rationale,
          tone:direction === "up" ? "support" : "counter", badge:"AI 条件关联 · 不计票", conditional:true, semanticReviewId:review.review_id}
      });
      edges.push({id:`edge-${nodeId}`,source:nodeId, sourceHandle:direction === "up" ? "right" : "left-source",
        target:"proposition",targetHandle:direction === "up" ? "left" : "right-target",type:"default",
        className:`evb-edge is-${direction === "up" ? "support" : "counter"} is-conditional`
      });
    });
  }
  data.historical_support.slice(0, 2).forEach((history, index) => {
    const claim = claims.get(history.claim_id);
    if (!claim) return;
    const nodeId = `hist-support-${history.claim_id}`;
    nodes.push({
      id: nodeId, type: "evb", position: { x: 0, y: 440 + index * 150 }, draggable: false,
      data: { label: compactClaimTitle(claim), sub: "历史类似支持案例", tone: "history", badge: "事后复盘", claimId: history.claim_id }
    });
    edges.push({
      id: `edge-hist-support-${history.claim_id}`, source: nodeId, sourceHandle: "right", target: "proposition", targetHandle: "left", type: "default",
      className: "evb-edge is-history"
    });
  });
  data.historical_counter.slice(0, 2).forEach((history, index) => {
    const claim = claims.get(history.claim_id);
    if (!claim) return;
    const nodeId = `hist-counter-${history.claim_id}`;
    nodes.push({
      id: nodeId, type: "evb", position: { x: 730, y: 440 + index * 150 }, draggable: false,
      data: { label: compactClaimTitle(claim), sub: "历史类似反例", tone: "history", badge: "事后复盘", claimId: history.claim_id }
    });
    edges.push({
      id: `edge-hist-counter-${history.claim_id}`, source: nodeId, sourceHandle: "left-source", target: "proposition", targetHandle: "right-target", type: "default",
      className: "evb-edge is-history"
    });
  });
  // Position each side from its actual material count, not historical fixed offsets.
  // Independent ports keep each relation visible at the central proposition.
  const left = nodes.filter(node => node.id !== "proposition" && node.position.x === 0);
  const right = nodes.filter(node => node.position.x === 730);
  const rows = Math.max(left.length, right.length, 1);
  const center = nodes[0];
  center.position = { x: 440, y: (rows * 164 - 196) / 2 };
  center.data.ports = [];
  [left, right].forEach((side, sideIndex) => side.forEach((node, index) => {
    node.position = { x: sideIndex === 0 ? 0 : 880, y: index * 164 + (rows - side.length) * 82 };
    const edge = edges.find(item => item.source === node.id)!;
    const portId = `relation-${node.id}`;
    center.data.ports!.push({ id: portId, side: sideIndex === 0 ? "left" : "right", top: (index + 1) * 100 / (side.length + 1) });
    edge.targetHandle = portId;
  }));
  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// 主组件：证据核验台。
// 页面职责：回答“某个具体判断为什么成立、为什么可能不成立、还缺什么”。
// 默认阅读顺序 = 核验对象 → 判断与边界 → 正反依据 → 历史案例 → 缺口与追溯。
// ---------------------------------------------------------------------------

export function EvidenceVerificationBoard({ data: workbench, ragVisual }: {
  data?: AgentWorkbenchData;
  ragVisual?: WorkbenchRagVisualResponse;
}) {
  const [target, setTarget] = useState<EvidenceTarget>(boardMemory.target);
  const [horizon, setHorizon] = useState<1 | 7 | 30>(boardMemory.horizon);
  const [view, setView] = useState<EvidenceView>(boardMemory.view);
  const [dossier, setDossier] = useState<EvidenceDossier | null>(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<{ message: string; status: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedClaimId, setSelectedClaimId] = useState<string | null>(null);
  const [graphZoomOpen, setGraphZoomOpen] = useState(false);
  const requestSeq = useRef(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const autoReads = useRef(0);
  const lastReadOffset = useRef(0);

  useEffect(() => {
    boardMemory.target = target;
    boardMemory.horizon = horizon;
    boardMemory.view = view;
  }, [target, horizon, view]);

  const load = useCallback(async (next: { target: EvidenceTarget; horizon: 1 | 7 | 30; view: EvidenceView; offset: number; refresh?: boolean; unpin?: boolean; background?: boolean }) => {
    const request = ++requestSeq.current;
    lastReadOffset.current = next.offset;
    const hadData = dossier !== null;
    setLoading(true);
    setSwitching(hadData && !next.background);
    setError(null);
    setNotice(null);
    // 切换品种/期限/翻页时固定同一资料截止时点（input_sha256），保证一致并复用服务端快照；
    // 跨视图切换不携带旧 pin；显式“重新读取”不带 pin（refresh=true 取得新当前资料）。
    const pin = !next.refresh && !next.unpin && dossier && dossier.view === next.view ? (dossier.input_sha256 ?? undefined) : undefined;
    const read = async (withPin: boolean, offset = next.offset) => api.evidenceDossierResult(
      next.target, next.horizon, next.view, offset, withPin ? pin : undefined, {}, { refresh: next.refresh }
    );
    let result = await read(true);
    if (!result.ok && result.code === "evidence_view_changed_reload_first_page") {
      // 快照 pin 已过期：回到新快照第一页，不拿旧 offset 继续读新快照。
      result = await read(false, 0);
      if (request === requestSeq.current && result.ok) {
        setNotice("资料已更新（截止时间见上），已回到第 1 页。");
      }
    }
    if (request !== requestSeq.current) return;
    if (result.ok) {
      setDossier(result.data);
      setSelectedClaimId(null);
    } else {
      if (!next.background && (result.status === 404 || result.status >= 500 || result.status === 0)) {
        // 读取失败时清掉陈旧内容，避免把上一个对象的结果当成当前对象。
        setDossier(null);
      }
      setError({ message: evidenceErrorMessage(result), status: result.status });
    }
    setLoading(false);
    setSwitching(false);
  }, [dossier]);

  useEffect(() => {
    void load({ target, horizon, view, offset: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, horizon, view]);

  useEffect(() => {
    autoReads.current = 0;
  }, [target, horizon, view]);

  useEffect(() => {
    if (!dossier?.revalidating || loading || view !== "current" || dossier.target !== target
      || dossier.horizon_days !== horizon || autoReads.current >= 6 || lastReadOffset.current !== 0) return;
    const timer = window.setTimeout(() => {
      autoReads.current += 1;
      // No pin: read the server's completed cache rather than rebuilding or
      // staying pinned to the old snapshot. Never poll frozen issuance views.
      void load({ target, horizon, view, offset: 0, unpin: true, background: true });
    }, 10000);
    return () => window.clearTimeout(timer);
  }, [dossier, loading, target, horizon, view, load]);

  const claimsById = useMemo(() => new Map((dossier?.claims ?? []).map(claim => [claim.claim_id, claim])), [dossier]);
  const mechanismLabels = useMemo(() => {
    const map = new Map<string, string>();
    dossier?.coverage.forEach(entry => map.set(entry.mechanism, entry.label));
    return map;
  }, [dossier]);

  const renderClaims = useCallback((ids: string[], histories: EvidenceHistory[] = [], emptyTitle: string, emptyReason: string) => {
    const selected = ids.map(id => claimsById.get(id)).filter((claim): claim is EvidenceClaim => Boolean(claim));
    if (!selected.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} className="evb-empty" description={<span><strong>{emptyTitle}</strong><br />{emptyReason}</span>} />;
    }
    return selected.map(claim => (
      <EvidenceClaimCard
        key={claim.claim_id}
        claim={claim}
        history={histories.find(history => history.claim_id === claim.claim_id)}
        selected={selectedClaimId === claim.claim_id}
        onBackToVerdict={() => rootRef.current?.querySelector("#evb-verdict")?.scrollIntoView({ behavior: "smooth", block: "start" })}
      />
    ));
  }, [claimsById, selectedClaimId]);

  // 空材料的原因必须具体：区分“有材料未过校验”“确实没有材料”“没有合格历史案例”。
  const supportEmptyReason = useMemo(() => {
    if (!dossier) return "证据尚未读取";
    const stored = dossier.coverage.reduce((total, entry) => total + entry.stored_claims, 0);
    const needsReview = dossier.coverage.filter(entry => entry.status === "needs_review");
    const parts: string[] = [];
    if (stored > 0) parts.push(`资料库中有 ${stored} 条相关材料，但尚未通过机制规则核验；待核验材料不计入正反证`);
    const directional = dossier.gaps.find(gap => gap.includes("方向性事实"));
    if (directional) parts.push(directional);
    if (needsReview.length) parts.push(`待核验集中在：${needsReview.map(entry => entry.label).join("、")}`);
    return parts.join("；") || "资料截止时间前没有可用的事件材料；这不等于不存在此类证据";
  }, [dossier]);

  const counterEmptyReason = useMemo(() => {
    if (!dossier) return "证据尚未读取";
    if (dossier.current_counter_episodes > 0) return "";
    return "当前资料内没有通过机制规则核验的相反驱动事件；没有反证记录不等于不存在反证，请结合下方缺口与推翻信号理解";
  }, [dossier]);

  const historyEmptyReason = useMemo(() => {
    if (!dossier) return "证据尚未读取";
    const noCases = dossier.gaps.find(gap => gap.includes("历史案例") || gap.includes("历史类似"));
    return noCases ?? "没有满足同主体、机制、地点与事前方向条件的历史案例；历史案例不按事后涨跌挑选";
  }, [dossier]);

  const otherIds = useMemo(() => dossier?.other_materials ?? [], [dossier]);
  const semanticReviews = useMemo(() => conditionalReviews(dossier), [dossier]);
  const graph = useMemo(() => buildGraph(dossier, semanticReviews), [dossier, semanticReviews]);
  const mixedIds = useMemo(() => Array.from(new Set((dossier?.mixed ?? []).flat())), [dossier]);
  const forecastBatch = workbench?.main_prediction;
  const cell = forecastCell(forecastBatch, target, horizon);
  const batchMismatch = view === "issued" && dossier?.batch_id && forecastBatch && dossier.batch_id !== forecastBatch.batch_id;

  const handleNodeClick = useCallback(((_: unknown, node: Node<EvbNodeData>) => {
    if (node.data.semanticReviewId) {
      const card = document.getElementById(`evb-semantic-${node.data.semanticReviewId}`);
      if (card && rootRef.current?.contains(card)) card.scrollIntoView({behavior:"smooth", block:"center"});
      return;
    }
    const claimId = node.data.claimId;
    if (!claimId) return;
    setSelectedClaimId(claimId);
    window.setTimeout(() => {
      rootRef.current?.querySelector(`#evb-claim-${claimId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
  }), []);

  const statusAlert = useMemo(() => {
    if (!dossier) return null;
    if (dossier.status === "capture_failed") {
      return <Alert type="warning" showIcon className="evb-alert" message="本次证据采集不完整" description="统一档案读取未完成，不能把空计数当作“证据不存在”。可稍后重新读取；已读取到的材料照常显示，但完整性标记为未通过。" />;
    }
    if (dossier.status === "legacy_input") {
      return <Alert type="info" showIcon className="evb-alert" message="该批次早于统一证据档案" description={dossier.gaps[0] ?? "旧档案没有冻结统一证据；不以今天的资料补写历史依据。"} />;
    }
    if (dossier.status === "unavailable") {
      return <Alert type="warning" showIcon className="evb-alert" message="暂无可读取的发行输入" description={dossier.gaps[0] ?? "当前没有可读取的已发行输入。"} />;
    }
    return null;
  }, [dossier]);

  const viewLabel = view === "current" ? "当前资料" : "最近发行时证据";

  return (
    <div className="evb-board" ref={rootRef} data-testid="evidence-verification-board">
      <header className="evb-selector">
        <div className="evb-selector-group" role="group" aria-label="核验品种">
          <span>品种</span>
          <Segmented
            value={target}
            onChange={value => setTarget(value as EvidenceTarget)}
            options={productOrder.map(key => ({ value: key, label: productLabels[key] }))}
          />
        </div>
        <div className="evb-selector-group" role="group" aria-label="核验期限">
          <span>期限</span>
          <Segmented
            value={horizon}
            onChange={value => setHorizon(Number(value) as 1 | 7 | 30)}
            options={[{ value: 1, label: "1天" }, { value: 7, label: "7天" }, { value: 30, label: "30天" }]}
          />
        </div>
        <div className="evb-selector-group" role="group" aria-label="资料时点">
          <span>时点</span>
          <Segmented
            value={view}
            onChange={value => setView(value as EvidenceView)}
            options={[{ value: "current", label: "当前资料" }, { value: "issued", label: "最近发行时证据" }]}
          />
        </div>
        <div className="evb-selector-meta">
          {dossier?.as_of_time ? <span>资料截止 {formatDisplayTimestamp(dossier.as_of_time)}</span> : null}
          <Button
            size="small" icon={<ReloadOutlined />} loading={loading}
            onClick={() => void load({ target, horizon, view, offset: 0, refresh: view === "current" })}
          >重新读取</Button>
        </div>
      </header>

      {notice ? <Alert type="info" showIcon className="evb-alert" message={notice} /> : null}

      {switching && dossier ? (
        <div className="evb-switching">
          <Spin size="small" />
          {/* 明确归属：以下内容仍是上一次的对象，选择器已是新对象，不产生混搭。 */}
          <span>
            以下内容仍为 {productLabels[dossier.target]} · {dossier.horizon_days}天 · {dossier.view === "current" ? "当前资料" : "最近发行时证据"}
            {dossier.as_of_time ? `（截止 ${formatDisplayTimestamp(dossier.as_of_time)}）` : ""}；
            正在读取 {productLabels[target]} · {horizon}天 · {viewLabel}…
          </span>
        </div>
      ) : null}

      {error ? (
        <Alert
          type="error" showIcon className="evb-alert"
          message="证据暂不可用"
          description={<>{error.message}<Button size="small" style={{ marginLeft: 12 }} onClick={() => void load({ target, horizon, view, offset: 0, refresh: view === "current" })}>重试</Button></>}
        />
      ) : null}

      {loading && !dossier ? <ContentLoading title="正在读取证据档案" detail={`${productLabels[target]} · ${horizon}天 · ${viewLabel}；原文、支持依据与相反依据返回后显示。`} /> : null}
      <>
        {dossier ? (
          <div className="evb-body">
            <section className="evb-section" id="evb-verdict">
              <h3><i className="evb-step-no">1</i>正在核验的判断</h3>
              {/* 内容自标注：行内对象始终取自 dossier 本身；切换期间不会把旧内容说成新选择。 */}
              <p className="evb-verdict-line">
                正在核验 <strong>{productLabels[dossier.target]}</strong> 的 <strong>{dossier.horizon_days}天</strong> 判断，依据<strong>{dossier.view === "current" ? "当前资料" : "最近发行时证据"}</strong>
                {dossier.view === "issued" && dossier.batch_id ? <>（发行批次 <code>{dossier.batch_id}</code>）</> : null}。
                {dossier.revalidating && dossier.view === "current" ? <span className="evb-revalidating">{autoReads.current >= 6 ? "后台更新尚未确认，当前保留上次快照；请稍后点击‘重新读取’。" : "当前显示上次快照；正在检查后台更新，取得新资料后自动展示。也可点击‘重新读取’。"}</span> : null}
                {dossier.scope_note ? <span className="evb-scope-note">{dossier.scope_note}</span> : null}
              </p>
              <div className="evb-verdict-grid">
                <article className="evb-verdict-card is-proposition">
                  <header><span className="evb-kind-tag">分析命题 · 非预测</span></header>
                  <strong>{dossier.hypothesis || "本档没有分析命题"}</strong>
                  <p>分析命题描述“其他条件相同时的方向压力”，不是看涨／看跌预测，也不能替代模型预测或正式结论。</p>
                </article>
                <article className={`evb-verdict-card is-model ${cell ? "" : "is-empty"}`}>
                  <header><span className="evb-kind-tag">模型预测 · 算法输出</span>{cell ? <span className={`evb-formal-pill is-${cell.formal_status}`}>{formalStatusLabels[cell.formal_status]}</span> : null}</header>
                  {cell ? (
                    <>
                      <strong>{directionLabels[cell.direction]}{cell.predicted_change_pct === null ? "" : ` · ${(cell.predicted_change_pct * 100).toFixed(2)}%`}</strong>
                      <p>
                        点位 {cell.point_forecast === null ? "—" : `${formatBaselineNumber(cell.point_forecast)} ${cell.unit}`}；
                        区间 {cell.interval_low === null || cell.interval_high === null ? "—" : `${formatBaselineNumber(cell.interval_low)} – ${formatBaselineNumber(cell.interval_high)}`}。
                        参考评分 {Math.round(cell.confidence * 100)}%（非正确率）· {dataStatusLabels[cell.data_status]}。
                      </p>
                      <p className="evb-model-reason">{forecastExplanation(cell)}</p>
                      {batchMismatch ? (
                        <p className="evb-mismatch">注意：档案绑定批次 {dossier.batch_id}，模型预测卡显示最新发行批次 {forecastBatch?.batch_id}，两者不是同一批次。</p>
                      ) : null}
                    </>
                  ) : (
                    <p>{workbench ? "当前返回的批次未含本格，请重新读取批次核对。" : "模型预测资料尚未读到；证据档案可先查看，批次状态暂不可核对。"}</p>
                  )}
                </article>
                <article className="evb-verdict-card is-conclusion">
                  <header><span className="evb-kind-tag">正式结论 / 观察级</span></header>
                  {cell && (cell.formal_status === "formal" || cell.formal_status === "low_confidence") ? (
                    <>
                      <strong>已通过正式证据门禁</strong>
                      <p>{cell.formal_status === "low_confidence" ? "低置信正式：通过门禁但置信度偏低，按正式结论对待时请同时注意该提示。" : "该格预测按正式结论口径发布。"}</p>
                    </>
                  ) : (
                    <>
                      <strong>{cell ? "观察级，未形成正式结论" : "正式状态暂不可核对"}</strong>
                      <p>{cell ? "模型或样本外证据尚未通过正式门禁；分析命题不包装为正式看涨预测。正式结论只能由受治理流程发行。" : "尚未读取完整预测批次，正式状态暂不可核对。条件材料用于解释价格压力，不代表发牌方向。"}</p>
                    </>
                  )}
                  {dossier.market_baseline.value != null ? (
                    <p className="evb-baseline">
                      价格基线（单列，不计为事件证据）：{formatBaselineNumber(dossier.market_baseline.value)} {dossier.market_baseline.unit ?? ""}
                      {dossier.market_baseline.observed_at ? ` · 观察日 ${dossier.market_baseline.observed_at.slice(0, 10)}` : ""}
                      {dossier.market_baseline.status === "stale" ? "（已过期）" : ""}。
                    </p>
                  ) : null}
                </article>
              </div>
              <p className="evb-boundary">
                适用边界：{forecastBatch?.customer_boundary ?? "以预测批次发行时声明的客户边界为准。"}
                历史类比不证明因果；来源条数不等于独立证据票数。
              </p>
            </section>

            {statusAlert}

            <section className="evb-section" id="evb-claims">
              <h3><i className="evb-step-no">2</i>最关键的支持依据与相反依据</h3>
              <div className="evb-claim-columns">
                <div className="evb-claim-column is-support">
                  <h4>当前支持 <span>{dossier.current_support_episodes} 个事件</span></h4>
                  {!dossier.current_support.length && semanticReviews.some(review => review.direction === "up")
                    ? <p>当前没有通过核验的直接支持事件；以下材料保留传导条件，不计票。</p>
                    : renderClaims(dossier.current_support, [], "当前没有通过核验的支持事件", supportEmptyReason)}
                  {semanticReviews.some(review => review.direction === "up") ? <>
                    <h4>条件支持依据 <span>{semanticReviews.filter(review => review.direction === "up").length} 条 · 不计票</span></h4>
                    {semanticReviews.filter(review => review.direction === "up").map(review => <SemanticEvidenceCard key={review.review_id} review={review} />)}
                  </> : null}
                </div>
                <div className="evb-claim-column is-counter">
                  <h4>当前相反驱动 <span>{dossier.current_counter_episodes} 个事件</span></h4>
                  {!dossier.current_counter.length && semanticReviews.some(review => review.direction === "down")
                    ? <p>当前没有通过核验的直接相反事件；以下材料保留传导条件，不计票。</p>
                    : renderClaims(dossier.current_counter, [], "当前没有相反驱动事件", counterEmptyReason || "资料截止前没有通过机制规则核验的相反事件")}
                  {semanticReviews.some(review => review.direction === "down") ? <>
                    <h4>条件相反依据 <span>{semanticReviews.filter(review => review.direction === "down").length} 条 · 不计票</span></h4>
                    {semanticReviews.filter(review => review.direction === "down").map(review => <SemanticEvidenceCard key={review.review_id} review={review} />)}
                  </> : null}
                </div>
              </div>
              {otherIds.length || mixedIds.length ? (
                // 默认展开：这些是在途材料，藏着会让“支持/反驳 0 条”读成
                // “系统没有内容”，而实际上只是还没过核验。
                <details className="evb-other-materials" open>
                  <summary>待核验与其他材料 · {otherIds.length + mixedIds.length} 条（不计入正反证）</summary>
                  <p className="evb-section-note">
                    这些材料存在于资料库，但尚未通过机制规则核验（传闻、计划、版本或引用问题），或同主体出现方向相反的报道（冲突混合）。它们不计入正反证计数；逐条卡片说明具体原因。
                  </p>
                  <div className="evb-claim-columns">
                    <div className="evb-claim-column">
                      {otherIds.length ? (
                        <details open><summary>其他材料（{otherIds.length}）</summary>
                          {renderClaims(otherIds, [], "本页没有其他材料", "本页范围内没有其他材料")}
                        </details>
                      ) : null}
                    </div>
                    <div className="evb-claim-column">
                      {mixedIds.length ? (
                        <details open><summary>同主体相反或混合报道（{mixedIds.length}）</summary>
                          {renderClaims(mixedIds, [], "本页没有混合材料", "本页范围内没有混合材料")}
                        </details>
                      ) : null}
                    </div>
                  </div>
                </details>
              ) : null}
            </section>

            <section className="evb-section" id="evb-history">
              <h3><i className="evb-step-no">3</i>历史类似事件：支持案例与反例</h3>
              <p className="evb-section-note">历史案例按同主体、机制、地点与事前方向匹配，不按事后涨跌挑选；每个案例的机制预期与事后实际变化分开呈现。</p>
              <div className="evb-claim-columns">
                <div className="evb-claim-column is-support">
                  <h4>历史类似支持案例 <span>{dossier.historical_support.length} 个</span></h4>
                  {renderClaims(dossier.historical_support.map(h => h.claim_id), dossier.historical_support, "没有符合条件的历史支持案例", historyEmptyReason)}
                </div>
                <div className="evb-claim-column is-counter">
                  <h4>历史类似反例 <span>{dossier.historical_counter.length} 个</span></h4>
                  {renderClaims(dossier.historical_counter.map(h => h.claim_id), dossier.historical_counter, "没有符合条件的历史反例", historyEmptyReason)}
                </div>
              </div>
            </section>

            <section className="evb-section" id="evb-gaps">
              <h3><i className="evb-step-no">4</i>尚缺的事实与如何追溯</h3>
              <div className="evb-gap-grid">
                <div className="evb-coverage">
                  <h4>机制覆盖</h4>
                  <ul>
                    {dossier.coverage.map(entry => (
                      <li key={entry.mechanism} className={`is-${entry.status}`}>
                        <strong>{entry.label}</strong>
                        <span>
                          {entry.status === "available"
                            ? `${entry.usable_episodes} 个可用事件`
                            : entry.status === "needs_review"
                              ? `有 ${entry.stored_claims} 条材料待核验（未计入正反证）`
                              : entry.stored_claims > 0
                                ? `仅有 ${entry.stored_claims} 条历史材料，本轮未形成可用事件`
                                : dossier.total_claims > 0 || dossier.other_materials.length > 0
                                  ? dossier.other_materials.length > 0
                                    ? `${dossier.other_materials.length} 条材料待核验，未通过机制规则不计票`
                                    : `${dossier.total_claims} 条关联材料尚未形成该机制的核验结果，未通过机制规则不计票`
                                  : "暂无材料"}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="evb-gaps-list">
                  <h4>证据缺口</h4>
                  {dossier.gaps.length ? (
                    <ul>{dossier.gaps.map(gap => <li key={gap}>{gap}</li>)}</ul>
                  ) : <p>本档没有列出的缺口。</p>}
                  <h4>如何追溯原始材料</h4>
                  <p>上方每条证据卡片都有“查看原始来源”直达原文链接，并标注发布时间与发生／报告期；原始材料与系统推断、事后结果分层显示。技术详情中保留本档快照哈希与批次标识，用于核对资料版本。</p>
                </div>
              </div>
            </section>

            <section className="evb-section" id="evb-graph">
              <h3><i className="evb-step-no">5</i>事件材料与机制图谱</h3>
              <EvidenceEventGraph dossier={dossier} />
              <h3>
                已核验方向与证据关系图{semanticReviews.length ? " · 含条件关联" : ""}
                {graph.nodes.length > 1 ? <Button size="small" icon={<ExpandAltOutlined />} onClick={() => setGraphZoomOpen(true)}>放大查看</Button> : null}
              </h3>
              {graph.nodes.length > 1 ? (
                <>
                  <div className="evb-graph-legend">
                    <span><i className="is-support" />支持命题</span>
                    <span><i className="is-counter" />相反驱动</span>
                    <span><i className="is-history" />历史类似（虚线）</span>
                    {semanticReviews.length ? <span><i className="is-history" />AI 条件关联（虚线 · 不计票）</span> : null}
                  </div>
                  <div className="evb-flow-stage" style={{ height: Math.max(440, Math.max(...graph.nodes.map(node => node.position.y)) + 210) }} data-testid="evb-graph-canvas">
                    <ReactFlow
                      nodes={graph.nodes}
                      edges={graph.edges}
                      nodeTypes={nodeTypes}
                      fitView
                      fitViewOptions={{ padding: 0.06, minZoom: 0.7, maxZoom: 1 }}
                      nodesConnectable={false}
                      edgesReconnectable={false}
                      minZoom={0.2}
                      maxZoom={1.4}
                      panOnScroll
                      nodesDraggable={false}
                      proOptions={{ hideAttribution: true }}
                      onNodeClick={handleNodeClick}
                    >
                      <Background color="#dbeafe" gap={22} />
                      <Controls showInteractive={false} />
                    </ReactFlow>
                  </div>
                  <p className="evb-graph-note">节点来自当前档案与原文材料；AI 条件关联使用虚线且不计票，不证明所选期限的发牌方向。当前依据每侧最多3条，历史类似每侧最多2条；完整材料见上方，点击节点定位来源卡片。</p>
                </>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} className="evb-empty" description={<span><strong>当前档案没有可成图的真实证据关系</strong><br />{supportEmptyReason}</span>} />
              )}
            </section>

            <details className="evb-tech">
              <summary>技术详情（快照、批次与来源质量）</summary>
              <dl>
                <div><dt>档案范围</dt><dd>{dossier.scope}{dossier.context_id ? ` · ${dossier.context_id}` : ""}{dossier.context_revision ? `（修订 ${dossier.context_revision}）` : ""}</dd></div>
                <div><dt>资料截止</dt><dd>{dossier.as_of_time ? formatDisplayTimestamp(dossier.as_of_time) : "无"}</dd></div>
                {dossier.batch_id ? <div><dt>发行批次</dt><dd><code>{dossier.batch_id}</code></dd></div> : null}
                <div><dt>快照哈希</dt><dd><code>{dossier.input_sha256 ?? "无"}</code></dd></div>
                <div><dt>来源行数／关联材料</dt><dd>{dossier.source_rows} 行 · 共 {dossier.total_claims} 条关联材料</dd></div>
                <div><dt>采集完整性</dt><dd>{dossier.capture_complete ? "完整" : "不完整（不能把空计数当作证据不存在）"}</dd></div>
                {dossier.matched_source_claims ? <div><dt>命中当前对象来源</dt><dd>{dossier.matched_source_claims} 条</dd></div> : null}
              </dl>
              {Object.keys(dossier.source_gaps).length ? (
                <>
                  <h4>本次全局资料质量检查</h4>
                  <ul>{Object.entries(dossier.source_gaps).map(([key, count]) => <li key={key}>{sourceGapLabels[key] ?? "其他来源质量问题"}：{count}</li>)}</ul>
                </>
              ) : null}
              {ragVisual && ragVisual.retrieval_path.length ? (
                <>
                  <h4>检索执行步骤（诊断信息，不影响上方业务结论）</h4>
                  <ol>{ragVisual.retrieval_path.map(step => <li key={step.id}>{step.step}：{step.description}（{step.status} · {step.count_label}）</li>)}</ol>
                </>
              ) : null}
            </details>

            <footer className="evb-pagination">
              <span>规则抽取材料 {dossier.total_claims} 条 · AI 条件材料 {semanticReviews.length} 条（不计票）；规则材料第 {Math.floor(dossier.offset / 50) + 1} 页；来源条数不等于独立证据票数</span>
              <span>
                <Button size="small" disabled={dossier.offset === 0 || loading} onClick={() => void load({ target, horizon, view, offset: Math.max(0, dossier.offset - 50) })}>上一页</Button>
                <Button size="small" disabled={dossier.next_offset === null || loading} style={{ marginLeft: 8 }} onClick={() => void load({ target, horizon, view, offset: dossier.next_offset! })}>下一页</Button>
              </span>
            </footer>
          </div>
        ) : !loading && !error ? (
          <Empty description="尚未读取证据" />
        ) : null}
      </>

      <Modal
        open={graphZoomOpen}
        onCancel={() => setGraphZoomOpen(false)}
        footer={null}
        title={`${productLabels[target]} · ${horizon}天 · 判断与证据关系图`}
        width="min(1500px, 96vw)"
        styles={{ body: { height: "76vh" } }}
      >
        <div className="evb-flow-stage is-modal" data-testid="evb-graph-modal">
          <ReactFlow
            nodes={graph.nodes}
            edges={graph.edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.08, minZoom: 0.6, maxZoom: 1 }}
            nodesConnectable={false}
            edgesReconnectable={false}
            minZoom={0.15}
            maxZoom={1.8}
            panOnScroll
            nodesDraggable={false}
            proOptions={{ hideAttribution: true }}
            onNodeClick={handleNodeClick}
          >
            <Background color="#dbeafe" gap={22} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <Typography.Paragraph type="secondary" style={{ marginTop: 10 }}>
          放大视图仅改变显示比例；节点与连线和页面内一致，均来自当前档案的真实分类关系。
        </Typography.Paragraph>
      </Modal>
    </div>
  );
}
