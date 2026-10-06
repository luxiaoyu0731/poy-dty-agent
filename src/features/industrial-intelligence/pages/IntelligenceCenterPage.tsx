import WorkbenchTabs from "../../../WorkbenchTabs";
import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Card, Empty, Input, Skeleton, Space, Spin, Table, Tabs, Tag, Typography } from "antd";
import * as intelligenceApi from "../api";
import type { EventSummary } from "../api";
import { EventDetailPanel, categoryLabel } from "../components/EventDetailPanel";
import { useRadar, useRuns, useSources } from "../hooks";
import { displayEventTitle, shanghaiDateTime } from "../presentation";

const { Paragraph, Text, Title } = Typography;

// The map chunk (MapLibre + GeoJSON) loads only when the operator opens the
// map tab; the default radar view never downloads it (spec 13.4 / 14).
const IntelligenceMap = lazy(() => import("../map/IntelligenceMap"));

const PRODUCT_LABELS: Record<string, string> = {
  crude: "原油",
  naphtha: "石脑油",
  px: "PX",
  pta: "PTA",
  meg: "MEG",
  poy: "POY",
  dty: "DTY",
};

const STATUS_LABELS: Record<string, { label: string; tone: "success" | "warning" | "error" | "default" }> = {
  ready: { label: "已发布", tone: "success" },
  ready_with_gaps: { label: "已发布（含缺口）", tone: "warning" },
  no_material_events: { label: "今日无达到摘要阈值的事件", tone: "default" },
  blocked: { label: "受阻（诚实终态，非成功）", tone: "error" },
};

function statusTag(status: string) {
  const meta = STATUS_LABELS[status] ?? { label: status, tone: "default" as const };
  return <Tag color={meta.tone === "default" ? undefined : meta.tone}>{meta.label}</Tag>;
}

function formatDateTime(value: string | null | undefined): string {
  return shanghaiDateTime(value);
}

function RadarView(props: {
  selectedEventId?: string;
  onOpenEvent: (event: EventSummary) => void;
  onOpenEventById: (eventId: string) => void;
  onOpenRuns: () => void;
}) {
  const [product, setProduct] = useState<string | null>(null);
  const [category, setCategory] = useState<string | null>(null);
  const [sort, setSort] = useState<"relevance" | "recency">("recency");
  const [searchDraft, setSearchDraft] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchState, setSearchState] = useState<
    | { ok: true; hits: intelligenceApi.SearchHit[]; cursor: string | null; loadingMore: boolean }
    | { ok: false; reason: string; pending?: boolean }
    | null
  >(null);
  const radarState = useRadar({ product, category, sort });
  const searchEpoch = useRef(0);
  const [searchMoreError, setSearchMoreError] = useState("");

  useEffect(() => {
    searchEpoch.current += 1;
    setSearchMoreError("");
    const query = searchQuery.trim();
    if (query.length < 2) {
      setSearchState(null);
      return;
    }
    const controller = new AbortController();
    setSearchState({ ok: false, reason: "正在搜索", pending: true });
    intelligenceApi
      .searchEvents(query, { limit: 30 }, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted) setSearchState({ ok: true, hits: page.items, cursor: page.next_cursor, loadingMore: false });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setSearchState({ ok: false, reason: error instanceof Error ? error.message : "搜索失败" });
      });
    return () => controller.abort();
  }, [searchQuery]);

  const loadMoreSearch = () => {
    if (!searchState?.ok || !searchState.cursor || searchState.loadingMore) return;
    const epoch = searchEpoch.current;
    setSearchMoreError("");
    const query = searchQuery.trim();
    const cursor = searchState.cursor;
    setSearchState({ ...searchState, loadingMore: true });
    intelligenceApi
      .searchEvents(query, { limit: 30, cursor }, undefined)
      .then((page) => {
        if (epoch !== searchEpoch.current) return;
        setSearchState((current) => {
          if (!current?.ok) return current;
          const merged = [...current.hits, ...page.items];
          return {
            ok: true,
            hits: merged.filter((hit, index) => merged.findIndex((other) => other.ref_id === hit.ref_id && other.ref_type === hit.ref_type) === index),
            cursor: page.next_cursor,
            loadingMore: false,
          };
        });
      })
      .catch(() => {
        if (epoch !== searchEpoch.current) return;
        setSearchMoreError("下一页搜索结果未加载，请重试；当前结果已保留。");
        setSearchState((current) => (current?.ok ? { ...current, loadingMore: false } : current));
      });
  };

  return (
    <div className="radar-workspace">
      <Space wrap>
        <Input.Search
          className="radar-search"
          allowClear
          aria-label="搜索已采集事件"
          placeholder="搜索事件标题/摘要关键词…"
          enterButton="搜索"
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
          onSearch={(value) => setSearchQuery(value.trim())}
        />
        {searchQuery ? <Button onClick={() => { setSearchQuery(""); setSearchDraft(""); }}>清除搜索</Button> : null}
      </Space>
      {!searchQuery ? (
        <Space wrap className="radar-filters">
          <Text>筛选：</Text>
          <select name="intelligence-product" aria-label="按品种筛选" value={product ?? ""} onChange={(e) => setProduct(e.target.value || null)}>
            <option value="">全部品种</option>
            {Object.entries(PRODUCT_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <select name="intelligence-category" aria-label="按类别筛选" value={category ?? ""} onChange={(e) => setCategory(e.target.value || null)}>
            <option value="">全部类别</option>
            {["energy", "plant_supply", "shipping_ports", "weather_disaster", "geopolitics_sanctions", "macro_policy", "trade_regulation", "other"].map(
              (value) => (
                <option key={value} value={value}>
                  {categoryLabel(value)}
                </option>
              ),
            )}
          </select>
          <select
            name="intelligence-sort"
            aria-label="排序方式"
            value={sort}
            onChange={(e) => setSort(e.target.value === "relevance" ? "relevance" : "recency")}
          >
            <option value="recency">最新优先</option>
            <option value="relevance">相关度优先</option>
          </select>
        </Space>
      ) : null}
      <div className="radar-results">
      {searchQuery ? (
        !searchState ? null : searchState.ok ? (
          <>
            {searchState.hits.length === 0 ? (
              <Empty description={`没有匹配“${searchQuery}”的已采集事件；不代表外部没有相关新闻。`} />
            ) : (
              <ul aria-label="事件搜索结果">
                {searchState.hits.map((hit) => (
                  <li key={`${hit.ref_type}-${hit.ref_id}`}>
                    <Button type="link" style={{ padding: 0, height: "auto", whiteSpace: "normal", textAlign: "left" }} onClick={() => props.onOpenEventById(hit.ref_id)}>
                      {displayEventTitle({ title: hit.title })}
                    </Button>
                    <Space size="small" wrap>
                      <Tag>{categoryLabel(hit.category)}</Tag>
                      <Text type="secondary">{hit.ref_type === "event" ? "事件" : "条目"}</Text>
                    </Space>
                  </li>
                ))}
              </ul>
            )}
            {searchMoreError ? <Alert type="warning" message={searchMoreError} /> : null}
            {searchState.cursor ? (
              <Button loading={searchState.loadingMore} onClick={loadMoreSearch}>
                加载更多搜索结果
              </Button>
            ) : null}
          </>
        ) : searchState.pending ? (
          <Spin aria-label="正在搜索事件" />
        ) : (
          <Alert type="warning" showIcon message="事件搜索不可用" description={searchState.reason} />
        )
      ) : (
        <>

      {!radarState.status.ok ? (
        radarState.status.pending ? (
          // 雷达快照在生产上常要 20-30 秒：骨架卡片让等待读作“列表正在填充”，
          // 而不是一块空白画布。
          <ul className="radar-event-list" aria-label="正在加载全球雷达" aria-busy="true">
            {[0, 1, 2].map((row) => (
              <li key={row}>
                <Skeleton active title={{ width: row % 2 ? "72%" : "55%" }} paragraph={{ rows: 1, width: "42%" }} />
              </li>
            ))}
          </ul>
        ) : (
          <Alert type="warning" showIcon message="全球雷达不可用" description={radarState.status.reason} action={<Button onClick={radarState.reload}>重试雷达</Button>} />
        )
      ) : radarState.status.data.items.length === 0 ? (
        <Empty description="当前筛选下没有已采集信号；不代表外部没有事件。可调整筛选或查看运行与来源。"><Button onClick={props.onOpenRuns}>查看运行与来源</Button></Empty>
      ) : (
        <ul className="radar-event-list" aria-label="雷达事件列表">
          {radarState.status.data.items.map((event) => (
            <li key={event.event_revision_id}>
              <button
                type="button"
                className="radar-event-card"
                aria-pressed={props.selectedEventId === event.event_id}
                onClick={() => props.onOpenEvent(event)}
              >
                <strong>{displayEventTitle(event)}</strong>
              <span className="radar-event-meta">
                <Tag>{categoryLabel(event.category)}</Tag>
                {event.product_ids.map((product) => (
                  <Tag key={product} color="purple">
                    {PRODUCT_LABELS[product] ?? product}
                  </Tag>
                ))}
                <Tag>相关性 {event.relevance_score.toFixed(0)}</Tag>
                <Tag>证据 {event.evidence_count}</Tag>
                <span>收录 {formatDateTime(event.last_seen_at)}</span>
              </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {radarState.moreError ? <Alert type="warning" message="下一页未加载，已显示内容保留" description={radarState.moreError} /> : null}
      {radarState.status.ok && radarState.status.data.has_more ? <Button loading={radarState.loadingMore} onClick={radarState.loadMore}>加载更多事件</Button> : null}
      <Paragraph className="radar-list-note" type="secondary">
        列表使用服务端快照游标分页；时间均为系统收录时间，事件本身的发布日期在详情中逐条标注。刷新页面即可看到最新已采集内容。系统不做前端轮询，也不发送任何即时提醒。
      </Paragraph>
        </>
      )}
      </div>
    </div>
  );
}

function RunsView() {
  const [sourcePage, setSourcePage] = useState(1);
  const [sourcePageSize, setSourcePageSize] = useState(10);
  const sourcesState = useSources();
  const runsState = useRuns();
  return (
    <WorkbenchTabs labels={["来源目录", "运行审计"]} className="review-source-tabs">
      <Card title="来源目录（派生，只读）" aria-label="来源目录" extra={<Button onClick={sourcesState.reload}>刷新来源状态</Button>}>
        {!sourcesState.status.ok ? (
          sourcesState.status.pending ? <Spin aria-label="正在加载来源目录" /> : <Alert type="warning" showIcon message="来源目录不可用" description={sourcesState.status.reason} />
        ) : (
          <Table
            size="small"
            rowKey="source_id"
            className="source-catalog-table"
            tableLayout="fixed"
            scroll={{ x: 1320 }}
            dataSource={sourcesState.status.data.items.filter(source => source.operational_status !== "soft_removed")}
            columns={[
              { title: "来源", dataIndex: "display_name", width: 230, ellipsis: true },
              { title: "最近读取", dataIndex: "last_attempt_at", width: 170, render: (value: string | null) => value ? formatDateTime(value) : "无运行记录" },
              { title: "最近成功读取", dataIndex: "last_success_at", width: 170, render: (value: string | null) => value ? formatDateTime(value) : "暂无成功记录" },
              { title: "读取结果", dataIndex: "quality_status", width: 160, render: (value: string) => ({ ok: "读取成功", unchanged: "数据未变", no_relevant_items: "读取完成，无相关条目", error: "失败", timeout: "超时", partial_error: "部分失败", overdue: "超过读取周期", historical_only: "历史保留", not_observed: "无采集运行记录", observations_without_run: "已有价格，缺运行记录", degraded: "部分失败" } as Record<string, string>)[value] || value },
              { title: "来源标识", dataIndex: "source_id", width: 100, render: (value: string) => <details><summary>技术详情</summary>{value}</details> },
              { title: "级别", dataIndex: "tier", width: 64 },
              { title: "类型", dataIndex: "source_type", width: 100, render: (value: string) => ({ core: "日线/基准", news: "新闻公告", price_channel: "盘中行情", intelligence_provider: "情报数据" }[value] || value) },
              { title: "节奏", dataIndex: "cadence", width: 100, render: (value: string) => ({ business_day: "业务日", daily_public: "每日公开", daily_weekly: "日/周", daily: "每日", weekly: "每周", manual: "手工", "30min": "每30分钟" }[value] || value) },
              {
                title: "状态",
                dataIndex: "operational_status",
                render: (value: string) => <Tag color={value === "active" ? "green" : undefined}>{({ active: "已启用", disabled: "已停用", blocked: "受阻" } as Record<string, string>)[value] || value}</Tag>,
                width: 110,
              },
              {
                title: "漂移",
                width: 85,
                dataIndex: "metadata_drift",
                render: (value: boolean, record) =>
                  value ? <Tag color="orange">drift: {record.drift_fields.join(",")}</Tag> : <Tag>无</Tag>,
              },
              {
                title: "凭据",
                dataIndex: "credential_status",
                render: (value: string) => <Tag>{({ not_required: "无需凭据", configured: "已配置", missing: "待配置" } as Record<string, string>)[value] || value}</Tag>,
                width: 110,
              },
            ]}
            pagination={{ current: sourcePage, pageSize: sourcePageSize, showSizeChanger: true, pageSizeOptions: [10, 20, 50], showTotal: (total) => `共 ${total} 条`, onChange: (page, size) => { setSourcePage(size === sourcePageSize ? page : 1); setSourcePageSize(size); } }}
          />
        )}
      </Card>
      <Card title="运行审计" aria-label="运行审计" extra={<Button onClick={runsState.reload}>刷新运行记录</Button>}>
        {!runsState.status.ok ? (
          runsState.status.pending ? <Spin aria-label="正在加载运行记录" /> : <Alert type="warning" showIcon message="运行记录不可用" description={runsState.status.reason} />
        ) : (
          <Table
            size="small"
            rowKey="run_id"
            dataSource={runsState.status.data.items}
            locale={{ emptyText: "尚无工业情报运行记录。来源目录不代表采集已执行，请核对工业情报定时任务。" }}
            columns={[
              { title: "运行", dataIndex: "run_type" },
              { title: "提供方", dataIndex: "provider_id", render: (value: string | null) => value ?? "—" },
              { title: "运行开始", dataIndex: "started_at", render: (value: string | null) => value ? formatDateTime(value) : "未记录" },
              { title: "业务日期", dataIndex: "business_date", render: (value: string | null) => value ?? "—" },
              {
                title: "状态",
                dataIndex: "status",
                render: (value: string) =>
                  <Tag color={value === "succeeded" ? "green" : value === "failed" ? "red" : "orange"}>{({ succeeded: "成功", failed: "失败", running: "运行中", degraded: "降级", blocked: "受阻", skipped: "跳过" } as Record<string, string>)[value] || value}</Tag>,
              },
              {
                title: "输入/新增/已有",
                render: (_: unknown, record) => `${record.counts.input}/${record.counts.inserted}/${record.counts.existing}`,
              },
              { title: "耗时(ms)", dataIndex: "duration_ms", width: 90, render: (value: number | null) => value == null ? "未记录" : value === 0 ? "不足记录精度" : value },
              {
                title: "失败或降级原因",
                dataIndex: "degraded_reasons",
                render: (value: string[], record) => {
                  const reasons = [...(value || []), record.error_code, record.error_detail_safe].filter(Boolean);
                  return reasons.length ? reasons.join("；") : record.status === "failed" ? "未返回具体原因，请核对运行日志" : "—";
                },
              },
            ]}
            pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50], showTotal: (total) => `共 ${total} 条` }}
          />
        )}
      </Card>
    </WorkbenchTabs>
  );
}

class MapErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <Alert type="warning" showIcon message="地图组件无法加载" description="可在全球雷达查看事件文字与来源；刷新页面后可以重试地图。" action={<Button onClick={() => window.location.reload()}>刷新页面</Button>} /> : this.props.children;
  }
}

function MapView(props: { onOpenEvent: (eventId: string) => void }) {
  return (
    <MapErrorBoundary>
    <Suspense fallback={<Spin aria-label="正在加载地图组件" />}>
      <IntelligenceMap onSelectEvent={props.onOpenEvent} />
    </Suspense>
    </MapErrorBoundary>
  );
}

/**
 * The eighth workbench module. Default subview is the global radar; the map
 * is a secondary lazy view. This page owns its own data fetching and never
 * joins the legacy workbench refresh graph.
 */
type IntelligenceView = "radar" | "runs" | "map";
function readIntelligenceView(): IntelligenceView {
  const value = new URLSearchParams(window.location.search).get("intelligenceView");
  return value === "runs" || value === "map" ? value : "radar";
}

export function IntelligenceCenterPage(): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [subView, setSubView] = useState<IntelligenceView>(readIntelligenceView);
  const changeSubView = (view: IntelligenceView) => {
    // Move focus before hiding the pane containing the shortcut button.
    const tab = containerRef.current?.querySelector<HTMLElement>(`[role="tab"][id$="-tab-${view}"]`);
    tab?.focus();
    const url = new URL(window.location.href);
    url.searchParams.set("intelligenceView", view);
    if (url.href !== window.location.href) window.history.pushState(null, "", url);
    eventRequest.current?.abort();
    setSelectedEvent(null);
    setEventError(null);
    setSubView(view);
  };
  useEffect(() => {
    const restore = () => { eventRequest.current?.abort(); setSelectedEvent(null); setEventError(null); setSubView(readIntelligenceView()); };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  const [selectedEvent, setSelectedEvent] = useState<EventSummary | null>(null);
  const [eventError, setEventError] = useState<string | null>(null);
  const eventRequest = useRef<AbortController | null>(null);
  useEffect(() => () => eventRequest.current?.abort(), []);

  const openByEventId = (eventId: string) => {
    eventRequest.current?.abort();
    const controller = new AbortController();
    eventRequest.current = controller;
    setEventError(null);
    intelligenceApi
      .getEvent(eventId, controller.signal)
      .then((event) => {
        if (!controller.signal.aborted) setSelectedEvent(event);
      })
      .catch(() => { if (!controller.signal.aborted) setEventError("事件详情未加载，请重新选择该事件或在全球雷达查看。"); });
  };

  return (
    <div ref={containerRef} className="intelligence-center" aria-label="工业情报中心">
      <Tabs
        activeKey={subView}
        onChange={(key) => changeSubView(key as IntelligenceView)}
        items={[
          { key: "radar", label: "全球雷达", children: <div className="review-radar-layout"><RadarView selectedEventId={selectedEvent?.event_id} onOpenEvent={(event) => { eventRequest.current?.abort(); setEventError(null); setSelectedEvent(event); }} onOpenEventById={openByEventId} onOpenRuns={() => changeSubView("runs")} /><EventDetailPanel inline event={selectedEvent} onClose={() => setSelectedEvent(null)} /></div> },
          { key: "runs", label: "运行与来源", children: <RunsView /> },
          { key: "map", label: "全球态势地图", children: subView === "map" ? <MapView onOpenEvent={openByEventId} /> : null },
        ]}
      />
      {eventError ? <Alert type="warning" message={eventError} closable onClose={() => setEventError(null)} /> : null}
      {subView !== "radar" ? <EventDetailPanel event={selectedEvent} onClose={() => setSelectedEvent(null)} /> : null}
    </div>
  );
}

export default IntelligenceCenterPage;
