import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Empty, Select, Space, Spin, Tabs, Tag, Typography } from "antd";
import { api } from "../../services/api";
import { formatDisplayTimestamp } from "../../utils/displayFormatting";
import { EvidenceClaimCard } from "./EvidenceClaimCard";
import { SemanticEvidenceCard } from "./SemanticEvidenceCard";
import { conditionalReviews } from "./semanticReviews";
import type { EvidenceContext, EvidenceDossier, EvidenceHistory, EvidenceTarget, EvidenceView } from "./types";

const products: Record<EvidenceTarget, string> = {crude: "原油", naphtha: "石脑油", px: "PX", pta: "PTA", meg: "MEG", poy: "POY", dty: "DTY"};

export function EvidenceDossierButton({ label = "七品种证据档案", initialTarget = "poy", initialHorizon = 1, initialView = "current", context = {} }: {
  label?: string; initialTarget?: EvidenceTarget; initialHorizon?: 1 | 7 | 30;
  initialView?: EvidenceView; context?: EvidenceContext;
}) {
  const contextKey = JSON.stringify(context);
  const scoped = Object.keys(context).length > 0;
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<EvidenceTarget>(initialTarget);
  const [horizon, setHorizon] = useState<1 | 7 | 30>(initialHorizon);
  const [view, setView] = useState<EvidenceView>(initialView);
  const [data, setData] = useState<EvidenceDossier | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [switching, setSwitching] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const dossierRef = useRef<EvidenceDossier | null>(null);
  dossierRef.current = data;
  const requestId = useRef(0);
  const load = useCallback(async (next: { target: EvidenceTarget; horizon: 1 | 7 | 30; view: EvidenceView; offset: number; refresh?: boolean }) => {
    const request = ++requestId.current;
    setLoading(true);
    setSwitching(dossierRef.current !== null);
    setError("");
    setNotice(null);
    // 品种/期限/翻页切换复用同一资料截止时点（input_sha256 pin）；跨视图不携带旧 pin；
    // 显式“重新读取”不带 pin（refresh=true 取得新当前资料，仅限当前资料视图）。
    const pin = !next.refresh && dossierRef.current && dossierRef.current.view === next.view
      ? (dossierRef.current.input_sha256 ?? undefined)
      : undefined;
    const read = (withPin: boolean, offset = next.offset) => api.evidenceDossierResult(
      next.target, next.horizon, next.view, offset, withPin ? pin : undefined, context, { refresh: next.refresh }
    );
    let result = await read(true);
    if (!result.ok && result.code === "evidence_view_changed_reload_first_page") {
      // 快照 pin 已过期：回到新快照第一页，不拿旧 offset 继续读新快照。
      result = await read(false, 0);
      if (request === requestId.current && result.ok) {
        setNotice("资料已更新（截止时间见上），已回到第 1 页。");
      }
    }
    if (request !== requestId.current) return;
    if (result.ok) {
      setData(result.data);
    } else {
      // 切换对象后读取失败必须清掉旧对象内容，避免显示上一对象的证明。
      setData(null);
      setError(result.status === 0
        ? "网络连接失败或请求超时；请检查网络后重试。"
        : result.code === "evidence_view_busy" || result.code === "single_flight_join_timeout"
          ? "证据读取排队超时（可能正有一次完整重建在进行）；请稍候重试。"
          : result.message);
    }
    setLoading(false);
    setSwitching(false);
  }, [context]);
  useEffect(() => {
    setData(null);
    if (open) void load({ target, horizon, view, offset: 0 });
    return () => { requestId.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, target, horizon, view, contextKey]);
  const claims = (ids: string[], history: EvidenceHistory[] = []) => {
    const selected = data?.claims.filter(c => ids.includes(c.claim_id)) ?? [];
    return selected.length ? selected.map(claim => <EvidenceClaimCard key={claim.claim_id} claim={claim} history={history.find(h => h.claim_id === claim.claim_id)} />) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本页没有符合条件的材料；不表示不存在此类证据" />;
  };
  const mixed = new Set(data?.mixed.flat() ?? []);
  const conditional = (direction: "up" | "down") => {
    const reviews = conditionalReviews(data).filter(review => review.direction === direction);
    return reviews.length ? <section aria-label={direction === "up" ? "条件上行依据" : "条件下行依据"}>
      <Typography.Title level={5}>条件依据 · 不计票</Typography.Title>
      <Typography.Paragraph type="secondary">以下是原文绑定的机制解释，仍需满足列出的条件，不等于直接证据或正式预测。</Typography.Paragraph>
      {reviews.map(review => <SemanticEvidenceCard key={`${review.target}:${review.review_id}`} review={review} />)}
    </section> : null;
  };
  const tabs = data ? [
    {key:"support", label:"当前正证", children:<>{claims(data.current_support)}{conditional("up")}</>},
    {key:"counter", label:"当前反证", children:<>{claims(data.current_counter)}{conditional("down")}</>},
    {key:"history-support", label:"历史类似正证", children:claims(data.historical_support.map(h=>h.claim_id),data.historical_support)},
    {key:"history-counter", label:"历史类似反证", children:claims(data.historical_counter.map(h=>h.claim_id),data.historical_counter)},
    {key:"other", label:"待核验与其他", children:<>{claims([...data.other_materials, ...mixed, ...data.historical_other.map(h=>h.claim_id)],data.historical_other)}</>}
  ] : [];
  return <>
    <Button size="small" onClick={() => { setTarget(initialTarget); setHorizon(initialHorizon); setView(initialView); setOpen(true); }}>{label}</Button>
    <Drawer title="七品种证据档案" width="min(980px, 95vw)" open={open} onClose={() => setOpen(false)}>
      <Space wrap style={{marginBottom: 16}}>
        <Select aria-label="证据品种" value={target} options={Object.entries(products).map(([value,label])=>({value,label}))} onChange={setTarget} style={{width: 115}} />
        <Select aria-label="证据期限" value={horizon} options={[1,7,30].map(value=>({value,label:`${value}天`}))} onChange={setHorizon} style={{width: 90}} />
        {!scoped ? <Select aria-label="证据时点" value={view} options={[{value:"current",label:"当前资料"},{value:"issued",label:"最近发行时证据"}]} onChange={setView} style={{width: 175}} /> : null}
        <Button onClick={() => void load({ target, horizon, view, offset: 0, refresh: !scoped && view === "current" })} loading={loading}>重新读取证据</Button>
      </Space>
      <Alert type="info" showIcon message={scoped ? "按当前对象与截止时点核对证明" : view === "current" ? "当前资料用于分析核验，不改写已发行预测" : "按发行时冻结输入核对证据"} description="当前正反证围绕下方命题分类；历史正反证检验类似事件的机制预期。事实否认、机制推断与历史价格反应分别理解。切换品种/期限时沿用同一资料截止时点。" />
      {switching && data ? (
        <Alert style={{marginTop: 12}} type="info" showIcon message={`以下内容仍为 ${products[data.target]} · ${data.horizon_days}天${data.as_of_time ? `（截止 ${formatDisplayTimestamp(data.as_of_time)}）` : ""}；正在读取 ${products[target]} · ${horizon}天…`} />
      ) : null}
      {notice ? <Alert style={{marginTop: 12}} type="info" showIcon message={notice} /> : null}
      {data?.revalidating ? <Alert style={{marginTop: 12}} type="warning" showIcon message="当前显示上次快照；服务端正在更新最新资料，完成后下次读取生效，也可点击“重新读取证据”立即取得。" /> : null}
      {error ? <Alert style={{marginTop: 12}} type="error" showIcon message="证据暂不可用" description={<>{error}<Button size="small" style={{marginLeft: 12}} onClick={() => void load({ target, horizon, view, offset: 0 })}>重试</Button></>} /> : null}
      <Spin spinning={loading && !data} tip="正在读取证据档案…">
        {data ? <>
          <Typography.Paragraph type="secondary">{data.scope_note}</Typography.Paragraph>
          <Typography.Title level={5}>{data.hypothesis || "此批次尚无统一证据档案"}</Typography.Title>
          <Typography.Paragraph type="secondary">资料截止：{data.as_of_time ? formatDisplayTimestamp(data.as_of_time) : "暂无"} · 当前支持事件 {data.current_support_episodes} · 当前相反驱动事件 {data.current_counter_episodes}</Typography.Paragraph>
          {data.market_baseline.value != null ? <Typography.Paragraph>价格基线：{data.market_baseline.value.toLocaleString("zh-CN", {maximumFractionDigits: 2})} {data.market_baseline.unit} · 观察日 {data.market_baseline.observed_at?.slice(0,10)}{data.market_baseline.status === "stale" ? "（已过期）" : ""}。价格基线单列，不重复计为事件证据。</Typography.Paragraph> : null}
          <Space wrap style={{marginBottom: 12}}>{data.coverage.map(c => <Tag key={c.mechanism} title={c.automatic_scope} color={c.status === "available" ? "blue" : c.status === "needs_review" ? "orange" : "default"}>{c.label}：{c.status === "available" ? `${c.usable_episodes}个可用事件` : c.status === "needs_review" ? "有材料待核验" : c.stored_claims ? `仅有${c.stored_claims}条历史材料` : "暂无材料"}</Tag>)}</Space>
          {data.gaps.length ? <Alert type="warning" message="证据缺口" description={<ul style={{marginBottom:0}}>{data.gaps.map(g=><li key={g}>{g}</li>)}</ul>} /> : null}
          <Tabs items={tabs} style={{marginTop: 12}} />
          <Space wrap><Typography.Text type="secondary">规则抽取材料 {data.total_claims} 条 · AI 条件材料 {conditionalReviews(data).length} 条（不计票）；规则材料第 {Math.floor(data.offset / 50) + 1} 页；来源条数不等于独立证据票数</Typography.Text>
            <Button disabled={data.offset === 0 || loading} onClick={() => void load({ target, horizon, view, offset: Math.max(0,data.offset - 50) })}>上一页证据</Button>
            <Button disabled={data.next_offset === null || loading} onClick={() => void load({ target, horizon, view, offset: data.next_offset! })}>下一页证据</Button></Space>
        </> : !loading && !error ? <Empty description="尚未读取证据" /> : null}
      </Spin>
    </Drawer>
  </>;
}
