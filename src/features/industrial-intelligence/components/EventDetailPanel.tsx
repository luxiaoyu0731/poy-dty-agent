import { ContentLoading } from "../../../components/ContentLoading";
import { productLabel } from "../../../components/displayLabels";
import { SemanticEvidenceCard } from "../../evidence-system/SemanticEvidenceCard";
import { EvidenceDossierButton } from "../../evidence-system/EvidenceDossierButton";
import { evidenceTarget } from "../../evidence-system/types";
import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Space, Tag, Typography } from "antd";
import * as intelligenceApi from "../api";
import type { EventDetail, EventSummary } from "../api";
import { newFeedbackRequestId } from "../hooks";
import { displayEventTitle, shanghaiDateTime } from "../presentation";
import { eventEvidenceGaps } from "../eventEvidence";

const { Text, Title } = Typography;

const CATEGORY_LABELS: Record<string, string> = {
  energy: "能源",
  plant_supply: "装置供应",
  shipping_ports: "港口航运",
  weather_disaster: "天气灾害",
  geopolitics_sanctions: "地缘制裁",
  macro_policy: "宏观政策",
  trade_regulation: "贸易监管",
  other: "其他",
};

const DIRECTION_LABELS: Record<string, string> = {
  upward_pressure: "成本上推压力",
  downward_pressure: "成本下移压力",
  mixed: "多空交织",
  unclear: "方向未明",
};

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category;
}

export function directionLabel(direction: string): string {
  return DIRECTION_LABELS[direction] ?? direction;
}

function safeExternalUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "https:" || parsed.protocol === "http:") && !parsed.username && !parsed.password ? parsed.toString() : null;
  } catch {
    return null;
  }
}

/**
 * Secondary event view shared by the summary, radar, and map. All source text
 * is rendered as React text nodes (never dangerouslySetInnerHTML), so hostile
 * titles/URLs stay inert data.
 */
export function EventDetailPanel(props: {
  event: EventSummary | null;
  inline?: boolean;
  onClose: () => void;
}) {
  const { event, onClose } = props;
  const [detail, setDetail] = useState<EventDetail | null>(null);
  const [evidence, setEvidence] = useState<intelligenceApi.EvidenceLink[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [evidencePending, setEvidencePending] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [feedbackState, setFeedbackState] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<intelligenceApi.EventDetail[]>([]);
  const [revisionsOpen, setRevisionsOpen] = useState(false);
  const [revisionsPending, setRevisionsPending] = useState(false);
  const [revisionsError, setRevisionsError] = useState<string | null>(null);

  useEffect(() => {
    if (!event) {
      setDetail(null);
      setEvidence([]);
      setError(null);
      setEvidenceError(null);
      setFeedbackState(null);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    setEvidence([]);
    setEvidenceError(null);
    setEvidencePending(true);
    setFeedbackState(null);
    intelligenceApi
      .getEvent(event.event_id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setDetail(value);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "加载事件详情失败");
      });
    intelligenceApi
      .listEvidence(event.event_id, 200, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted) setEvidence(page.items);
      })
      .catch(() => {
        if (!controller.signal.aborted) setEvidenceError("证据链接暂未加载，不能据此判定事件没有证据。");
      }).finally(() => { if (!controller.signal.aborted) setEvidencePending(false); });
    // 审计 A5：修订历史按需拉取（默认收起，事件有多版演变时才展开看）。
    setRevisions([]);
    setRevisionsOpen(false);
    setRevisionsPending(false);
    setRevisionsError(null);
    return () => {
      controller.abort();
    };
  }, [event, attempt]);

  // Audit history is secondary: fetch it only when the user expands it.
  useEffect(() => {
    if (!event || !revisionsOpen) return;
    const controller = new AbortController();
    setRevisionsPending(true);
    setRevisionsError(null);
    intelligenceApi.listEventRevisions(event.event_id, 5, controller.signal)
      .then(page => { if (!controller.signal.aborted) setRevisions(page.items); })
      .catch(() => { if (!controller.signal.aborted) setRevisionsError("修订快照暂未返回；可收起后重新展开，不能据此判定没有历史记录。"); })
      .finally(() => { if (!controller.signal.aborted) setRevisionsPending(false); });
    return () => controller.abort();
  }, [event, revisionsOpen]);

  const sendFeedback = (action: "relevant" | "irrelevant" | "duplicate" | "watch") => {
    if (!event) return;
    intelligenceApi
      .postFeedback({
        client_request_id: newFeedbackRequestId(),
        target_type: "event",
        target_id: event.event_id,
        action,
      })
      .then((receipt) => {
        setFeedbackState(receipt.replayed ? "反馈已记录（重复请求已合并）" : "反馈已记录");
      })
      .catch((cause: unknown) => {
        setFeedbackState(cause instanceof Error ? `反馈失败：${cause.message}` : "反馈失败");
      });
  };

  const visibleGaps = detail ? eventEvidenceGaps(detail) : [];
  const contents = <>
      {!event ? null : !detail && !error ? (
        <ContentLoading title="正在加载事件详情" detail="核对事件事实、传导条件与来源；列表仍可继续阅读。" compact />
      ) : error ? (
        <Alert type="error" showIcon message="事件详情加载失败" description={error} action={<Button onClick={() => setAttempt((value) => value + 1)}>重试详情</Button>} />
      ) : detail ? (
        <Space direction="vertical" size="middle" className="radar-detail-content" style={{ width: "100%" }}>
          {evidenceError ? <Alert type="warning" showIcon message={evidenceError} action={<Button onClick={() => setAttempt((value) => value + 1)}>重试证据</Button>} /> : null}
          <Space wrap size="small" aria-label="事件元信息">
            <Tag color="blue">{categoryLabel(detail.category)}</Tag>
            <Tag>来源等级 {detail.evidence_count > 0 ? "见证据链" : "无证据"}</Tag>
            <Tag title="根据来源等级与证据数量计算，不代表事实摘要完整或影响判断已获验证">来源支持度 {(detail.confidence * 100).toFixed(0)}%</Tag>
            <Tag>相关性 {detail.relevance_score.toFixed(0)}</Tag>
            <Tag>修订 {detail.revision_count}</Tag>
            <Tag>证据 {detail.evidence_count}</Tag>
            {detail.product_ids.map((product) => (
              <Tag key={product} color="purple">
                {productLabel(product)}
              </Tag>
            ))}
          </Space>
          <Text>最近收录：{shanghaiDateTime(detail.last_seen_at)} · 研判截止：{shanghaiDateTime(detail.as_of_time)}</Text>
          {detail.overview_text ? <section aria-label="中文阅读摘要"><Title level={5}>中文阅读摘要</Title><Text>{detail.overview_text}</Text></section> : null}

          <section aria-labelledby="event-facts-heading">
            <Title id="event-facts-heading" level={5}>
              来源报道
            </Title>
            {detail.facts.length === 0 ? (
              <Text type="secondary">暂无来源直接支持的事实。</Text>
            ) : (
              <ul>
                {detail.facts.map((claim) => (
                  <li key={claim.claim_id}>
                    {claim.text.length > 280 ? <details>
                      <summary>{claim.text.slice(0, 220)}… 展开完整摘录</summary>
                      <Text style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{claim.text}</Text>
                    </details> : <Text>{claim.text}</Text>}
                    {claim.published_date ? <Text type="secondary">（来源发布日期 {claim.published_date}，日粒度）</Text> : null}
                    {claim.evidence_link_ids.length > 0 ? (
                      <Text type="secondary">（证据 {claim.evidence_link_ids.length} 条）</Text>
                    ) : (
                      <Tag color="warning">缺证据引用</Tag>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {detail.semantic_reviews?.length ? <section aria-label="当前来源机制复核">
            <Title level={5}>当前来源机制复核</Title>
            <p>截至 {shanghaiDateTime(detail.semantic_review_as_of ?? detail.as_of_time)}；这是关联来源的当前复核，单独列示，不补写原事件修订或历史发牌依据。</p>
            {detail.semantic_reviews.map(review => <SemanticEvidenceCard key={`${review.target}:${review.review_id}`} review={review} />)}
          </section> : <p className="radar-analysis-gap">当前关联来源尚无通过原文绑定的语义复核，不以通用传导句代替。</p>}
          {detail.inferences.length > 0 ? <section aria-labelledby="event-inferences-heading">
            <Title id="event-inferences-heading" level={5}>
              事件原修订中的推断（非事实）
            </Title>
            {detail.inferences.length === 0 ? (
              <Text type="secondary">暂无推断。</Text>
            ) : (
              <ul>
                {detail.inferences.map((inference) => (
                  <li key={inference.inference_id}>
                    {inference.text.startsWith("依据来源直接报道，事件可能与") ? <details>
                      <summary>原修订保留的通用推断；尚未完成具体机制核验</summary>
                      <Text>{inference.text}</Text>
                    </details> : <Text>{inference.text}</Text>}
                    <br />
                    <Text type="secondary">
                      置信度 {(inference.confidence * 100).toFixed(0)}%；假设：{inference.assumptions.join("；") || "无"}
                    </Text>
                  </li>
                ))}
              </ul>
            )}
          </section> : null}
          {detail.horizon_impact.length > 0 ? <section aria-labelledby="event-horizon-heading">
            <Title id="event-horizon-heading" level={5}>
              事件原修订中的期限判断（尚待核验）
            </Title>
            <Descriptions size="small" column={1} bordered>
              {detail.horizon_impact.map((impact) => (
                <Descriptions.Item
                  key={`${impact.product_id}-${impact.horizon}`}
                  label={`${productLabel(impact.product_id)} · ${{ D1: "1 天", D7: "7 天", D30: "30 天" }[impact.horizon] ?? impact.horizon}`}
                >
                  {directionLabel(impact.direction)}（置信度 {(impact.confidence * 100).toFixed(0)}%）
                </Descriptions.Item>
              ))}
            </Descriptions>
          </section> : null}

          {(detail.supply_chain_paths?.length ?? 0) > 0 ? <section aria-label="传导路径"><Title level={5}>为什么影响产业链</Title>{(detail.supply_chain_paths ?? []).map(path => <article key={path.path_id}><p>{path.explanation}</p><small>关联来源事实 {path.basis_claim_ids.length} 条；路径解释属于系统推断。</small></article>)}</section> : null}
          {!detail.inferences.length || !detail.horizon_impact.length ? <p className="radar-analysis-gap">分析尚未齐备：{[!detail.inferences.length && "传导推断", !detail.horizon_impact.length && "分期限影响"].filter(Boolean).join("、")}未返回。来源报道不能据此变成方向结论。</p> : null}
          <section aria-label="事件统一证明">
            <EvidenceDossierButton key={detail.event_revision_id} label="查看本事件完整证明"
              initialTarget={evidenceTarget(detail.product_ids[0] ?? "crude")}
              context={{event_id: detail.event_id, event_revision_id: detail.event_revision_id}} />
            <Text type="secondary"> 当前来源、相反驱动与历史类似事件分别核验。</Text>
          </section>
          {detail.revision_count > 1 ? (
            <section aria-labelledby="event-revisions-heading">
              <Title id="event-revisions-heading" level={5}>
                修订历史（{detail.revision_count} 版，展开核对快照）
              </Title>
              <Button size="small" onClick={() => setRevisionsOpen((open) => !open)}>
                {revisionsOpen ? "收起修订" : "展开修订"}
              </Button>
              {revisionsOpen ? (
                <>
                {revisionsPending ? <p role="status">正在读取修订快照…</p> : null}
                {revisionsError ? <Alert type="warning" message={revisionsError} /> : null}
                <ul style={{ marginTop: 8 }}>
                  {revisions.map((revision) => (
                    <li key={revision.event_revision_id}>
                      <Tag>第 {revision.revision_no} 版</Tag>
                      <Text type="secondary">
                        最近见到 {shanghaiDateTime(revision.last_seen_at)} · 事实 {revision.facts.length} 条 · 证据{" "}
                        {revision.evidence_count} 条
                      </Text>
                      {revision.gaps.length > 0 ? (
                        <Text type="secondary">（存在 {revision.gaps.length} 项待补缺口）</Text>
                      ) : null}
                    </li>
                  ))}
                </ul>
                </>
              ) : null}
            </section>
          ) : null}
          <section aria-labelledby="event-evidence-heading">
            <Title id="event-evidence-heading" level={5}>
              证据与转载关系
            </Title>
            {evidencePending ? <p role="status">正在读取原始来源链接…</p> : null}
            {!evidencePending && !evidenceError && evidence.length === 0 ? <Text type="secondary">当前未返回可展示的证据链接。</Text> : null}
            <ul>
              {evidence.map((link) => (
                <li key={link.evidence_link_id}>
                  <Tag>{{fact:"事实来源关联",corroboration:"佐证来源",counterevidence:"反证来源",discovery:"发现线索",location:"地点依据"}[link.evidence_role]}</Tag>
                  <Tag color={link.independent_corroboration ? "green" : "default"}>
                    {link.evidence_role === "fact" ? "原始来源" : link.independent_corroboration ? "独立佐证" : "同源转载"}
                  </Tag>
                  <Text>{link.citation_label}</Text>
                  <Text type="secondary">（{link.source_tier} 级）</Text>
                  <br />
                  {safeExternalUrl(link.canonical_url) ? (
                    <a
                      href={safeExternalUrl(link.canonical_url) ?? undefined}
                      target="_blank"
                      rel="noreferrer noopener nofollow"
                    >
                      原始链接
                    </a>
                  ) : (
                    <Text type="secondary">原始链接不可安全打开</Text>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="event-gaps-heading">
            <Title id="event-gaps-heading" level={5}>
              反证与缺口
            </Title>
            <div aria-label="事件反证">
              {detail.counterevidence.length ? <ul>{detail.counterevidence.map((claim) => (
                <li key={claim.claim_id}>
                  <Tag color="purple">反证</Tag><Text>{claim.text}</Text>
                  {claim.evidence_link_ids.length ? claim.evidence_link_ids.map((id, index) => {
                    const link = evidence.find(item => item.evidence_link_id === id);
                    const url = link ? safeExternalUrl(link.canonical_url) : null;
                    return url ? <a key={id} href={url} target="_blank" rel="noreferrer noopener nofollow"> [反证来源 {index + 1}]</a>
                      : <Text key={id} type="secondary">（反证来源 {index + 1} 暂未加载）</Text>;
                  }) : <Tag color="warning">尚未关联来源，待核验</Tag>}
                </li>
              ))}</ul> : <Text type="secondary">当前事件暂无已关联的反证记录，不代表不存在反证。</Text>}
            </div>
            <div aria-label="事件缺口">
              {visibleGaps.length ? <ul>{visibleGaps.map(gap => (
                <li key={gap.message}>
                  <Tag color="orange">缺口</Tag><Text type="secondary">{gap.message}</Text>
                  {gap.contexts.length ? <div><Text type="secondary">适用：{gap.contexts.join("；")}</Text></div> : null}
                </li>
              ))}</ul> : <Text type="secondary">当前事件未记录额外缺口，不代表证据已完整。</Text>}
            </div>
          </section>

          <section aria-label="操作者反馈">
            <Space wrap>
              <Button size="small" onClick={() => sendFeedback("relevant")}>
                相关
              </Button>
              <Button size="small" onClick={() => sendFeedback("irrelevant")}>
                无关
              </Button>
              <Button size="small" onClick={() => sendFeedback("duplicate")}>
                重复
              </Button>
              <Button size="small" onClick={() => sendFeedback("watch")}>
                持续观察
              </Button>
              {feedbackState ? <Text type="secondary">{feedbackState}</Text> : null}
            </Space>
            <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
              反馈只影响未来排序与显示，不会改写历史证据，也不会触发任何提醒。
            </Text>
          </section>
        </Space>
      ) : null}
    </>;
  return props.inline ? <aside className="review-radar-detail"><header><div><small>事件阅读</small><strong>{event ? displayEventTitle(event) : "事件详情与证据"}</strong></div>{event ? <Button size="small" aria-label="关闭" onClick={onClose}>关闭</Button> : null}</header><div className="review-detail-scroll">{event ? contents : <p className="review-detail-empty">选择左侧事件，在这里核对摘要、事实、传导与来源。列表保持原位，便于连续阅读。</p>}</div></aside> : <Drawer open={event !== null} onClose={onClose} width={560} title={event ? displayEventTitle(event) : "事件详情"} destroyOnClose>{contents}</Drawer>;
}
