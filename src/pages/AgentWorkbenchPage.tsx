import { requestDeadline as uiRequestTimeout } from "../utils/requestDeadline";
import { pipelineCardGeometry, pipelineLaneGeometry, pipelineRowGeometry } from "../components/pipelineCardGeometry";
import { pipelineRelayContracts } from "../components/pipelineRelayContracts";
import { pipelineMemoryObservation } from "../components/pipelineMemoryObservation";
import { ContentLoading } from "../components/ContentLoading";
import { AgentImplementationProfile } from "../components/AgentImplementationProfile";
import { pipelineEdgeTypes } from "../components/PipelineRoutedEdge";
import { pipelineNodeSummary } from "../components/pipelineNodeSummary";
import { pipelineLenses, PipelineTechnicalDrawer } from "../components/PipelineTechnicalViews";
import { evidenceTarget, evidenceTargetFromQuestion } from "../features/evidence-system/types";
import { EvidenceDossierButton } from "../features/evidence-system/EvidenceDossierButton";
import { EvidenceVerificationBoard } from "../features/evidence-system/EvidenceVerificationBoard";
import WorkbenchBrand from "../WorkbenchBrand";
import WorkbenchTabs from "../WorkbenchTabs";
import { Cylinder, Factory, Hexagon, FlaskConical, Network, Layers } from "lucide-react";
import { priceSourceLabel } from "../utils/priceSourceLabel";
import { MARKET_DAY_MS, marketDay, marketSeriesLabel } from "../utils/marketTimeline";
import { PublicQuoteHistory } from "../components/PublicQuoteHistory";
import { splitAssistantCitations } from "../utils/assistantCitationText";
import { InformationReports } from "../components/InformationReports";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import {
  App as AntApp,
  Alert,
  Button,
  ConfigProvider,
  Drawer,
  Empty,
  Input,
  Segmented,
  Spin,
  Tag,
  Typography
} from "antd";
import {
  AlertOutlined,
  ApartmentOutlined,
  AppstoreOutlined,
  ArrowUpOutlined,
  ArrowRightOutlined,
  BankOutlined,
  BarChartOutlined,
  BellOutlined,
  CheckCircleFilled,
  CheckCircleOutlined,
  CheckSquareOutlined,
  ClockCircleOutlined,
  CloudSyncOutlined,
  ClusterOutlined,
  CommentOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FileDoneOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  FilterOutlined,
  InfoCircleOutlined,
  LineChartOutlined,
  LinkOutlined,
  MenuOutlined,
  MinusCircleOutlined,
  NodeIndexOutlined,
  PartitionOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RiseOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SendOutlined,
  SettingOutlined,
  StarOutlined,
  SwapOutlined,
  LogoutOutlined,
  ThunderboltOutlined,
  UserOutlined,
  WarningFilled,
  WarningOutlined,
  GlobalOutlined
} from "@ant-design/icons";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useReactFlow,
  useNodesInitialized,
  type Edge,
  type Node,
  type NodeProps
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { formatMarketFreshnessSummary, formatDateRangesInText, formatDisplayDate, formatDisplayTimestamp, formatTimestampsInText } from "../utils/displayFormatting";
import type {
  AgentRunStatus,
  AssistantAnswerSections,
  AssistantChatResponse,
  AssistantEvidenceView,
  AssistantQualityGate,
  AgentTraceTurn,
  AgentWorkbenchData,
  DeliveryClientReport,
  DeliverySourceAutomation,
  EventImpact,
  EventLibraryItem,
  EventLibraryWorkbenchResponse,
  FactorScore,
  FormalPredictionBatch,
  PredictionEventFactors,
  ForecastPricePoint,
  FullChainSummaryItem,
  FullChainSummaryResponse,
  LatestPriceItem,
  LatestPricesResponse,
  MarketChainProductView,
  MarketChainWorkbenchResponse,
  MorningBriefItem,
  NewsArticle,
  NewsEventCluster,
  OverviewResponse,
  PipelineDailyCost,
  PipelineGraphEdge,
  PipelineGraphNode,
  PipelineGraphResponse,
  PipelineNodeInspectionResponse,
  PipelineNodeStatus,
  PriceComparison,
  RagEvidence,
  RagGraphResponse,
  RagSearchResponse,
  PredictionReview,
  PredictionObservation,
  SevenProductEvaluationBatch,
  SevenProductForecastBatch,
  SevenProductForecastLedgerBatch,
  StoredPrediction,
  WorkbenchRagVisualResponse
} from "../services/api";
import {
  AgentLessonsResponse,
  GovernanceReportResponse,
  api,
} from "../services/api";
import { EventEvidenceCard, FormalPredictionBatchView } from "../components/domain";
import { SevenProductForecastGrid, SevenProductIssuedHistory } from "../components/prediction";

// The eighth module is code-split: its chunk (and the lazy map chunk inside
// it) is only fetched when the operator navigates to it.
const IntelligenceCenterPage = lazy(
  () => import("../features/industrial-intelligence/pages/IntelligenceCenterPage")
);

// The market trend chart pulls the recharts chunk; it is only needed when the
// market module actually renders its price panel.
const MarketTrendChart = lazy(() =>
  import("../components/MarketTrendChart").then((m) => ({ default: m.MarketTrendChart }))
);

const { Paragraph, Text, Title } = Typography;

type ModuleId =
  | "overview"
  | "market"
  | "evidence"
  | "workflow"
  | "assistant"
  | "reports"
  | "intelligence";

type ReportWorkspace = "reports" | "ledger";
type ReportType = "日报" | "周报" | "复盘报告" | "专题报告";

const reportTypes: ReportType[] = ["日报", "周报", "复盘报告", "专题报告"];

function readNavigationState() {
  const params = new URLSearchParams(window.location.search);
  const moduleParam = params.get("module");
  const module = navigationItems.some((item) => item.id === moduleParam)
    ? moduleParam as ModuleId
    : "overview";
  const reportView = params.get("reportView") === "ledger" ? "ledger" : "reports";
  const reportTypeParam = params.get("reportType");
  const reportType = reportTypes.includes(reportTypeParam as ReportType)
    ? reportTypeParam as ReportType
    : "日报";
  return { module, reportView: reportView as ReportWorkspace, reportType };
}

function writeNavigationState(module: ModuleId, reportView: ReportWorkspace, reportType: ReportType, mode: "replace" | "push" = "replace") {
  const href = navigationHref(module, reportView, reportType);
  window.history[mode === "push" ? "pushState" : "replaceState"](null, "", href);
}

function navigationHref(module: ModuleId, reportView: ReportWorkspace, reportType: ReportType) {
  const url = new URL(window.location.href);
  url.searchParams.set("module", module);
  if (module === "reports") {
    url.searchParams.set("reportView", reportView);
    if (reportView === "reports") url.searchParams.set("reportType", reportType);
    else url.searchParams.delete("reportType");
  } else {
    url.searchParams.delete("reportView");
    url.searchParams.delete("reportType");
  }
  // Hash routes belonged to the retired workspace. Keeping them on new links
  // can reopen a stale view and also makes the skip link target ambiguous.
  return `${url.pathname}${url.search}`;
}

type Tone = "normal" | "success" | "warning" | "danger" | "info" | "muted";

type NavigationItem = {
  id: ModuleId;
  label: string;
  description: string;
  icon: ReactNode;
};

type BusinessEvent = {
  id: string;
  category: string;
  categoryKey?: string;
  title: string;
  time: string;
  timeLabel?: string;
  sourceTitleRaw?: string;
  factSummary?: string;
  overviewText?: string;
  overviewBasis?: "title" | "body" | null;
  factSummaryReady?: boolean;
  summaryGenerationStatus?: EventLibraryItem["summary_generation_status"];
  summaryStatusLabel?: string;
  sourceContentStatus?: "full_text" | "partial_text" | "title_only" | string;
  sourceContentStatusLabel?: string;
  sourceName?: string;
  sourceUrl?: string;
  analysisAvailable?: boolean;
  analysisReady?: boolean;
  direction?: string;
  affectedProducts?: string[];
  impactStrength?: string;
  evidenceLevel?: string;
  impact: string;
  evidence: string[];
  counter: string[];
  change: string;
  tone: Tone;
  links: Array<{ label: string; href?: string }>;
  political?: {
    summary: string;
    interestMap: string[];
    powerStructure: string[];
    stakeholders: Array<{
      name: string;
      role: string;
      interest: string;
      boundary: string;
    }>;
    speechAct: {
      label: string;
      reason: string;
    };
    executionLikelihood: {
      label: string;
      reason: string;
    };
    priceInStatus: {
      label: string;
      reason: string;
    };
    actionBoundary: string[];
    secondOrderRisks: string[];
    sourceBasis: string[];
  };
};

type PipelineFlowNodeData = {
  nodeId: string;
  kind: "code" | "agent";
  name: string;
  status?: PipelineNodeStatus;
  statusDetail: string;
  timestamp: string;
  budget?: { used?: number; cap: number; fallback?: number };
  lensActive?: boolean;
  compact?: boolean;
  memoryPlanning?: boolean;
};

type AssistantEvidence = {
  id?: string;
  category: string;
  title: string;
  reason: string;
  tone?: Tone;
  observed?: string;
  url?: string;
};

type ChatMessage = {
  contextPackId?: string | null;
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence?: AssistantEvidence[];
  evidenceGroups?: {
    adopted: AssistantEvidence[];
    referenceMaterials: AssistantEvidence[];
    excluded: AssistantEvidence[];
    conflicts: AssistantEvidence[];
  };
  sections?: AssistantAnswerSections;
  status?: "success" | "degraded" | "timeout" | "failed";
  fallbackReason?: string;
  question?: string;
  qualityGates?: AssistantQualityGate[];
  qualityOverall?: "all_passed" | "passed_with_flags";
};

type DataStatus<T> =
  | { ok: true; value: T }
  | { ok: false; reason: string; pending?: boolean };

type WorkbenchLiveData = {
  overview: DataStatus<OverviewResponse>;
  events: DataStatus<EventImpact[]>;
  eventLibrary: DataStatus<EventLibraryWorkbenchResponse>;
  morningBrief: DataStatus<MorningBriefItem[]>;
  factors: DataStatus<FactorScore[]>;
  latestPrices: DataStatus<LatestPricesResponse>;
  priceComparison: DataStatus<PriceComparison>;
  marketChain: DataStatus<MarketChainWorkbenchResponse>;
  retrieval: DataStatus<RagSearchResponse>;
  graph: DataStatus<RagGraphResponse>;
  fullChain: DataStatus<FullChainSummaryResponse>;
  ragVisual: DataStatus<WorkbenchRagVisualResponse>;
  formalPredictions: DataStatus<FormalPredictionBatch[]>;
  eventFactors: DataStatus<PredictionEventFactors>;
};

const navigationItems: NavigationItem[] = [
  { id: "overview", label: "总览看板", description: "今日结论与风险", icon: <BarChartOutlined /> },
  { id: "market", label: "行情与原料链", description: "价格与传导关系", icon: <RiseOutlined /> },
  { id: "evidence", label: "证据图谱", description: "结论证据链路", icon: <NodeIndexOutlined /> },
  { id: "workflow", label: "Agent 系统", description: "每日研判执行与 Agent 交接", icon: <ApartmentOutlined /> },
  { id: "assistant", label: "AI 研判助手", description: "证据问答", icon: <CommentOutlined /> },
  { id: "reports", label: "研判报告", description: "日报周报与复盘", icon: <FileTextOutlined /> },
  { id: "intelligence", label: "工业情报中心", description: "全球雷达与来源观察", icon: <GlobalOutlined /> }
];

const statusLabels: Record<AgentRunStatus, string> = {
  running: "运行中",
  success: "正常",
  failed: "异常",
  blocked: "需处理",
  needs_human_review: "需关注"
};

const statusTone: Record<AgentRunStatus, Tone> = {
  running: "info",
  success: "success",
  failed: "danger",
  blocked: "warning",
  needs_human_review: "warning"
};

const toneColor: Record<Tone, string> = {
  normal: "default",
  success: "success",
  warning: "warning",
  danger: "error",
  info: "processing",
  muted: "default"
};

const forbiddenFieldPattern = /[A-Za-z]+(?:_[A-Za-z0-9]+)+|\b[a-z]+[A-Z][A-Za-z0-9]*\b/g;

function formatDate(value?: string | null) {
  if (!value) return "暂无时间";
  return formatDisplayTimestamp(value) ?? cleanBusinessText(value.slice(0, 16).replace("T", " "));
}

function formatPercent(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "暂无数据";
  return `${(value * 100).toFixed(1)}%`;
}

function formatInteger(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "暂无数据";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function readableUnit(value?: string | null) {
  if (!value) return "";
  const normalized = value.trim();
  if (/^CNY\/mt$/i.test(normalized) || normalized === "元/吨") return "元/吨";
  if (/^USD\/mt$/i.test(normalized) || normalized === "美元/吨") return "美元/吨";
  if (/^USD\/bbl$/i.test(normalized) || /^dollars_per_barrel$/i.test(normalized) || normalized === "$/BBL") return "美元/桶";
  if (/^MBBL$/i.test(normalized)) return "千桶";
  if (/^MBBL\/D$/i.test(normalized)) return "千桶/日";
  if (/^percent$/i.test(normalized) || normalized === "%") return "%";
  if (/^RMB\/ton$/i.test(normalized)) return "元/吨";
  if (/contracts/i.test(normalized)) return "张";
  return cleanBusinessText(normalized);
}

function externalEvidenceUrl(value?: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch {
    return undefined;
  }
}

function formatPrice(value?: number | null, unit?: string | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "未返回";
  const formatted = Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const unitLabel = readableUnit(unit);
  return unitLabel ? `${formatted} ${unitLabel}` : formatted;
}

function compactText(value?: string | null, maxLength = 120) {
  const text = cleanBusinessText(value, "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

function isMostlyEnglishText(value?: string | null) {
  const text = cleanBusinessText(value, "").replace(/\s+/g, "");
  if (!text) return false;
  const asciiLetters = (text.match(/[A-Za-z]/g) ?? []).length;
  const cjkLetters = (text.match(/[\u4e00-\u9fa5]/g) ?? []).length;
  return asciiLetters > 12 && asciiLetters > cjkLetters * 2;
}

function eventBusinessTitle(params: {
  category?: string | null;
  title?: string | null;
  summary?: string | null;
  impact?: string | null;
  affected?: string[];
  direction?: string | null;
}) {
  const source = cleanBusinessText(params.title, "");
  if (source) return compactText(source, 96);
  return `${cleanBusinessText(params.category, "事件")}事件`;
}

function eventBusinessImpact(params: {
  category?: string | null;
  summary?: string | null;
  impact?: string | null;
  affected?: string[];
  direction?: string | null;
}) {
  const summary = cleanBusinessText(params.summary || params.impact, "");
  if (summary && !isMostlyEnglishText(summary)) return compactText(summary, 220);
  const category = cleanBusinessText(params.category, "事件");
  const affected = (params.affected ?? []).filter(Boolean).slice(0, 6);
  const affectedText = affected.length ? affected.join("、") : "原油到聚酯链路";
  const direction = cleanBusinessText(params.direction, "");
  const directionText = direction && direction !== "中性" && direction !== "待确认" ? `，初步方向${direction}` : "";
  return `${category}事件影响${affectedText}${directionText}。当前按风险溢价、成本传导和下游承接三条线观察，只有价格、库存、开工或利润出现同向信号时，才升级今日判断。`;
}

function settledData<T>(result: PromiseSettledResult<T>): DataStatus<T> {
  if (result.status === "fulfilled") return { ok: true, value: result.value };
  return { ok: false, reason: result.reason instanceof Error ? result.reason.message : "数据暂未返回" };
}

function settledDataValidated<T>(result: PromiseSettledResult<T>, valid: (value: T) => boolean, reason: string): DataStatus<T> {
  const settled = settledData(result);
  if (!settled.ok) return settled;
  return valid(settled.value) ? settled : { ok: false, reason };
}


async function uiRequestWithRetry<T>(
  request: () => Promise<T>,
  ms: number,
  label: string,
  retries = 1
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await uiRequestTimeout(request(), ms, label);
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        await new Promise((resolve) => window.setTimeout(resolve, 400));
      }
    }
  }
  throw lastError;
}

type LiveDataKey = keyof WorkbenchLiveData;

const unloadedLiveData = (): WorkbenchLiveData => ({
  overview: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  events: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  eventLibrary: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  morningBrief: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  factors: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  latestPrices: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  priceComparison: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  marketChain: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  retrieval: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  graph: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  fullChain: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  ragVisual: { ok: false, reason: "当前模块尚未加载此数据", pending: true },
  formalPredictions: { ok: false, reason: "正式格 0/21：OOS 正式门未过，当前以观察口径每日发牌（治理状态，非数据缺失）", pending: true },
  eventFactors: { ok: false, reason: "事件依据卡尚未生成", pending: true }
});

const moduleLiveDependencies: Record<ModuleId, LiveDataKey[]> = {
  overview: ["fullChain", "overview", "morningBrief", "events", "eventLibrary", "latestPrices", "marketChain", "formalPredictions", "eventFactors"],
  market: ["fullChain", "factors", "latestPrices", "priceComparison", "marketChain"],
  evidence: ["fullChain", "overview", "retrieval", "graph", "ragVisual"],
  workflow: ["fullChain"],
  assistant: ["fullChain", "overview", "events", "eventLibrary", "retrieval", "graph", "ragVisual"],
  reports: ["fullChain", "overview", "morningBrief", "events", "eventLibrary", "ragVisual"],
  intelligence: []
};

let cachedWorkbenchData: AgentWorkbenchData | undefined;
let cachedLiveData = unloadedLiveData();
let workbenchRequest: Promise<AgentWorkbenchData> | undefined;
let serverSnapshotRequest: Promise<boolean> | undefined;
const liveRequests = new Map<LiveDataKey, Promise<DataStatus<unknown>>>();
const lockedSnapshotKeys = new Set<LiveDataKey>();
const snapshotSeededLiveKeys = new Set<LiveDataKey>();
// Market figures move intraday; a long-lived tab must not keep showing the
// values from its first successful load until the operator hard-refreshes.
const MARKET_REVALIDATE_MS = 10 * 60 * 1000;
const marketRevalidateKeys = new Set<LiveDataKey>(["latestPrices", "marketChain"]);
const liveDataSuccessAt = new Map<LiveDataKey, number>();
const lastDisplayedMarketPriceByProduct = new Map<string, LatestPriceItem>();
let serverSnapshotUnavailable = false;

// Business-day age of a frozen judgement in Shanghai time. Snapshots stop
// materializing whenever Agent runs end at needs_human_review, so an old
// business_date here means "no fresh daily judgement", never "prices are old".
function marketSnapshotAgeDays(businessDate: string): number {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(businessDate ?? ""));
  if (!match) return Number.POSITIVE_INFINITY;
  const shanghai = new Date(Date.now() + 8 * 3_600_000);
  const snapshotDay = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const today = Date.UTC(shanghai.getUTCFullYear(), shanghai.getUTCMonth(), shanghai.getUTCDate());
  return Math.round((today - snapshotDay) / 86_400_000);
}

function liveDataFromServerSnapshot(snapshot: Awaited<ReturnType<typeof api.workbenchSnapshot>>): Partial<WorkbenchLiveData> {
  lockedSnapshotKeys.clear();
  snapshotSeededLiveKeys.clear();
  const frozen = snapshot.workbench;
  if (!frozen) return {};
  // The judgement snapshot feeds the overview, but its frozen full-chain view
  // doubles as the 判断依据 price evidence. A snapshot older than 3 business
  // days must not masquerade as current price evidence (e.g. a stale CCF-era
  // July card in September); leave fullChain unseeded so the live
  // /full-chain/summary request supplies the accepted current sources.
  const snapshotAgeDays = marketSnapshotAgeDays(snapshot.business_date);
  const snapshotFresh = snapshotAgeDays <= 3;
  const locked: LiveDataKey[] = snapshotFresh
    ? ["overview", "fullChain", "factors", "morningBrief", "events", "formalPredictions"]
    : ["overview", "factors", "morningBrief", "events", "formalPredictions"];
  locked.forEach((key) => lockedSnapshotKeys.add(key));
  // Market fields may only be seeded from a business-fresh snapshot. A frozen
  // judgement older than 3 business days (e.g. snapshots starved because Agent
  // runs end at needs_human_review) must never masquerade as current prices —
  // leave those keys unseeded so the live market-chain request supplies them,
  // or the module shows its honest degraded state.
  const marketSeedable = snapshotFresh;
  if (marketSeedable) snapshotSeededLiveKeys.add("latestPrices");
  if (marketSeedable) snapshotSeededLiveKeys.add("marketChain");
  return {
    overview: { ok: true, value: frozen.judgement.overview },
    fullChain: snapshotFresh
      ? { ok: true, value: frozen.judgement.full_chain }
      : { ok: false, reason: "日度快照已过期，正在读取当前验收源价格链" },
    factors: { ok: true, value: frozen.judgement.factors },
    latestPrices: marketSeedable ? { ok: true, value: frozen.market.latest_prices } : { ok: false, reason: "日度快照已过期，正在读取实时价格" },
    marketChain: marketSeedable ? { ok: true, value: frozen.market.chain } : { ok: false, reason: "日度快照已过期，正在读取实时行情链" },
    morningBrief: { ok: true, value: frozen.briefing.morning_brief },
    events: { ok: true, value: frozen.briefing.events },
    formalPredictions: { ok: true, value: frozen.judgement.formal_predictions }
  };
}

async function loadServerSnapshot(force = false): Promise<boolean> {
  if (serverSnapshotUnavailable && !force) return false;
  if (force) serverSnapshotUnavailable = false;
  if (serverSnapshotRequest) {
    if (!force) return serverSnapshotRequest;
    await serverSnapshotRequest;
  }
  serverSnapshotRequest = (async () => {
    try {
      // Cold-cache snapshot assembly on a small cloud host can exceed 15s once;
      // the 30s budget lets the first visitor of the day get live data instead
      // of falling back to yesterday's saved snapshot.
      const snapshot = await uiRequestTimeout(api.workbenchSnapshot(), 30_000, "统一工作台快照");
      // The daily snapshot is the initial fallback for market data, but a
      // forced refresh must not replace a newer runtime success with that
      // older fallback. During an explicit refresh, every usable market field
      // is the last committed success even if it originally came from a daily
      // snapshot; the live refresh below may replace it only on success.
      const preservedMarketKeys = (["latestPrices", "marketChain"] as const).filter(
        (key) => cachedLiveData[key].ok && (force || !snapshotSeededLiveKeys.has(key))
      );
      const snapshotData = liveDataFromServerSnapshot(snapshot);
      preservedMarketKeys.forEach((key) => {
        delete snapshotData[key];
        snapshotSeededLiveKeys.delete(key);
      });
      cachedLiveData = { ...cachedLiveData, ...snapshotData };
      return true;
    } catch (error) {
      serverSnapshotUnavailable = error instanceof Error && /\b404\b/.test(error.message);
      return false;
    }
  })();
  try {
    return await serverSnapshotRequest;
  } finally {
    serverSnapshotRequest = undefined;
  }
}

async function fetchLiveField(key: LiveDataKey, force = false): Promise<DataStatus<unknown>> {
  if (lockedSnapshotKeys.has(key) && cachedLiveData[key].ok) return cachedLiveData[key] as DataStatus<unknown>;
  const marketStale =
    marketRevalidateKeys.has(key) && Date.now() - (liveDataSuccessAt.get(key) ?? 0) > MARKET_REVALIDATE_MS;
  if (!force && !marketStale && !snapshotSeededLiveKeys.has(key) && cachedLiveData[key].ok) {
    return cachedLiveData[key] as DataStatus<unknown>;
  }
  const pending = liveRequests.get(key);
  if (pending) {
    if (!force) return pending;
    // A forced refresh must observe a request started for this refresh. Reusing
    // an earlier request in its final cleanup microtask can falsely turn an
    // operator-visible outage into a successful partial refresh.
    await pending;
  }
  const canonicalAsOf = cachedLiveData.fullChain.ok ? cachedLiveData.fullChain.value.as_of_time : undefined;
  const request = (async (): Promise<DataStatus<unknown>> => {
    try {
      const value = await ({
        fullChain: () => uiRequestTimeout(api.fullChainSummary(), 8_000, "全链路实验"),
        overview: () => uiRequestWithRetry(() => api.overview(canonicalAsOf), 15_000, "总览数据"),
        events: () => uiRequestTimeout(api.events(), 8_000, "事件摘要"),
        eventLibrary: () => uiRequestTimeout(api.eventLibraryWorkbench(), 12_000, "事件库"),
        morningBrief: () => uiRequestWithRetry(() => api.morningBrief(), 15_000, "晨报摘要"),
        factors: () => uiRequestWithRetry(() => api.factors(), 15_000, "因子数据"),
        latestPrices: () => uiRequestWithRetry(() => api.latestPrices(), 15_000, "最新价格"),
        priceComparison: () => uiRequestWithRetry(() => api.priceComparisonForProduct("crude_oil"), 15_000, "原油价格"),
        marketChain: () => uiRequestWithRetry(() => api.marketChainWorkbench(), 30_000, "行情链路", 0),
        retrieval: () => uiRequestWithRetry(() => api.knowledgeRetrieval("POY DTY 上游原料 今日研判", 8), 30_000, "证据检索", 0),
        graph: () => uiRequestWithRetry(() => api.knowledgeGraph({ q: "POY DTY 上游原料 今日研判", limit: 80 }), 30_000, "证据关系"),
        ragVisual: () => uiRequestWithRetry(() => api.workbenchRagVisual("当前证据是否支持 POY/DTY 上游成本压力判断？", "POY", 8, canonicalAsOf), 30_000, "本轮证据快照", 0),
        formalPredictions: async () => {
          if (cachedLiveData.formalPredictions.ok) return cachedLiveData.formalPredictions.value;
          throw new Error("正式预测批次尚未进入日度快照");
        },
        eventFactors: () => uiRequestWithRetry(() => api.predictionEventFactors(), 65_000, "事件依据卡"),
      } satisfies Record<LiveDataKey, () => Promise<unknown>>)[key]();
      if (key === "retrieval" && !Array.isArray((value as RagSearchResponse).documents)) return { ok: false, reason: "证据检索返回格式不可用" };
      if (key === "graph" && (!Array.isArray((value as RagGraphResponse).nodes) || !Array.isArray((value as RagGraphResponse).edges))) return { ok: false, reason: "证据关系返回格式不可用" };
      liveDataSuccessAt.set(key, Date.now());
      return { ok: true, value };
    } catch (error) {
      const reason = error instanceof Error ? error.message : "数据暂未返回";
      // A read deadline cannot prove an HTTP failure. Preserve an honest pending
      // state rather than turning a slow endpoint into a connection alarm.
      if (key === "eventFactors" && /超时|秒内返回/.test(reason)) {
        return { ok: false, pending: true, reason: "事件依据卡读取较慢，状态尚未确认；可重新读取。" };
      }
      return { ok: false, reason };
    }
  })();
  liveRequests.set(key, request);
  try {
    return await request;
  } finally {
    if (liveRequests.get(key) === request) liveRequests.delete(key);
  }
}

async function fetchLiveDataForModule(
  module: ModuleId,
  force = false,
  committedFallback?: WorkbenchLiveData,
  perFieldCommit = false,
  onProgress?: (live: WorkbenchLiveData) => void
): Promise<WorkbenchLiveData> {
  const dependencies = moduleLiveDependencies[module];
  const previousModuleData = new Map<LiveDataKey, DataStatus<unknown>>(
    dependencies.map((key) => [key, (committedFallback ?? cachedLiveData)[key] as DataStatus<unknown>])
  );
  if (dependencies.includes("fullChain")) {
    const fullChain = await fetchLiveField("fullChain", force);
    if (fullChain.ok || !cachedLiveData.fullChain.ok) {
      cachedLiveData = { ...cachedLiveData, fullChain: fullChain as WorkbenchLiveData["fullChain"] };
      if (!force || perFieldCommit) onProgress?.({ ...cachedLiveData });
    }
  }
  const remaining = dependencies.filter((key) => key !== "fullChain");
  const results: (readonly [LiveDataKey, DataStatus<unknown>])[] = new Array(remaining.length);
  let next = 0;
  const readWorker = async () => {
    while (next < remaining.length) {
      const index = next++;
      const key = remaining[index];
      const result = await fetchLiveField(key, force);
      results[index] = [key, result];
      // Initial/background reads reveal successful fields immediately. Explicit
      // refresh keeps its existing atomic rollback unless recovery opts in.
      if ((!force || perFieldCommit) && !marketRevalidateKeys.has(key)) {
        if (result.ok || !cachedLiveData[key].ok) cachedLiveData = { ...cachedLiveData, [key]: result };
        if (result.ok) snapshotSeededLiveKeys.delete(key);
        onProgress?.({ ...cachedLiveData });
      }
    }
  };
  // Overview has eight additional reads: bound only its fan-out so heavier
  // evidence reads do not compete with every first-screen request at once.
  await Promise.all(Array.from({ length: module === "overview" ? Math.min(3, remaining.length) : remaining.length }, readWorker));
  if (force && !perFieldCommit && results.some(([, result]) => !result.ok)) {
    // An explicit module refresh is atomic from the operator's perspective.
    // If one dependency fails, keep the complete last-successful module view
    // instead of publishing a mixture of a daily fallback and partial results.
    dependencies.forEach((key) => {
      const previous = previousModuleData.get(key);
      if (previous) cachedLiveData = { ...cachedLiveData, [key]: previous };
    });
    return cachedLiveData;
  }
  results.forEach(([key, result]) => {
    // A background refresh must not replace usable cached content with an
    // intermittent network error. The affected card keeps its last snapshot.
    if (result.ok || !cachedLiveData[key].ok) cachedLiveData = { ...cachedLiveData, [key]: result };
    if (result.ok) snapshotSeededLiveKeys.delete(key);
  });
  return cachedLiveData;
}

function cleanBusinessText(value?: string | null, fallback = "暂无数据") {
  if (!value?.trim()) return fallback;
  const replacements: Array<[RegExp, string]> = [
    // Source-side encoding damage: strip replacement characters, they carry no
    // information and render as "���" noise in titles and summaries.
    [/\uFFFD+/g, ""],
    [/待人工(?:复核|确认|审核|核验)/g, "待复核"],
    [/需(?:要)?人工(?:复核|确认|审核|核验)/g, "需复核"],
    [/人工(?:复核|确认|审核|核验)记录/g, "系统复核记录"],
    [/人工(?:复核|确认|审核|核验)/g, "证据复核"],
    [/(?:人工|真人)(?:签署|签字)/g, "系统复核"],
    [/POY\/DTY\s*上游原料周期变化是否支持今日业务行动[？?]?/g, "当前证据是否支持 POY/DTY 上游原料成本压力判断？"],
    [/是否足以支持今日业务行动[？?]?/g, "是否足以支持上游原料成本压力判断？"],
    [/full[_-]?chain/gi, "成本链判断"],
    [/crude[_-]?oil/gi, "原油"],
    [/data quality\s*good/gi, "数据质量覆盖良好"],
    [/数据质量\s*good/gi, "数据质量覆盖良好"],
    [/\bdata quality\s*usable\b/gi, "数据质量可用于观察"],
    [/数据质量\s*usable/gi, "数据质量可用于观察"],
    [/\busable\b/gi, "可用于观察"],
    [/\bgood\b/gi, "覆盖良好"],
    [/strict/gi, "严格口径"],
    [/GraphRAG/gi, "证据图谱"],
    [/DeepSeek/gi, "AI 研判服务"],
    [/README/gi, "项目说明"],
    [/AGENTS/gi, "工作说明"],
    [/runbook/gi, "更新流程"],
    [/architecture/gi, "系统结构"],
    [/Safari/gi, "浏览器"],
    [/authorized/gi, "已接入"],
    [/\bhorizon\b/gi, "周期"],
    [/CCF/gi, "行业价格数据"],
    [/\bRAG\b/gi, "证据检索"],
    [/Computer Use/gi, "人工处理"],
    [/hit[_-]?rate/gi, "命中比例"],
    [/scored/gi, "历史验证"],
    [/\bpending\b/gi, "观察样本"],
    [/leaks/gi, "时点边界"],
    [/source[_-]?id/gi, "来源"],
    [/dataset[_-]?type/gi, "数据类型"],
    [/action[_-]?state/gi, "状态"],
    [/quality[_-]?gates/gi, "数据边界"],
    [/coverage[_-]?gaps/gi, "覆盖边界"],
    [/backtest[_-]?metrics/gi, "历史表现"],
    [/replenishment[_-]?tasks/gi, "补数任务"],
    [/derived[_-]?graph/gi, "证据关系"],
    [/run[_-]?id/gi, "运行记录"],
    [/agent[_-]?id/gi, "处理环节"],
    [/API key/gi, "系统配置"],
    [/\bAPI\b/gi, "系统接口"],
    [/internal[_-]?weak[_-]?signal/gi, "人工市场线索"],
    [/need[_-]?at[_-]?least[_-]?two[_-]?points[_-]?in[_-]?lookback/gi, "观察窗口内至少需要两个可审计价格点"],
    [/\binternal\b/gi, "内部线索"],
    [/\brealtime\b/gi, "实时更新"],
    [/\bcommodity\b/gi, "商品链路"],
    [/provider/gi, "服务"],
    [/token/gi, "凭据"],
    [/prompt/gi, "指令"],
    [/password/gi, "敏感信息"],
    [/login/gi, "人工处理"],
    [/授权/g, "可用"],
    [/登录/g, "人工处理"],
    [/账号|密码/g, "敏感信息"],
    [forbiddenFieldPattern, "业务数据"]
  ];
  return replacements
    .reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), formatTimestampsInText(value))
    .replace(/只有\s*业务数据\s*才能进入主策略候选；\s*业务数据\s*只能作为确认信号。/g, "仅数据边界检查通过的数据可进入主判断，其余信息只作为辅助观察信号。")
    .trim();
}

function reportPreviewText(content?: string | null, contentType?: string | null) {
  if (!content?.trim()) return "报告正文已读取，客户页面当前只展示业务摘要。";
  if (contentType === "json") return "结构化报告原文已读取。客户页面保留业务摘要，完整原文可通过下载查看。";
  const lines = content
    .replace(/```[\s\S]*?```/g, "")
    .split(/\r?\n/)
    .map((line) => cleanBusinessText(line, "").replace(/^[#>*\-\s\d.、]+/, "").trim())
    .filter(Boolean)
    .filter((line) => !/[A-Za-z]+(?:_[A-Za-z0-9]+)+/.test(line))
    .filter((line) => !/\b[a-z]+[A-Z][A-Za-z0-9]*\b/.test(line))
    .filter((line) => !/[{}[\]"]/.test(line))
    .slice(0, 80);
  const text = lines.join("\n");
  return text || "报告原文已读取。客户页面保留业务摘要，完整原文可通过下载查看。";
}

function statusFromText(value?: string): AgentRunStatus {
  const normalized = (value ?? "").toLowerCase();
  if (["success", "completed", "ready", "usable", "promote", "actionable"].includes(normalized)) return "success";
  if (["failed", "error"].includes(normalized)) return "failed";
  if (["blocked", "disabled", "insufficient", "do_not_use_for_actions"].includes(normalized)) return "blocked";
  if (["needs_human_review", "manual_review", "watch_only", "needs_export"].includes(normalized)) return "needs_human_review";
  return "running";
}

function sourceCategoryLabel(value?: string | null) {
  const normalized = (value ?? "").toLowerCase();
  if (/(polyester|poy|dty|pta|px|meg|chemical|price|industry|portal|authorized|ccf)/.test(normalized)) return "行业价格数据";
  if (/(eia|brent|wti|crude|oil|naphtha|energy|inventory)/.test(normalized)) return "上游原料数据";
  if (/(fred|macro|rate|currency|cftc|usd|cny|fx)/.test(normalized)) return "宏观与能源数据";
  if (/(gdelt|rss|news|announcement|official|event|policy|公告)/.test(normalized)) return "新闻与公告数据";
  if (/(user|excel|csv|file|manual|upload|用户)/.test(normalized)) return "用户补充文件";
  return "业务证据来源";
}

function eventCategory(value?: EventImpact) {
  const text = `${value?.event_type ?? ""} ${(value?.affected_products ?? []).join(" ")}`.toLowerCase();
  if (/sanction|geopolitic|iran|ofac|blockade|military|centcom/.test(text)) return "制裁";
  if (/shipping|freight|port|strait|hormuz|marine|vessel/.test(text)) return "航运";
  if (/crude|oil|opec|iea|eia|brent|wti/.test(text)) return "原油";
  if (/plant|maintenance|device|unit|pta|px|meg|polyester/.test(text)) return "装置";
  if (/demand|supply|inventory|stock|poy|dty/.test(text)) return "供需";
  return "宏观";
}

function eventTone(value?: EventImpact): Tone {
  const nature = value?.nature ?? "";
  if (nature.includes("利多")) return "warning";
  if (nature.includes("利空")) return "info";
  if ((value?.confidence ?? 0) >= 0.8) return "success";
  return "muted";
}

function normalizedHeatScore(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return 0;
  return value > 1 ? value / 100 : value;
}

function eventLibraryTone(event: EventLibraryItem): Tone {
  const strength = cleanBusinessText(event.impact_strength);
  const heat = normalizedHeatScore(event.heat_score);
  const evidenceLevel = event.evidence_level;
  const directional = event.direction === "利多" || event.direction === "利空";
  const featured = event.status_label === "重点跟踪";
  const strongEvidence = evidenceLevel === "A" || evidenceLevel === "B";

  if (
    strength === "高影响" && strongEvidence
  ) {
    return "danger";
  }
  if (
    strength === "中高影响"
    || strength === "中等影响"
    || heat >= 0.45
    || directional
    || featured
  ) {
    return "warning";
  }
  if (strength === "低影响" || heat > 0) return "success";
  return "muted";
}

function eventAnalysisSummary(event: EventLibraryItem, affected: string[]) {
  const politicalSummary = cleanBusinessText(event.political_intelligence?.summary, "");
  const factualSummary = cleanBusinessText(event.factual_summary || event.summary, "");
  if (factualSummary) return factualSummary;
  if (politicalSummary && !politicalSummary.includes("从权力、利益")) return politicalSummary;
  const direction = cleanBusinessText(event.direction, "待研判");
  const products = affected.length ? affected.join("、") : "具体品种";
  return `系统已关联该事件与${products}，初步方向为${direction}；仍需结合价格、库存、开工率或利润的同向变化完成核验。`;
}

function qualifiedChineseEventSummary(value?: string | null) {
  const summary = String(value ?? "").trim();
  if (!summary) return undefined;
  const hanCharacters = summary.match(/[\u3400-\u9fff]/g)?.length ?? 0;
  const latinWords = summary.match(/[A-Za-z]{3,}/g)?.length ?? 0;
  if (hanCharacters < 12 || latinWords > Math.max(4, Math.floor(hanCharacters / 6))) return undefined;
  if (/(?:traceback|upstream error|api key|<html|https?:\/\/)/i.test(summary)) return undefined;
  return compactText(cleanBusinessText(summary), 220);
}

function eventAnalysisGateReason(event?: BusinessEvent) {
  if (!event) return "尚未选择事件。";
  const sourceReason = event.sourceContentStatusLabel;
  if (sourceReason && /原站|原文链接|来源文件/.test(sourceReason)
    && (event.sourceContentStatus === "title_only" || event.sourceContentStatus === "partial_text")) {
    return `${sourceReason}；取得可核验正文后才能生成事实摘要。当前不生成影响研判。`;
  }
  if (event.sourceContentStatus === "title_only") return "仅取得可核验标题，尚无正文；不生成事实摘要、方向或风险等级。";
  if (event.sourceContentStatus === "partial_text") return "来源正文不完整；不把截断摘录作为事实摘要，也不生成影响研判。";
  if (event.summaryGenerationStatus !== "ready") {
    return `${event.summaryStatusLabel || "中文摘要尚未生成"}；在摘要完成前不生成方向、风险等级或影响品种。`;
  }
  if (!event.factSummaryReady) return "摘要未通过中文质量门禁；不展示来源正文摘录，也不生成影响研判。";
  if (event.analysisAvailable === false) return "事实摘要可核验，但影响分析尚未通过研判门禁。";
  return "事实摘要与影响分析均已通过当前展示门禁。";
}

function productKey(value: string) {
  const normalized = value.toUpperCase();
  if (normalized.includes("石脑")) return "NAPHTHA";
  if (normalized.includes("原油")) return "BRENT";
  return normalized;
}

function displayProduct(value?: string | null) {
  const normalized = (value ?? "").toUpperCase();
  if (normalized === "NAPHTHA") return "石脑油";
  if (normalized === "BRENT" || normalized === "WTI" || normalized === "CRUDE") return "原油";
  if (normalized === "CRUDE_OIL") return "原油";
  if (normalized === "UPSTREAM_COST_PRESSURE") return "上游成本压力";
  return cleanBusinessText(value, "业务品种");
}

function latestPriceFor(live: WorkbenchLiveData | undefined, product: string) {
  if (!live?.latestPrices.ok) return undefined;
  return newestPriceForProduct(product, live.latestPrices.value.items);
}

function newestPriceForProduct(product: string, items: LatestPriceItem[]) {
  const key = productKey(product);
  const aliases = key === "BRENT" ? new Set(["BRENT", "WTI", "CRUDE", "CRUDE_OIL"]) : new Set([key]);
  const candidates = items
    .filter((item) => {
      const tokens = `${item.instrument} ${item.label}`.toUpperCase().split(/[^A-Z0-9_]+/).filter(Boolean);
      return tokens.some((token) => aliases.has(token))
        && Boolean(item.latest?.observed_at)
        && Number.isFinite(item.latest?.last)
        && (item.latest?.last ?? 0) > 0;
    })
    .sort((left, right) => Date.parse(right.latest!.observed_at) - Date.parse(left.latest!.observed_at));
  if (key === "BRENT") {
    return candidates.find((item) => item.instrument.toUpperCase() === "BRENT") ?? candidates[0];
  }
  return candidates[0];
}

function preferredLatestPriceForProduct(
  product: string,
  items: LatestPriceItem[],
  canonicalUnit?: string
) {
  const key = productKey(product);
  const aliases = key === "BRENT" ? new Set(["BRENT", "WTI", "CRUDE", "CRUDE_OIL"]) : new Set([key]);
  const candidates = items
    .filter((item) => {
      const tokens = `${item.instrument} ${item.label}`.toUpperCase().split(/[^A-Z0-9_]+/).filter(Boolean);
      return tokens.some((token) => aliases.has(token))
        && Boolean(item.latest?.observed_at)
        && Number.isFinite(item.latest?.last)
        && (item.latest?.last ?? 0) > 0;
    })
    .sort((left, right) => {
      const score = (item: LatestPriceItem) => {
        const exactInstrument = item.instrument.toUpperCase() === key ? 8 : 0;
        const compatibleUnit = canonicalUnit && item.latest?.unit === canonicalUnit ? 4 : 0;
        const formalQuote = item.latest?.price_type === "daily_reference"
          || item.latest?.price_type === "spot_public_valuation" ? 2 : 0;
        return exactInstrument + compatibleUnit + formalQuote;
      };
      return Date.parse(right.latest!.observed_at) - Date.parse(left.latest!.observed_at)
        || score(right) - score(left);
    });
  if (key === "BRENT") {
    return candidates.find((item) => item.instrument.toUpperCase() === "BRENT") ?? candidates[0];
  }
  return candidates[0];
}

function compatibleLatestPriceForProduct(
  product: string,
  items: LatestPriceItem[],
  view?: MarketChainProductView
) {
  const candidate = newestPriceForProduct(product, items);
  const canonicalUnit = view?.price_series.find((point) => point.trend_eligible !== false)?.unit
    ?? view?.latest_price.unit;
  if (!candidate?.latest || !canonicalUnit) return candidate;
  return candidate.latest.unit === canonicalUnit ? candidate : undefined;
}

function marketFreshnessSummary(view?: MarketChainProductView, displayedPriceStale?: boolean) {
  return formatMarketFreshnessSummary(view?.data_freshness, displayedPriceStale);
}

function factorFor(live: WorkbenchLiveData | undefined, keyword: string) {
  if (!live?.factors.ok) return undefined;
  const normalized = keyword.toUpperCase();
  return live.factors.value.find((factor) => `${factor.name} ${factor.symbol} ${factor.route}`.toUpperCase().includes(normalized));
}

function latestPriceLabel(item?: LatestPriceItem) {
  if (!item?.latest) return "未返回";
  return formatPrice(item.latest.last, item.latest.unit);
}

function latestPriceDate(item?: LatestPriceItem) {
  const observed = item?.latest?.observed_at;
  if (!observed) return "暂无时间";
  return /^\d{4}-\d{2}-\d{2}$/.test(observed) ? observed : formatDate(observed);
}

function calendarWindow<T extends { date: string }>(points: T[], window: "30" | "90" | "all") {
  const sorted = [...points].sort((left, right) => left.date.localeCompare(right.date));
  if (window === "all" || !sorted.length) return sorted;
  const latest = Date.parse(sorted[sorted.length - 1].date);
  if (!Number.isFinite(latest)) return sorted;
  const cutoff = latest - Number(window) * 24 * 60 * 60 * 1000;
  return sorted.filter((point) => Date.parse(point.date) >= cutoff);
}

function realTrendPoints(product: string, live?: WorkbenchLiveData, window: "30" | "90" | "all" = "all") {
  if (product !== "原油" || !live?.priceComparison.ok) return [];
  const summary = live.priceComparison.value.summaries.find((item) => /brent/i.test(`${item.series_id} ${item.label}`))
    ?? live.priceComparison.value.summaries[0];
  const unit = summary?.points.find((point) => /barrel|bbl/i.test(point.unit))?.unit;
  if (!summary || !unit) return [];
  const byDate = new Map<string, number>();
  summary.points
    .filter((point) => point.unit === unit && !/cftc|contract|open interest/i.test(`${point.source_id} ${point.indicator}`))
    .forEach((point) => {
      byDate.set(point.observed_at.slice(0, 10), point.value);
    });
  return calendarWindow(Array.from(byDate.entries()).map(([date, value]) => ({ date, value })), window);
}

function marketProductView(product: string, live?: WorkbenchLiveData): MarketChainProductView | undefined {
  if (!live?.marketChain.ok) return undefined;
  return live.marketChain.value.products.find((item) => item.label === product || item.key === productKey(product));
}

function marketTrendPoints(product: string, live?: WorkbenchLiveData, window: "30" | "90" | "all" = "all") {
  const view = marketProductView(product, live);
  return marketTrendPointsFromView(product, view, live, window);
}

function marketTrendPointsFromView(
  product: string,
  view?: MarketChainProductView,
  live?: WorkbenchLiveData,
  window: "30" | "90" | "all" = "all"
) {
  if (view?.price_series.length) {
    const latestEligible = view.price_series.filter((point) => point.trend_eligible !== false)
      .sort((a, b) => b.date.localeCompare(a.date))[0];
    const canonicalUnit = latestEligible?.unit || view.latest_price.unit;
    const eligible = view.price_series.filter((point) => (
      point.trend_eligible !== false
      && (!point.unit || point.unit === canonicalUnit)
    ));
    const basisGroups = new Map<string, typeof eligible>();
    eligible.forEach((point) => {
      const basis = point.comparison_basis;
      const key = basis
        ? [basis.product, basis.series_id, basis.market, basis.spec, basis.quote_type, basis.source_basis, basis.unit]
          .map((value) => String(value ?? "").trim().toUpperCase()).join("|")
        : "__legacy__";
      basisGroups.set(key, [...(basisGroups.get(key) ?? []), point]);
    });
    const canonicalRows = Array.from(basisGroups.values()).sort((left, right) => (
      String(right[right.length - 1]?.date ?? "").localeCompare(String(left[left.length - 1]?.date ?? ""))
      || right.length - left.length
    ))[0] ?? [];
    const byDate = new Map<string, (typeof canonicalRows)[number]>();
    canonicalRows.forEach((point) => {
      const date = point.date.slice(0, 10);
      const previous = byDate.get(date);
      if (!previous || (point.sample_count ?? 0) >= (previous.sample_count ?? 0)) byDate.set(date, point);
    });
    return calendarWindow(Array.from(byDate.values()).map((point) => ({
      date: point.date.slice(0, 10),
      value: point.value,
      unit: point.unit,
      comparisonBasis: point.comparison_basis,
      originalValue: point.original_value,
      originalUnit: point.original_unit,
      fxRate: point.fx_rate,
      fxDate: point.fx_date,
      conversionStatus: point.conversion_status
    })), window);
  }
  return realTrendPoints(product, live, window);
}

function trendPointsFromPriceDetails(rows: ForecastPricePoint[], window: "30" | "90" | "all" = "all") {
  const groups = new Map<string, ForecastPricePoint[]>();
  rows.forEach((row) => {
    if (!row.observed_at || !Number.isFinite(row.price) || row.price <= 0 || !row.unit) return;
    const key = [row.unit, row.series, row.spec, row.quote_type].map((item) => String(item ?? "").trim().toUpperCase()).join("|");
    groups.set(key, [...(groups.get(key) ?? []), row]);
  });
  const compatibleRows = Array.from(groups.values()).sort((left, right) => {
    const latest = (items: ForecastPricePoint[]) => Math.max(...items.map((item) => Date.parse(item.observed_at)));
    return latest(right) - latest(left) || right.length - left.length;
  })[0] ?? [];
  const byDate = new Map<string, { total: number; count: number }>();
  compatibleRows.forEach((row) => {
    if (!row.observed_at || !Number.isFinite(row.price) || row.price <= 0) return;
    const date = row.observed_at.slice(0, 10);
    const current = byDate.get(date) ?? { total: 0, count: 0 };
    current.total += row.price;
    current.count += 1;
    byDate.set(date, current);
  });
  const points = Array.from(byDate.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([date, item]) => ({
      date,
      value: Number((item.total / Math.max(item.count, 1)).toFixed(2)),
      unit: compatibleRows[0]?.unit
    }));
  return calendarWindow(points, window);
}

function marketSummaryTone(value?: string): Tone {
  if (value === "success" || value === "warning" || value === "info" || value === "danger" || value === "muted") {
    return value;
  }
  return "muted";
}

function marketSummaryTag(summary?: MarketChainProductView["spread_summary"]) {
  if (!summary) return "未返回";
  if (summary.status === "missing") return "未入库";
  return cleanBusinessText(summary.tag || summary.quality_label || "已更新");
}

function marketSummaryBody(summary?: MarketChainProductView["spread_summary"]) {
  if (!summary) return "系统未返回该项业务指标。";
  return cleanBusinessText(summary.detail);
}

function isLatestPriceStale(item?: LatestPriceItem) {
  if (!item?.latest?.observed_at) return true;
  if (["stale", "missing", "delayed"].includes(item.freshness)) return true;
  if (["realtime", "near_realtime"].includes(item.freshness)) return false;
  // The market-chain page is a daily decision view, not a trading terminal.
  // Use the observation's business date consistently across proxy, public
  // valuation and daily-reference prices. T and T-1 are current.
  const now = new Date();
  const [year, month, day] = item.latest.observed_at.slice(0, 10).split("-").map(Number);
  const observedDate = new Date(year, month - 1, day);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const ageDays = Math.floor((today.getTime() - observedDate.getTime()) / (24 * 60 * 60 * 1000));
  return !Number.isFinite(ageDays) || ageDays > 1;
}

function historicalMarketSummaryBody(
  title: string,
  asOf?: string,
  summary?: MarketChainProductView["spread_summary"]
) {
  const metric = cleanBusinessText(summary?.metric_label || title);
  const date = formatDisplayDate(asOf);
  return `${metric}截至 ${date}，仅作历史参考，不参与当前判断。`;
}

const customerHiddenEvidencePattern = /AGENTS|README|runbook|architecture|deployment|openapi|docs\/|project_document|source_config|Computer Use|Safari|authorized|license|login|password|token|provider|prompt|账号|密码|授权|采集方式/i;

function isCustomerVisibleRagEvidence(document: RagEvidence) {
  const haystack = [
    document.doc_id,
    document.doc_type,
    document.source_id,
    document.title,
    document.summary,
    document.snippet,
    document.url,
    JSON.stringify(document.metadata ?? {})
  ].join(" ");
  return !customerHiddenEvidencePattern.test(haystack);
}

function evidenceFromRetrieval(live?: WorkbenchLiveData): AssistantEvidence[] {
  if (!live?.retrieval.ok || !Array.isArray(live.retrieval.value.documents)) return [];
  return live.retrieval.value.documents.filter(isCustomerVisibleRagEvidence).slice(0, 6).map((document, index) => ({
    category: sourceCategoryLabel(document.source_id || document.doc_type),
    id: document.doc_id,
    title: document.title || `证据 ${index + 1}`,
    reason: document.summary || document.snippet || "被当前问题检索命中。",
    observed: document.observed_at,
    url: /^https?:\/\//i.test(document.url ?? "") ? document.url : undefined
  }));
}

function horizonText(value?: string | null) {
  if (value === "h1") return "1 日";
  if (value === "h3") return "3 日";
  if (value === "h7") return "7 日";
  if (value === "h14") return "14 日";
  return cleanBusinessText(value, "当前周期");
}

function hasUsableEvidence(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const bundleCount = data?.evidence_bundle.documents ?? 0;
  const retrievedCount = live?.retrieval.ok && Array.isArray(live.retrieval.value.documents) ? live.retrieval.value.documents.filter(isCustomerVisibleRagEvidence).length : 0;
  return bundleCount > 0 || retrievedCount > 0;
}

function formalConclusionSnapshot(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const overview = live?.overview.ok ? live.overview.value : undefined;
  const fullChain = live?.fullChain.ok ? live.fullChain.value : undefined;
  const ragVisual = live?.ragVisual.ok ? live.ragVisual.value : undefined;
  const marketChain = live?.marketChain.ok ? live.marketChain.value : undefined;
  const snapshotIds = [overview?.data_snapshot_id, fullChain?.data_snapshot_id, ragVisual?.data_snapshot_id, marketChain?.data_snapshot_id];
  const asOfTimes = [overview?.as_of_time, fullChain?.as_of_time, ragVisual?.as_of_time, marketChain?.as_of_time];
  const sameSnapshot = snapshotIds.every(Boolean) && new Set(snapshotIds).size === 1;
  const sameAsOfTime = asOfTimes.every(Boolean) && new Set(asOfTimes).size === 1;
  const fullChainGate = fullChain?.status === "ready"
    && (fullChain.poy_dty_gate as { qualified?: boolean } | undefined)?.qualified === true;
  const evidenceGate = ragVisual?.formal_conclusion_gate?.qualified === true
    && (ragVisual.formal_conclusion_gate.adopted_evidence_ids?.length ?? 0) > 0
    && (ragVisual.conclusion_confidence ?? 0) > 0;
  const qualified = Boolean(
    data
    && data.source_mode === "live"
    && data.operational_status !== "blocked"
    && overview
    && fullChainGate
    && evidenceGate
    && sameSnapshot
    && sameAsOfTime
  );
  if (!qualified) return undefined;
  return {
    dataSnapshotId: snapshotIds[0]!,
    asOfTime: asOfTimes[0]!
  };
}

function hasFormalConclusion(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (data?.prediction_authority === "seven_product_ledger") return false;
  return Boolean(formalConclusionSnapshot(data, live));
}

function mappedFormalEvidence(live: WorkbenchLiveData | undefined, mappingKey: string) {
  const ragVisual = live?.ragVisual.ok ? live.ragVisual.value : undefined;
  const ids = new Set(ragVisual?.formal_conclusion_gate?.evidence_mapping?.[mappingKey] ?? []);
  return (ragVisual?.evidence_buckets.adopted ?? []).filter((item) => ids.has(item.id));
}

function primaryConclusion(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (!hasFormalConclusion(data, live)) {
    const prediction = data?.low_confidence_prediction;
    if (prediction?.source === "main_forecast") return prediction.rationale;
    if (prediction) {
      return `当前成本压力判断为${prediction.direction}，置信度 ${formatPercent(prediction.confidence)}；该判断为非正式预测，不进入正式报告。`;
    }
    return "当前未取得可用的方向信号，暂无法形成判断。";
  }
  const overview = live?.overview.ok ? live.overview.value : undefined;
  const overviewPrefix = overview ? `成本压力${cleanBusinessText(overview.status)}，指数 ${formatInteger(overview.cost_pressure_index)}。` : "";
  return `${overviewPrefix}本轮结论仅依据同一数据快照内已采用并完成复核的正式证据。`;
}

function bossDirectionLabel(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (!hasFormalConclusion(data, live)) return data?.low_confidence_prediction?.direction ?? "暂无方向信号";
  const overview = live?.overview.ok ? live.overview.value : undefined;
  const status = cleanBusinessText(overview?.status, "").replace(/\s+/g, "");
  const trend = `${cleanBusinessText(overview?.trend_1d, "")} ${cleanBusinessText(overview?.trend_7d, "")}`;
  if (/偏强|上行|上涨|利多|增强|走强/.test(`${status} ${trend}`)) return "偏强";
  if (/偏弱|下行|下跌|利空|减弱|走弱/.test(`${status} ${trend}`)) return "偏弱";
  if (/震荡|分化|中性/.test(`${status} ${trend}`)) return "震荡";
  return "观察";
}

function observationSignalLabel(direction: string, data?: AgentWorkbenchData) {
  const hasBlocker = data?.operational_status === "blocked";
  if (hasBlocker) return "数据边界待复核，当前仅保留观察级判断";
  if (direction === "偏强") return "观察原油、PX/PTA 是否继续同向走强";
  if (direction === "偏弱") return "观察原油止跌与聚酯端去库信号";
  if (direction === "震荡") return "观察原料链各环节是否形成同向确认";
  return "等待价格链、事件链与供需验证信号";
}

function bossSupportEvidence(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (!hasFormalConclusion(data, live)) {
    const prediction = data?.low_confidence_prediction;
    return prediction
      ? [cleanBusinessText(prediction.rationale, "当前方向由已返回的价格趋势与事件信号形成，缺口已计入置信度。")]
      : ["当前没有可用于形成方向的有效输入。"];
  }
  const result = mappedFormalEvidence(live, "supporting_evidence").map((item) => `${cleanBusinessText(item.title)}：${compactText(item.summary, 64)}`);
  return result.length ? result.slice(0, 3) : ["正式支持证据尚未返回，本轮不生成判断依据。"];
}

function bossCounterEvidence(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (!hasFormalConclusion(data, live)) {
    const prediction = data?.low_confidence_prediction;
    return prediction
      ? (prediction.counter_evidence.length ? prediction.counter_evidence.map((item) => cleanBusinessText(item)) : [cleanBusinessText(prediction.reversal_condition)])
      : ["当前没有有效方向输入，反证随下一批真实观测一并更新。"];
  }
  const result = mappedFormalEvidence(live, "counter_evidence").map((item) => `${cleanBusinessText(item.title)}：${compactText(item.summary, 64)}`);
  return result.length ? result.slice(0, 3) : ["本轮正式门禁未映射反证，页面不生成反证内容。"];
}

function bossReversalSignal(direction: string, live?: WorkbenchLiveData) {
  if (direction === "暂无方向信号") return "等待价格、事件与证据链形成可追溯的同向确认。";
  const fullChain = fullChainSummaryRows(live);
  const weakLayer = fullChain.find((item) => /分化|冲突|待|弱|下行|反向/.test(`${item.state}${item.detail}`));
  if (weakLayer) return `${weakLayer.label}出现反向确认，会推翻当前判断。`;
  if (direction === "偏强") return "若原油回落且 PX/PTA 不再跟随，当前偏强判断需要降级。";
  if (direction === "偏弱") return "若原油止跌且聚酯库存继续下降，当前偏弱判断需要复核。";
  return "若原油、PX/PTA 和聚酯端同向放量，当前观察判断需要升级。";
}

function bossRiskSentence(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  if (!hasFormalConclusion(data, live) && data?.low_confidence_prediction) {
    return cleanBusinessText(data.low_confidence_prediction.key_risks[0], "部分链路数据尚不完整，已降低置信度。");
  }
  const risk = mappedFormalEvidence(live, "risks")[0];
  return risk ? `${cleanBusinessText(risk.title)}：${compactText(risk.summary, 64)}` : "本轮正式门禁未映射风险，页面不生成风险结论。";
}

function bossWaitingSignal(direction: string, live?: WorkbenchLiveData) {
  if (direction === "偏强") return "等待 PX/PTA 跟涨、聚酯库存下降或下游承接改善。";
  if (direction === "偏弱") return "等待原油止跌、库存消化或下游补货恢复。";
  return bossReversalSignal(direction, live);
}

function bossConclusionView(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const direction = bossDirectionLabel(data, live);
  const prediction = !hasFormalConclusion(data, live) ? data?.low_confidence_prediction : undefined;
  return {
    direction,
    action: observationSignalLabel(direction, data),
    risk: bossRiskSentence(data, live),
    waiting: prediction?.verification_signals.length
      ? cleanBusinessText(prediction.verification_signals[0])
      : bossWaitingSignal(direction, live),
    why: bossSupportEvidence(data, live),
    counter: bossCounterEvidence(data, live),
    reversal: prediction
      ? cleanBusinessText(prediction.invalidation_conditions[0] || prediction.reversal_condition)
      : bossReversalSignal(direction, live)
  };
}


function shortBossDailyReport(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const view = bossConclusionView(data, live);
  const support = view.why.join("；");
  const counter = view.counter.join("；");
  const events = buildBusinessEvents(data, live).slice(0, 2).map((event) => {
    const title = compactText(event.title, 36);
    return title.startsWith(`${event.category}：`) ? title : `${event.category}：${title}`;
  });
  return [
    `今日方向：${view.direction}。${view.action}。`,
    `主要依据：${support}。`,
    `反过来看：${counter}。`,
    `需要盯住：${view.waiting}`,
    events.length ? `事件关注：${events.join("；")}。` : "事件关注：暂无新增高优先级事件。"
  ].join("\n");
}

function freshnessView(live?: WorkbenchLiveData): { label: string; detail: string; tone: Tone } {
  const products = ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"];
  if (!live?.marketChain.ok) return { label: "待核验", detail: "正在读取各品种价格时效", tone: "info" };
  const states = products.map(label => {
    const view = marketProductView(label, live);
    return { label, status: view?.latest_display_freshness?.status ?? view?.data_freshness.categories?.price?.status ?? "missing" };
  });
  const stale = states.filter(x => x.status === "stale");
  const missing = states.filter(x => x.status === "missing");
  const pending = states.filter(x => x.status === "available");
  return { label: stale.length ? "部分价格滞后" : missing.length || pending.length ? "部分待核验" : "价格时效正常",
    detail: [...(stale.length ? [`滞后：${stale.map(x => x.label).join("、")}`] : []),
      ...(missing.length ? [`未返回：${missing.map(x => x.label).join("、")}`] : []),
      ...(pending.length ? [`时效待核验：${pending.map(x => x.label).join("、")}`] : [])].join("；") || "七品种按来源发布节奏核验 · 运行状态独立显示",
    tone: stale.length || missing.length || pending.length ? "warning" : "success" };
}
function freshnessLabel(_data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  return freshnessView(live).label;
}

function updatedAt(data?: AgentWorkbenchData) {
  return data?.data_latest_at || data?.generated_at;
}

function operationalTone(data?: AgentWorkbenchData): Tone {
  if (!data) return "warning";
  if (data.operational_status === "blocked") return "danger";
  if (data.operational_status === "ready_with_warnings") return "warning";
  if (data.operational_status === "ready") return "success";
  return data.source_mode === "live" ? "success" : "warning";
}

function evidenceTrust(data?: AgentWorkbenchData) {
  const level = data?.evidence_bundle.evidence_level ?? "D";
  if (level === "A") return "高";
  if (level === "B") return "较高";
  if (level === "C") return "需关注";
  return "证据偏少";
}

type BusinessRiskItem = {
  title: string;
  detail: string;
  impact: string;
  action: string;
  level: "高" | "中" | "低";
  tone: Tone;
};

function buildRiskItems(data?: AgentWorkbenchData, live?: WorkbenchLiveData): BusinessRiskItem[] {
  const risks: BusinessRiskItem[] = [];
  const overview = live?.overview.ok ? live.overview.value : undefined;
  const pressure = overview?.cost_pressure_index;
  const keyDrivers = overview?.key_drivers.map((item) => cleanBusinessText(item)).slice(0, 3).join("、") || "原油与芳烃链";
  if (pressure !== undefined) {
    if (pressure >= 70) {
      risks.push({
        title: "上游成本上行压力",
        detail: `成本压力指数 ${formatInteger(pressure)}，主要驱动：${keyDrivers}`,
        impact: "POY/DTY 上游原料成本压力可能继续增强，成本传导空间需要下游数据验证。",
        action: "重点跟踪原油、PX、PTA 是否继续同向上涨，并核对聚酯库存、开工与利润。",
        level: "高",
        tone: "warning"
      });
    } else if (pressure <= 35) {
      risks.push({
        title: "成本支撑转弱",
        detail: `成本压力指数 ${formatInteger(pressure)}，成本端暂未形成强支撑。`,
        impact: "若下游需求没有同步改善，上游成本支撑可能难以向聚酯端完整传导。",
        action: "关注聚酯端需求承接，确认成本压力是否继续减弱。",
        level: "中",
        tone: "info"
      });
    } else {
      risks.push({
        title: "成本传导分化",
        detail: `成本压力指数 ${formatInteger(pressure)}，处于观察区间。`,
        impact: "原油、PX/PTA 与聚酯端可能不同步，单一价格变化不足以支撑强方向判断。",
        action: "以原油、PX/PTA、MEG 和 POY/DTY 价差是否同向为主要确认条件。",
        level: "中",
        tone: "info"
      });
    }
  }

  const priceRisks = latestPriceRisks(live);
  risks.push(...priceRisks);

  return risks
    .filter((risk, index, list) => list.findIndex((item) => item.title === risk.title) === index)
    .slice(0, 6);
}

function latestPriceRisks(live?: WorkbenchLiveData): BusinessRiskItem[] {
  const latest = live?.latestPrices.ok ? live.latestPrices.value.items : [];
  if (!latest.length) return [];
  const changeOf = (pattern: RegExp) => latest.find((item) => pattern.test(`${item.instrument} ${item.label}`))?.latest?.change_pct;
  const crude = changeOf(/brent|wti|sc|原油/i);
  const ptaPx = [changeOf(/px/i), changeOf(/pta/i)].filter((item): item is number => typeof item === "number");
  const meg = changeOf(/meg/i);

  const risks: BusinessRiskItem[] = [];
  if (typeof crude === "number" && Math.abs(crude) >= 1.5) {
    risks.push({
      title: crude > 0 ? "原油价格明显上涨" : "原油价格明显下跌",
      detail: `原油相关报价最新变化约 ${crude.toFixed(1)}%。`,
      impact: crude > 0 ? "石脑油和 PX 估值可能跟随上移，POY/DTY 成本端存在上行预期。" : "石脑油和 PX 成本支撑可能转弱，需确认是否传导至 POY/DTY。",
      action: "跟踪油价变化是否持续，并确认 PX/PTA 是否同步变化。",
      level: Math.abs(crude) >= 3 ? "高" : "中",
      tone: Math.abs(crude) >= 3 ? "warning" : "info"
    });
  }
  const maxAromatics = Math.max(...ptaPx, Number.NEGATIVE_INFINITY);
  if (Number.isFinite(maxAromatics) && maxAromatics >= 1.2) {
    risks.push({
      title: "PX/PTA 成本支撑增强",
      detail: `PX/PTA 最新涨幅最高约 ${maxAromatics.toFixed(1)}%。`,
      impact: "聚酯主原料成本上移，POY/DTY 上游成本压力可能增强。",
      action: "优先观察 PTA 加工费、聚酯利润和长丝成交是否同步改善。",
      level: maxAromatics >= 2.5 ? "高" : "中",
      tone: "warning"
    });
  }
  if (typeof meg === "number" && Math.abs(meg) >= 1.5) {
    risks.push({
      title: "MEG 波动放大",
      detail: `MEG 最新变化约 ${meg.toFixed(1)}%。`,
      impact: "辅原料波动会影响聚酯综合成本，但传导强度通常弱于 PTA/PX。",
      action: "将 MEG 与 PTA 同向性合并观察，避免单一 MEG 波动误导成本判断。",
      level: "低",
      tone: "info"
    });
  }
  return risks;
}

function overviewStatusLabel(tone: Tone) {
  if (tone === "success") return "可作为辅助";
  if (tone === "danger") return "暂停行动";
  if (tone === "info") return "观察";
  return "需关注";
}

function evidenceSummaryRows(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const retrievalCount = live?.retrieval.ok ? live.retrieval.value.documents.length : 0;
  const marketCount = live?.marketChain.ok ? live.marketChain.value.products.filter((item) => item.latest_price.status === "available").length : 0;
  const eventCount = live?.eventLibrary.ok ? live.eventLibrary.value.total_events : buildBusinessEvents(data, live).length;
  const graphCount = live?.graph.ok ? live.graph.value.nodes.length : 0;
  const userCount = data?.evidence_bundle.documents ?? 0;
  return [
    { label: "业务证据来源", detail: "价格、事件与来源边界证据", count: userCount },
    { label: "行情价格数据", detail: "原油到 POY/DTY 链路价格", count: marketCount },
    { label: "上游原料数据", detail: "原料链节点与传导关系", count: marketCount },
    { label: "宏观与能源数据", detail: "原油、美元、利率等观察项", count: graphCount },
    { label: "新闻与公告数据", detail: "事件库与影响线索", count: eventCount },
    { label: "检索证据资料", detail: "当前问题命中的证据", count: retrievalCount }
  ];
}

function fullChainQuoteLabel(item: FullChainSummaryItem) {
  // 报价口径必须与来源一致：郑商所=主力合约结算、生意社=公开现货评估、
  // TNC=公开近期均价；仅 CCF 授权日均保留旧标注，非正式资格不得冒用授权口径。
  switch (String(item.quote_type ?? "")) {
    case "main_continuous_settlement":
      return "郑商所主力结算";
    case "public_spot_assessment":
      return "生意社公开现货评估";
    case "public_recent_average":
      return "TNC公开近期均价";
    case "daily_average":
      return "授权日均现货";
    default:
      return item.formal_eligible === false ? "不具备正式资格" : "公开价格观测";
  }
}

const fullChainSourceNames: Record<string, string> = {
  czce_pta_px: "郑商所",
  sunsirs_public_commodity_assessment: "生意社",
  tnc_polyester_history: "TNC全球纺织网",
  eia_petroleum_api: "EIA",
  fred_macro_api: "FRED"
};

function fullChainSummaryRows(live?: WorkbenchLiveData) {
  if (!live?.fullChain.ok) return [];
  return live.fullChain.value.summary.filter((item) => {
    const unit = String(item.unit ?? "").toLowerCase();
    const metric = String(item.metric ?? "").toLowerCase();
    // A frozen snapshot may contain CFTC positioning observations for CRUDE.
    // They are useful evidence, but they are not prices and must never occupy a
    // customer-facing price slot or inherit a price label.
    return !/(contracts?|lots?|positions?|open[_\s-]?interest|张|手)/i.test(unit)
      && !/(cftc|cot\b|open[_\s-]?interest|position|持仓)/i.test(metric);
  }).map((item, index) => ({
    id: `${item.product ?? item.label ?? "product"}-${item.observed_at ?? "unknown"}-${index}`,
    label: displayProduct(item.product ?? item.label),
    state: `${item.observed_at && formatDate(item.observed_at).slice(0, 10) < formatDate(new Date().toISOString()).slice(0, 10) ? "历史观测 · " : ""}${cleanBusinessText(item.state, item.formal_eligible === false ? "不具备正式资格" : item.evidence_tier ? `${item.evidence_tier}级观测` : "已返回")}`,
    value: typeof item.value === "number" ? formatPrice(item.value, item.unit) : "暂无数据",
    detail: cleanBusinessText(item.detail, [
      item.observed_at ? `观测日期 ${formatDate(item.observed_at)}` : "",
      item.product === "CRUDE"
        ? "官方现货观测"
        : `${fullChainSourceNames[String(item.source_id ?? "")] ?? "当前验收源"} · ${fullChainQuoteLabel(item)}`
    ].filter(Boolean).join(" · ") || "当前观测已返回")
  }));
}

function sourceAutomationView(source?: DeliverySourceAutomation) {
  const tasks = Array.isArray(source?.tasks) ? source.tasks : [];
  const summary = source?.summary ?? {};
  const success = Number(summary.success ?? 0);
  const ready = Number(summary.ready ?? 0);
  const blocked = Number(summary.blocked ?? 0);
  const needsAction = Number(source?.manual_or_blocked ?? 0);
  const automationReady = Number(source?.automation_ready ?? 0);
  const total = tasks.length || success + ready + blocked;
  const publicTasks = tasks.filter((task) => ["eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"].includes(String(task.source_id ?? "")));
  const newsTasks = tasks.filter((task) => String(task.dataset_type ?? "").includes("news") || String(task.source_id ?? "").includes("news"));
  const publicOk = publicTasks.filter((task) => task.last_run_succeeded === true || ["success", "ready"].includes(String(task.status))).length;
  const newsOk = newsTasks.filter((task) => ["success", "ready"].includes(String(task.status))).length;
  const publicBlocked = publicTasks.some((task) => ["blocked", "failed", "error"].includes(String(task.status)));
  const newsBlocked = newsTasks.some((task) => ["blocked", "failed", "error"].includes(String(task.status)));
  const publicDue = publicTasks.some((task) => task.refresh_due === true || String(task.status) === "due");
  const publicPending = publicTasks.length > 0 && (publicOk < publicTasks.length || publicDue);
  const newsPending = newsTasks.length > 0 && newsOk < newsTasks.length;
  const tone: Tone = publicBlocked || newsBlocked || publicPending || newsPending || blocked > 0 ? "warning" : total ? "success" : "muted";
  const status = publicBlocked || newsBlocked ? "部分来源异常" : publicPending || newsPending ? "部分待更新" : total ? "运行正常" : "等待检查";
  return {
    status,
    tone,
    total,
    success,
    ready,
    blocked,
    needsAction,
    automationReady,
    publicReadyLabel: publicTasks.length ? `${publicOk}/${publicTasks.length}` : "待检查",
    newsReadyLabel: newsTasks.length ? `${newsOk}/${newsTasks.length}` : "待检查",
    generatedAt: source?.generated_at,
    cards: [
      {
        label: "外部宏观公开源",
        value: publicTasks.length ? `${publicOk}/${publicTasks.length}` : "待检查",
        detail: "EIA、FRED、CFTC 用于宏观、能源和持仓补充；不代表服务器行情不可用。",
        tone: publicBlocked || publicPending ? "warning" as Tone : "success" as Tone
      },
      {
        label: "新闻事件",
        value: newsTasks.length ? `${newsOk}/${newsTasks.length}` : "待检查",
        detail: "公开新闻与官方公告进入事件库和证据链。",
        tone: newsBlocked || newsPending ? "warning" as Tone : "success" as Tone
      },
      {
        label: "服务器行情价格",
        value: automationReady ? "已纳入" : "待检查",
        detail: "用于观察原油、PX、PTA、MEG 与 POY/DTY 传导。",
        tone: automationReady ? "success" as Tone : "muted" as Tone
      },
      {
        label: "系统提醒",
        value: publicBlocked || newsBlocked ? "需关注" : publicPending || newsPending ? "待更新" : "正常",
        detail: "外部网站异常会保留记录，不影响其它来源更新。",
        tone: publicBlocked || newsBlocked || publicPending || newsPending ? "warning" as Tone : "success" as Tone
      }
    ]
  };
}

function buildBusinessEvents(data?: AgentWorkbenchData, live?: WorkbenchLiveData): BusinessEvent[] {
  const libraryEvents = live?.eventLibrary.ok ? live.eventLibrary.value.events : [];
  if (live?.eventLibrary.ok) {
    return businessEventsFromLibrary(libraryEvents);
  }

  const realEvents = live?.events.ok ? live.events.value : [];
  if (realEvents.length) {
    return deduplicateBusinessEvents(realEvents.slice(0, 100).map((event) => {
      const category = eventCategory(event);
      const affected = event.affected_products.map(displayProduct).filter(Boolean);
      const impact = event.impact_chain.length
        ? event.impact_chain.map((item) => cleanBusinessText(item)).join(" → ")
        : eventBusinessImpact({ category, summary: event.judgement, affected, direction: event.confidence >= 0.75 ? "进入复核" : "观察" });
      return {
        id: event.event_id,
        category,
        title: eventBusinessTitle({
          category,
          title: event.title,
          summary: event.judgement,
          impact,
          affected,
          direction: event.confidence >= 0.75 ? "进入复核" : "观察"
        }),
        time: formatDate(event.occurred_at || data?.generated_at),
        impact,
        evidence: [
          `${event.evidence_level} 级证据`,
          `置信度 ${formatPercent(event.confidence)}`,
          affected.length ? `影响品种：${affected.join("、")}` : "影响品种未返回"
        ],
        counter: event.counter_evidence.length ? event.counter_evidence.map((item) => cleanBusinessText(item)) : ["反证信息未返回"],
        change: event.confidence >= 0.75 && event.evidence_level !== "D"
          ? "进入今日判断复核，需结合价格和行业指标确认。"
          : "当前只作为观察线索，不单独改变今日判断。",
        tone: eventTone(event),
        links: [
          { label: "新闻" },
          { label: "公告" },
          { label: "来源" }
        ]
      };
    }));
  }

  const briefItems = live?.morningBrief.ok ? live.morningBrief.value : [];
  if (briefItems.length) {
    return deduplicateBusinessEvents(briefItems.map((item, index) => ({
      id: `brief-${index}`,
      category: item.linked_factors.includes("CRUDE") ? "原油" : item.linked_factors.includes("EVENTS") ? "宏观" : "供需",
      title: cleanBusinessText(item.title),
      time: formatDate(data?.generated_at),
      impact: cleanBusinessText(item.body),
      evidence: item.linked_factors.map((factor) => cleanBusinessText(factor)),
      counter: ["简报未返回单独反证，需进入事件详情复核。"],
      change: item.priority === "high" ? "进入今日判断复核。" : "作为观察线索。",
      tone: item.priority === "high" ? "warning" : "info",
      links: [{ label: "新闻" }, { label: "公告" }, { label: "来源" }]
    })));
  }

  return [];
}

function businessEventIdentity(event: BusinessEvent) {
  const normalizedTitle = event.title
    .toLocaleLowerCase("zh-CN")
    .replace(/^[^：:]{1,12}[：:]/u, "")
    .replace(/[\s\p{P}\p{S}]+/gu, "");
  const businessTime = String(event.time ?? "").slice(0, 10);
  return {
    id: String(event.id ?? "").trim(),
    titleTime: `${normalizedTitle}|${businessTime}`
  };
}

function deduplicateBusinessEvents(events: BusinessEvent[]) {
  const seenIds = new Set<string>();
  const seenTitleTimes = new Set<string>();
  return events.filter((event) => {
    const identity = businessEventIdentity(event);
    if ((identity.id && seenIds.has(identity.id)) || seenTitleTimes.has(identity.titleTime)) return false;
    if (identity.id) seenIds.add(identity.id);
    seenTitleTimes.add(identity.titleTime);
    return true;
  });
}

function businessEventsFromLibrary(libraryEvents: EventLibraryItem[]): BusinessEvent[] {
  // Event-library pages are already canonicalized by the backend before the
  // pagination cursor is calculated. Preserve every returned identity here so
  // the UI count and load-more cursor cannot diverge from the API.
  return libraryEvents.map((event) => {
    const affected = event.affected_products.map((item) => cleanBusinessText(item)).filter(Boolean);
    const sourceTitle = event.overview_source_title || event.factual_title || event.title;
    const titleEncodingIssue = sourceTitle.includes("\uFFFD");
    const qualifiedFactSummary = qualifiedChineseEventSummary(event.factual_summary);
    const factSummaryReady = event.summary_generation_status === "ready"
      && !titleEncodingIssue
      && event.source_content_status !== "title_only"
      && event.source_content_status !== "partial_text"
      && Boolean(qualifiedFactSummary);
    const analysisReady = event.summary_generation_status === "ready"
      && event.source_content_status !== "title_only"
      && event.source_content_status !== "partial_text"
      && factSummaryReady
      && event.analysis_available !== false;
    const sourceText = `${formatInteger(event.article_count)} 篇资料，${formatInteger(event.source_count)} 类来源`;
    const direction = analysisReady ? cleanBusinessText(event.direction, "中性") : event.overview_text ? "信息线索" : "待研判";
    return {
      id: event.id,
      category: cleanBusinessText(event.category),
      categoryKey: event.category_key,
      // A source title and a generated summary are separate audit fields. Never
      // concatenate the summary into the title merely to make an English title
      // look more customer-facing.
      title: titleEncodingIssue ? "来源标题编码异常，待核验" : compactText(sourceTitle, 96),
      sourceTitleRaw: titleEncodingIssue ? sourceTitle : undefined,
      time: formatDate(event.published_at || event.time),
      timeLabel: event.time_label || "来源发布时间",
      factSummary: qualifiedFactSummary,
      overviewText: event.overview_text?.trim() || undefined,
      overviewBasis: event.overview_basis,
      factSummaryReady,
      summaryGenerationStatus: event.summary_generation_status,
      summaryStatusLabel: event.summary_status_label?.trim() || undefined,
      sourceContentStatus: event.source_content_status,
      sourceContentStatusLabel: event.source_content_status_label?.trim() || undefined,
      sourceName: cleanBusinessText(event.source_name || ""),
      sourceUrl: event.source_url || event.links.find((link) => link.href)?.href,
      analysisAvailable: event.analysis_available !== false,
      analysisReady,
      direction,
      affectedProducts: analysisReady ? affected : [],
      impactStrength: analysisReady ? cleanBusinessText(event.impact_strength, "待评估") : "待评估",
      evidenceLevel: analysisReady ? cleanBusinessText(event.evidence_label, "待复核") : "待正文确认",
      impact: analysisReady
        ? eventAnalysisSummary(event, affected)
        : eventAnalysisGateReason({
            id: event.id,
            category: event.category,
            title: event.factual_title || event.title,
            time: event.published_at || event.time,
            factSummaryReady,
            summaryGenerationStatus: event.summary_generation_status,
            summaryStatusLabel: event.summary_status_label,
            sourceContentStatus: event.source_content_status,
            sourceContentStatusLabel: event.source_content_status_label,
            analysisAvailable: event.analysis_available,
            impact: "",
            evidence: [],
            counter: [],
            change: "",
            tone: "muted",
            links: []
          }),
      evidence: analysisReady
        ? [
            event.evidence_label,
            event.impact_strength,
            sourceText,
            affected.length ? `影响品种：${affected.join("、")}` : "影响品种待识别"
          ]
        : [event.source_content_status_label || "正文待补充", sourceText],
      counter: event.counter_evidence.length ? event.counter_evidence.map((item) => cleanBusinessText(item)) : ["反证信息待观察"],
      change: analysisReady ? cleanBusinessText(event.change_label) : event.overview_text ? "本条作为信息线索供参考，当前不改变价格判断。" : "展示门禁通过后再决定是否进入今日判断。",
      tone: analysisReady ? eventLibraryTone(event) : "muted",
      political: event.political_intelligence ? {
        summary: cleanBusinessText(event.political_intelligence.summary),
        interestMap: event.political_intelligence.interest_map.map((item) => cleanBusinessText(item)),
        powerStructure: event.political_intelligence.power_structure.map((item) => cleanBusinessText(item)),
        stakeholders: event.political_intelligence.stakeholders.map((item) => ({
          name: cleanBusinessText(item.name),
          role: cleanBusinessText(item.role),
          interest: cleanBusinessText(item.interest),
          boundary: cleanBusinessText(item.boundary)
        })),
        speechAct: {
          label: cleanBusinessText(event.political_intelligence.speech_act.label),
          reason: cleanBusinessText(event.political_intelligence.speech_act.reason)
        },
        executionLikelihood: {
          label: cleanBusinessText(event.political_intelligence.execution_likelihood.label),
          reason: cleanBusinessText(event.political_intelligence.execution_likelihood.reason)
        },
        priceInStatus: {
          label: cleanBusinessText(event.political_intelligence.price_in_status.label),
          reason: cleanBusinessText(event.political_intelligence.price_in_status.reason)
        },
        actionBoundary: event.political_intelligence.action_boundary.map((item) => cleanBusinessText(item)),
        secondOrderRisks: event.political_intelligence.second_order_risks.map((item) => cleanBusinessText(item)),
        sourceBasis: event.political_intelligence.source_basis.map((item) => cleanBusinessText(item))
      } : undefined,
      links: event.links?.length ? event.links.map((link) => ({ label: cleanBusinessText(link.label || "来源"), href: link.href })) : [{ label: "来源" }]
    };
  });
}

function eventRiskLabel(tone: Tone) {
  if (tone === "danger") return "高风险";
  if (tone === "warning") return "中风险";
  if (tone === "success") return "低风险";
  return "观察";
}

function businessEventNeedsSource(event?: BusinessEvent) {
  return event?.sourceContentStatus === "title_only" || event?.sourceContentStatus === "partial_text";
}

function businessEventPendingStage(event?: BusinessEvent) {
  if (!event || event.analysisReady !== false) return undefined;
  if (event.overviewText) return "中文概述";
  if (event.sourceContentStatus === "title_only" || event.sourceContentStatus === "partial_text") {
    if (event.sourceContentStatusLabel && /原站|原文链接|来源文件/.test(event.sourceContentStatusLabel)) {
      return event.sourceContentStatusLabel;
    }
    return "正文待补";
  }
  if (event.summaryGenerationStatus !== "ready") {
    return event.summaryStatusLabel || "等待进入摘要队列";
  }
  return "影响待研判";
}

function businessEventPendingTone(event?: BusinessEvent): Tone {
  if (event?.overviewText) return "info";
  if (businessEventNeedsSource(event) || event?.summaryGenerationStatus === "dead_letter") return "warning";
  if (event?.summaryGenerationStatus === "queued" || event?.summaryGenerationStatus === "processing" || event?.summaryGenerationStatus === "provider_delayed") return "info";
  return businessEventPendingStage(event) ? "muted" : event?.tone ?? "muted";
}

function businessEventRiskLabel(event?: BusinessEvent) {
  return businessEventPendingStage(event) || eventRiskLabel(event?.tone ?? "muted");
}

function eventDirectionLabel(event?: BusinessEvent) {
  if (event?.analysisReady === false) return event.overviewText ? "不作推断" : "待研判";
  if (event?.direction) return event.direction;
  const text = `${event?.title ?? ""} ${event?.impact ?? ""} ${event?.change ?? ""}`;
  if (text.includes("利空") || text.includes("下行") || text.includes("回落")) return "利空";
  if (text.includes("利多") || text.includes("上行") || text.includes("抬升") || text.includes("上涨")) return "利多";
  return "中性";
}

function eventDirectionTone(direction: string): Tone {
  if (direction === "利多") return "danger";
  if (direction === "利空") return "success";
  return "info";
}

function eventProductTags(event?: BusinessEvent) {
  if (!event) return [];
  if (event.analysisReady === false) return [];
  if (event.affectedProducts?.length) return event.affectedProducts.slice(0, 4);
  const text = `${event.title} ${event.impact} ${event.evidence.join(" ")}`;
  return ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY", "航运", "燃料油"]
    .filter((item) => text.includes(item))
    .slice(0, 4);
}

function businessSources(data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const ragSources = live?.retrieval.ok
    ? live.retrieval.value.documents.filter(isCustomerVisibleRagEvidence).map((document) => sourceCategoryLabel(document.source_id || document.doc_type))
    : [];
  const eventSources = live?.events.ok && live.events.value.length ? ["新闻与公告数据"] : [];
  if (!data && !ragSources.length && !eventSources.length) return [];
  return Array.from(
    new Set([
      ...(data?.evidence_bundle.top_sources ?? []).map((source) => sourceCategoryLabel(source)),
      ...(data?.authorized_sources ?? []).map((source) => sourceCategoryLabel(source.name)),
      ...ragSources,
      ...eventSources,
      "历史研判样本"
    ])
  ).slice(0, 6);
}

function agentStatus(data: AgentWorkbenchData | undefined, keywords: string[], fallback: AgentRunStatus = "running") {
  if (!data) return fallback;
  const haystacks = [
    ...data.runs.map((run) => ({ text: `${run.name} ${run.agent} ${run.summary} ${run.output}`, status: run.status })),
    ...data.tasks.map((task) => ({ text: `${task.label} ${task.summary}`, status: task.status }))
  ];
  const found = haystacks.find((item) => keywords.some((keyword) => item.text.toLowerCase().includes(keyword.toLowerCase())));
  return found?.status ?? fallback;
}

function reportPipelineStall(reports: DeliveryClientReport[]): string | undefined {
  const dates = reports.map((report) => {
    const generated = (report as DeliveryClientReport & { generated_at?: string }).generated_at;
    const raw = generated || report.business_date || report.as_of_time || report.title.match(/\d{4}-\d{2}-\d{2}/)?.[0];
    if (!raw) return undefined;
    const timestamp = Date.parse(raw.length === 10 ? `${raw}T00:00:00+08:00` : raw);
    return Number.isFinite(timestamp) ? { timestamp, date: raw.slice(0, 10) } : undefined;
  }).filter((value): value is { timestamp: number; date: string } => Boolean(value));
  if (!dates.length || dates.length !== reports.length) return undefined;
  const latest = dates.reduce((a, b) => a.timestamp > b.timestamp ? a : b);
  return Date.now() - latest.timestamp > 14 * MARKET_DAY_MS
    ? `管线停摆自 ${latest.date}，正式报告待操作员重启`
    : undefined;
}

function reportStatus(report?: DeliveryClientReport) {
  if (!report) return "待生成";
  if (report.content_status !== "ready" || report.download_available !== true) return "待生成";
  const status = statusFromText(report.status);
  if (status === "success") return "可查看";
  if (status === "needs_human_review") return "需关注";
  if (status === "blocked") return "待处理";
  return "生成中";
}

function FieldTag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <Tag color={toneColor[tone]} className="delivery-status-tag">{children}</Tag>;
}

function evidenceNodeIcon(kind?: string, title?: string) {
  const text = `${kind ?? ""} ${title ?? ""}`;
  if (text.includes("question") || text.includes("业务问题")) return <QuestionCircleOutlined />;
  if (text.includes("boundary") || text.includes("判断边界")) return <SafetyCertificateOutlined />;
  if (text.includes("chain") || text.includes("产业链")) return <ApartmentOutlined />;
  if (text.includes("价格")) return <RiseOutlined />;
  if (text.includes("行业") || text.includes("供需")) return <BarChartOutlined />;
  if (text.includes("新闻") || text.includes("事件")) return <AlertOutlined />;
  if (text.includes("历史") || text.includes("复盘")) return <ClockCircleOutlined />;
  if (text.includes("反证") || text.includes("冲突")) return <WarningOutlined />;
  if (text.includes("结论")) return <CheckCircleOutlined />;
  return <NodeIndexOutlined />;
}

// Restore the original business-lane palette independently of execution kind/status.
const pipelinePalette = {
  control: { label: "编排", color: "#2563eb" },
  data: { label: "数据", color: "#16a34a" },
  event: { label: "事件理解", color: "#7c3aed" },
  prediction: { label: "预测推演", color: "#2563eb" },
  decision: { label: "复盘", color: "#7c3aed" },
  assistant: { label: "链外", color: "#64748b" }
};
function pipelineLane(nodeId: string): keyof typeof pipelinePalette {
  if (["collect", "clean", "index"].includes(nodeId)) return "data";
  if (["event_summary", "event_overview", "factor_score"].includes(nodeId)) return "event";
  if (["event_signal", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion", "seven_product", "shadow_eval"].includes(nodeId)) return "prediction";
  if (["counter_scan", "daily_interpretation", "report_assembly"].includes(nodeId)) return "decision";
  if (nodeId === "assistant") return "assistant";
  return "control";
}

// 展示编号不参与节点 ID/拓扑；顺序与 pipelineSkeletonNodes 一致。
const pipelineNodeOrder: Record<string, number> = {
  collect: 1, clean: 2, index: 3,
  event_summary: 4, event_overview: 5, factor_score: 6,
  event_signal: 7, political_analysis: 8, historical_analog: 9, product_synthesis: 10,
  skeptic_review: 11, event_fusion: 12, seven_product: 13, shadow_eval: 14,
  counter_scan: 15, daily_interpretation: 16, report_assembly: 17,
  assistant: 18, unified_memory: 19
};
function WorkflowFlowNode({ data, selected }: NodeProps<Node<PipelineFlowNodeData>>) {
  const status = data.status;
  const isPlanning = data.nodeId === "unified_memory" && data.memoryPlanning !== false;
  const contract = pipelineRelayContracts[data.nodeId];
  // 状态药丸（对照原型：✓成功 / ⚠降级 / 虚线等待 / 灰空闲），图标+彩底。
  const statusPill = isPlanning
    ? { tone: "is-plan", icon: <ExperimentOutlined />, label: "规划中" }
    : status === "ok"
      ? { tone: "is-ok", icon: <CheckCircleFilled />, label: pipelineStatusLabels.ok }
      : status === "degraded"
        ? { tone: "is-deg", icon: <WarningFilled />, label: pipelineStatusLabels.degraded }
        : status === "waiting"
          ? { tone: "is-wait", icon: <ClockCircleOutlined />, label: pipelineStatusLabels.waiting }
          : status === "idle"
            ? { tone: "is-idle", icon: <MinusCircleOutlined />, label: pipelineStatusLabels.idle }
            : { tone: "is-idle", icon: <QuestionCircleOutlined />, label: "状态未知" };
  return (
    <div
      style={{ "--pipeline-card-height": `${pipelineCardGeometry(data.nodeId).height}px`, "--pipeline-accent": pipelinePalette[pipelineLane(data.nodeId)].color, "--pipeline-card-width": `${pipelineCardWidth(data.nodeId) * (data.compact ? 0.7 : 1)}px` } as CSSProperties}
      className={`pipeline-flow-node is-relay${data.budget ? " has-budget" : ""} is-lane-${pipelineLane(data.nodeId)} is-${data.kind}${status ? ` is-${status}` : " is-unknown"}${selected ? " is-selected" : ""}${isPlanning ? " is-adr10" : ""}${["event_fusion", "seven_product"].includes(data.nodeId) ? " is-decision-node" : ""}${data.lensActive ? " is-lens-active" : ""}`}
      aria-label={`${data.name}，${isPlanning ? "规划中" : status ? pipelineStatusLabels[status] : "状态未知"}，${data.statusDetail || "状态详情待返回"}`}
    >
      <Handle type="target" position={Position.Top} id="top" />
      <Handle type="target" position={Position.Left} id="left" />
      <Handle type="target" position={Position.Right} id="right" />
      <Handle type="target" position={Position.Bottom} id="bottom" />
      <header className="pipeline-node-head">
        <span className="pipeline-relay-number">{pipelineNodeOrder[data.nodeId]}</span>
        <strong className="pipeline-node-name">{data.name}</strong>
      </header>
      <div className="pipeline-node-status">
        <span className={`pipeline-node-status-pill ${statusPill.tone}`} data-status={isPlanning ? "planning" : status ?? "unknown"}>
          {statusPill.icon}<span>{statusPill.label}</span>
        </span>
        <span className="pipeline-node-kind">{data.kind === "agent" ? "智能体" : "数据"}</span>
      </div>
      <ol className="pipeline-relay-steps" aria-label="节点执行接力">
        <li><span className="pipeline-relay-dot" /><div><h4><strong>收到</strong> <span>输入约定</span></h4><p>{contract?.input ?? "上游材料"}</p></div></li>
        <li className="pipeline-relay-execution"><span className="pipeline-relay-dot" /><div><h4><strong>执行</strong> <span>{isPlanning ? "尚未启用" : "实际状态"}</span></h4><p className="pipeline-node-detail" title={data.statusDetail}>{data.statusDetail || "状态详情待返回"}</p></div></li>
        <li><span className="pipeline-relay-dot" /><div><h4><strong>交出</strong> <span>输出约定</span></h4><p>{contract?.output ?? "下游工件"}</p></div></li>
      </ol>
      {data.budget ? <div className="pipeline-node-budget-row"><span className="pipeline-node-budget" data-testid="pipeline-budget-badge">调用 {data.budget.used ?? "—"}/{data.budget.cap}{` · 回退 ${data.budget.fallback ?? "—"}`}</span></div> : null}
      <footer className="review-node-footer"><time>{data.timestamp ? formatDate(data.timestamp) : (isPlanning ? "—" : "暂无更新时间")}</time><span>查看详情 ›</span></footer>
      <Handle type="source" position={Position.Left} id="left" />
      <Handle type="source" position={Position.Right} id="right" />
      <Handle type="source" position={Position.Top} id="top" />
      <Handle type="source" position={Position.Bottom} id="bottom" />
      {data.nodeId === "assistant" ? ["event_summary", "skeptic_review", "daily_interpretation"].map((id, index) => (
        <Handle key={id} type="target" position={Position.Left} id={`entry-${id}`} style={{ top: `${35 + index * 15}%` }} />
      )) : null}
    </div>
  );
}

const workflowNodeTypes = {
  workflowAgent: WorkflowFlowNode
};

function PageFrame({
  title,
  eyebrow,
  description,
  actions,
  children,
  className,
  hideHeader = false
}: {
  title: string;
  eyebrow?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  hideHeader?: boolean;
}) {
  return (
    <section aria-label={title} className={`delivery-page ${className ?? ""} ${actions && !hideHeader ? "review-has-actions" : "review-no-page-heading"}`}>
      {actions && !hideHeader ? <div className="delivery-page-title review-actions-only">
        <div className="delivery-page-actions">{actions}</div>
      </div> : null}
      <div className="delivery-page-body">{children}</div>
    </section>
  );
}

function Panel({
  title,
  subtitle,
  icon,
  extra,
  children,
  className,
  hideHeader = false
}: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
  hideHeader?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const expandable = /(?:trend-panel|evidence-canvas-panel|workflow-board-panel)/.test(className ?? "");
  useEffect(() => {
    if (!expanded) return;
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [expanded]);
  useEffect(() => { window.dispatchEvent(new Event("resize")); }, [expanded]);
  return (
    <section className={`delivery-panel ${className ?? ""} ${expanded ? "review-focus" : ""}`}>
      {!hideHeader ? <header className="delivery-panel-head">
        <div>
          <strong>{icon}{title}</strong>
        </div>
        {extra ? <div>{extra}</div> : null}
        {expandable ? <button type="button" className="review-expand" aria-pressed={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? "恢复" : "放大"}</button> : null}
      </header> : null}
      <div className="delivery-panel-body">{children}</div>
    </section>
  );
}

function MetricCard({
  title,
  value,
  detail,
  tone,
  icon
}: {
  title: string;
  value: string;
  detail: string;
  tone: Tone;
  icon: ReactNode;
}) {
  return (
    <div className={`delivery-metric is-${tone}`}>
      <div className="delivery-metric-icon">{icon}</div>
      <span>{title}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function LoadingSurface({ onReload }: { onReload: () => void }) {
  return <div className="workbench-loading-surface"><ContentLoading /><Button icon={<ReloadOutlined />} onClick={onReload}>重新读取</Button></div>;
}

function ErrorSurface({ message, onReload }: { message: string; onReload: () => void }) {
  return (
    <div className="delivery-loading is-error">
      <WarningOutlined />
      <strong>无法读取交付状态</strong>
      <span>{cleanBusinessText(message, "后端服务暂时不可用，请稍后重试。")}</span>
      <Button type="primary" icon={<ReloadOutlined />} onClick={onReload}>重新读取</Button>
    </div>
  );
}

function WorkbenchDegradedBanner({
  data,
  live,
  refreshError,
  onReload
}: {
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  refreshError?: string;
  onReload: () => void;
}) {
  if (!data) return null;
  const labels: Record<keyof WorkbenchLiveData, string> = {
    overview: "总览", events: "事件摘要", eventLibrary: "事件库", morningBrief: "晨报",
    factors: "因子", latestPrices: "最新价格", priceComparison: "原油价格",
    marketChain: "原料链", retrieval: "证据检索", graph: "证据关系",
    fullChain: "成本链判断", ragVisual: "证据快照", formalPredictions: "正式预测批次",
    eventFactors: "事件依据卡"
  };
  const failed = live
    ? (Object.entries(live) as [keyof WorkbenchLiveData, DataStatus<unknown>][])
        .filter(([, status]) => !status.ok && !status.pending)
        .map(([key]) => labels[key])
    : [];
  if (!refreshError && !failed.length) return null;
  const detail = refreshError
    ? "本次刷新未完成，当前七个模块继续展示上次成功保存的真实快照。"
    : `${failed.slice(0, 4).join("、")}${failed.length > 4 ? `等 ${failed.length} 项` : ""}暂未返回`;
  return (
    <section className="workbench-degraded-banner" data-testid="workbench-degraded-banner" role="status">
      <WarningOutlined />
      <div><strong>数据连接降级</strong><span>{detail}</span></div>
      <Button size="small" icon={<ReloadOutlined />} onClick={onReload}>重新读取</Button>
    </section>
  );
}

function chainNodeProduct(label: string, live?: WorkbenchLiveData) {
  return marketProductView(label, live);
}

function chainNodeChange(view?: MarketChainProductView) {
  if (!view?.price_series.length) return "未返回";
  const latest = view.price_series[view.price_series.length - 1];
  const previous = view.price_series[view.price_series.length - 2];
  if (!latest || !previous) return view.latest_price.quality_label ?? "已覆盖";
  const delta = Number(latest.value) - Number(previous.value);
  if (delta > 0) return `+${delta.toFixed(2)}`;
  if (delta < 0) return delta.toFixed(2);
  return "持平";
}

function ProductSymbol({ product }: { product: string }) {
  const Icon = product === "原油" ? Cylinder : product === "石脑油" ? Factory
    : product === "PX" ? Hexagon : product === "PTA" ? FlaskConical
    : product === "MEG" ? Network : Layers;
  return <Icon size={26} strokeWidth={1.6} className="chain-product-icon" aria-hidden="true" />;
}

function ChainMini({ live }: { live?: WorkbenchLiveData }) {
  const items = ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"];
  const loading = !live;
  const fullChainItems = live?.fullChain.ok ? live.fullChain.value.summary : [];
  return (
    <div className="chain-mini-wrap">
      <div className="chain-mini chain-mini-designed" aria-label="原料链简图：原油至PTA，PTA与MEG聚合至POY，POY加弹至DTY">
        {items.map((item, index) => {
          const product = chainNodeProduct(item, live);
          const latest = latestPriceFor(live, item);
          const expectedKey = item === "原油" ? "CRUDE" : productKey(item);
          const fullChain = fullChainItems.find((row) => String(row.product ?? "").toUpperCase() === expectedKey);
          const latestAvailable = latest?.latest && Number.isFinite(Number(latest.latest.last));
          const marketAvailable = product?.latest_price.status === "available"
            && (!latestAvailable || Date.parse(product.latest_price.date || "") >= Date.parse(latest?.latest?.observed_at || ""));
          const fullChainAvailable = Number.isFinite(Number(fullChain?.value));
          const available = marketAvailable || latestAvailable || fullChainAvailable;
          const value = loading
            ? "读取中"
            : marketAvailable
            ? formatPrice(product.latest_price.value, product.latest_price.unit)
            : latestAvailable
              ? formatPrice(latest.latest!.last, latest.latest!.unit)
              : fullChainAvailable
                ? formatPrice(Number(fullChain!.value), fullChain?.unit)
                : "未返回";
          const detail = loading
            ? "价格正在读取"
            : marketAvailable
            ? `${chainNodeChange(product)} · 观测 ${formatDate(product.latest_price.date)}`
            : latestAvailable
              ? `观测 ${latestPriceDate(latest)}`
              : fullChainAvailable
                ? `观测 ${formatDate(fullChain?.observed_at)}`
                : "价格序列未返回";
          return (
            <div key={item} data-product={item} className={`chain-mini-node ${available ? "has-data" : "is-missing"}`}>
              <i className={`material-illustration material-${index < 5 ? index : index + 1}`} aria-hidden="true" /><span>{item}</span>
              <strong>{value}</strong>
              <small>{detail}</small>
              {[0, 1, 2, 4, 5].includes(index) ? <ArrowRightOutlined className="material-arrow" aria-hidden="true" /> : null}
            </div>
          );
        })}
        <div className="chain-pta-join" aria-label="PTA 汇入聚酯" />
        <div className="chain-mini-node polymer-stage"><i className="material-illustration material-5" aria-hidden="true" /><span>聚酯</span><small>PTA + MEG 聚合</small><ArrowRightOutlined className="material-arrow" aria-hidden="true" /></div>
      </div>
      <div className="chain-mini-foot">
        <span>{loading ? "观测时间正在读取" : "观测时间见各品种 · 亚洲/上海"}</span>
        <span>PTA + MEG → 聚酯 → POY → DTY · 原始币种</span>
      </div>
    </div>
  );
}

function chainPressureTone(view?: MarketChainProductView): "up" | "down" | "flat" | "missing" {
  if (!view?.price_series.length) return "missing";
  const latest = view.price_series[view.price_series.length - 1];
  const previous = view.price_series[view.price_series.length - 2];
  if (!latest || !previous) return "flat";
  const delta = Number(latest.value) - Number(previous.value);
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "flat";
}

function chainPressureLabel(tone: "up" | "down" | "flat" | "missing") {
  if (tone === "up") return "偏强";
  if (tone === "down") return "偏弱";
  if (tone === "flat") return "震荡";
  return "暂无数据";
}

function EvidenceBalance({ adopted, excluded, conflicts }: { adopted: number; excluded: number; conflicts: number }) {
  const total = Math.max(1, adopted + excluded + conflicts);
  return (
    <div className="evidence-balance" aria-label="证据天平">
      <span className="is-adopted" style={{ flexGrow: Math.max(1, adopted) }}>进入判断 <b>{formatInteger(adopted)}</b></span>
      <span className="is-excluded" style={{ flexGrow: Math.max(1, excluded) }}>被排除 <b>{formatInteger(excluded)}</b></span>
      <span className="is-conflict" style={{ flexGrow: Math.max(1, conflicts) }}>有冲突 <b>{formatInteger(conflicts)}</b></span>
      <em style={{ left: `${Math.min(92, Math.max(8, (adopted / total) * 100))}%` }} />
    </div>
  );
}

function ChainPressureRail({ live }: { live?: WorkbenchLiveData }) {
  const items = ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"];
  return (
    <div className="chain-pressure-rail" aria-label="全链路压力条">
      {items.map((item, index) => {
        const product = chainNodeProduct(item, live);
        const tone = chainPressureTone(product);
        return (
          <span key={`pressure-${item}`} className={`is-${tone}`}>
            <b>{item}</b>
            <small>{chainPressureLabel(tone)}</small>
            {index < items.length - 1 ? <i /> : null}
          </span>
        );
      })}
    </div>
  );
}

function ReferenceOverviewDashboard() {
  const nav = [
    { label: "总览看板", icon: <BarChartOutlined /> },
    { label: "行情与原料链", icon: <PartitionOutlined /> },
    { label: "证据图谱", icon: <NodeIndexOutlined /> },
    { label: "运行状态", icon: <ApartmentOutlined /> },
    { label: "AI 研判助手", icon: <CommentOutlined /> },
    { label: "研判报告", icon: <FileTextOutlined /> }
  ];
  const metrics = [
    {
      title: "今日结论",
      tag: "偏强",
      body: "原料价格预计维持震荡偏强",
      detail: "置信度 72%",
      icon: <CheckSquareOutlined />,
      tone: "green"
    },
    {
      title: "成本压力方向",
      tag: "上行",
      body: "成本端压力增强",
      detail: "较昨日 ↑ 2.6%",
      icon: <ArrowUpOutlined />,
      tone: "orange"
    },
    {
      title: "数据新鲜度",
      tag: "良好",
      body: "覆盖近 30 天数据",
      detail: "最新更新 2026/06/22 10:10",
      icon: <DatabaseOutlined />,
      tone: "blue"
    },
    {
      title: "证据可信度",
      tag: "较高",
      body: "证据链完整度较高",
      detail: "评分 78 / 100",
      icon: <SafetyCertificateOutlined />,
      tone: "green"
    }
  ];
  const riskRows = [
    ["原油价格大幅波动", "高", "高", "密切跟踪国际油价及地缘动态，考虑锁定部分原料"],
    ["下游需求不及预期", "中", "中", "关注终端需求及库存变化，验证成本传导强弱"],
    ["装置检修延期或不确定", "中", "中", "跟踪装置动态，及时调整供应预期"],
    ["宏观政策或贸易政策变化", "低", "低", "关注政策动向，评估潜在影响"]
  ];
  const events = [
    ["供给", "国内某大型 PX 装置检修 15 天", "涉及产能约 70 万吨/年，计划 6 月底检修，预计 7 月中旬重启。", "1 小时前"],
    ["原油", "中东局势紧张，原油走强", "地缘风险升温，国际油价上涨超 2%，成本端支撑增强。", "2 小时前"],
    ["需求", "聚酯开工小幅回升，库存下降", "聚酯综合开工提升至 88.1%，成品库存环比下降 1.2 天。", "5 小时前"],
    ["政策", "宏观政策定调稳定增长", "多部门释放稳增长信号，关注后续具体政策落地节奏。", "10 小时前"]
  ];
  const evidence = [
    ["行业价格数据", "20 条", "最新 2026/06/22 10:10", "blue", <BarChartOutlined />],
    ["官方能源数据", "8 条", "最新 2026/06/22 09:30", "green", <FileTextOutlined />],
    ["新闻公告", "15 条", "最新 2026/06/22 09:50", "orange", <AlertOutlined />],
    ["用户补充文件", "3 份", "最新 2026/06/21 18:10", "purple", <CloudSyncOutlined />]
  ];

  return (
    <div className="reference-workbench">
      <header className="reference-topbar">
        <div className="reference-topbar-left">
          <MenuOutlined />
          <strong>POY/DTY 上游原料智能研判生产工作台</strong>
        </div>
        <div className="reference-topbar-right">
          <span className="reference-bell"><BellOutlined /><i>12</i></span>
          <QuestionCircleOutlined />
          <span className="reference-avatar"><UserOutlined /></span>
          <span>研判团队</span>
        </div>
      </header>

      <aside className="reference-sidebar">
        <nav>
          {nav.map((item, index) => (
            <button key={item.label} className={index === 0 ? "is-active" : ""}>
              <i>{item.icon}</i>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="reference-side-status">
          <strong><CheckCircleOutlined /> 数据覆盖正常</strong>
          <span>更新时间 10:15</span>
        </div>
      </aside>

      <main className="reference-main">
        <h1>总览看板</h1>
        <div className="reference-alert">
          <InfoCircleOutlined />
          <span>当前系统可用于业务研判辅助，请持续关注原料成本、供需与事件扰动</span>
        </div>

        <section className="reference-metrics">
          {metrics.map((metric) => (
            <article key={metric.title} className={`reference-metric is-${metric.tone}`}>
              <div className="reference-metric-icon">{metric.icon}</div>
              <div>
                <div className="reference-metric-title">
                  <strong>{metric.title}</strong>
                  <span>{metric.tag}</span>
                </div>
                <p>{metric.body}</p>
                <small>{metric.detail}</small>
              </div>
            </article>
          ))}
        </section>

        <section className="reference-mid">
          <article className="reference-card reference-summary">
            <header><strong>研判结论摘要</strong></header>
            <div className="reference-card-body">
              <p>综合价格走势、成本变化、供需情况及相关事件影响，POY/DTY 上游原料短期预计维持震荡偏强走势。</p>
              <ul>
                <li>成本端：原油价格偏强运行，石脑油及 PX 成本支撑增强。</li>
                <li>供需端：装置负荷维持高位，部分装置计划检修，供应略有收紧。</li>
                <li>事件端：中东地缘紧张及部分装置检修带来不确定性。</li>
                <li>风险提示：需求变化不及预期，政策及宏观风险，原油波动放大。</li>
              </ul>
            </div>
            <footer>
              <span>研判时间：2026/06/22 10:10</span>
              <span>研判周期：近 7 天</span>
            </footer>
          </article>

          <article className="reference-card reference-risk-table">
            <header>
              <strong>风险提示与下一步建议</strong>
              <a>查看全部 ›</a>
            </header>
            <div className="reference-table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>风险事项</th>
                    <th>风险等级</th>
                    <th>影响程度</th>
                    <th>建议措施</th>
                  </tr>
                </thead>
                <tbody>
                  {riskRows.map((row) => (
                    <tr key={row[0]}>
                      <td>{row[0]}</td>
                      <td><span className={`risk-pill is-${row[1]}`}>{row[1]}</span></td>
                      <td><span className={`risk-pill is-${row[2]}`}>{row[2]}</span></td>
                      <td>{row[3]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <footer>下一步重点关注：原油走势、PX 装置动态、下游开工及库存变化、宏观政策动向</footer>
          </article>
        </section>

        <section className="reference-bottom">
          <article className="reference-card reference-chain">
            <header>
              <strong>原料链简图</strong>
              <button>近 7 天⌄</button>
            </header>
            <div className="reference-chain-map">
              <div className="chain-node node-oil"><strong>原油</strong><span>78.45</span><em>+2.56%</em></div>
              <div className="chain-node node-naphtha"><strong>石脑油</strong><span>663</span><em>+2.31%</em></div>
              <div className="chain-node node-px"><strong>PX</strong><span>807.6</span><em>+1.76%</em></div>
              <div className="chain-node node-pta"><strong>PTA</strong><span>5,412</span><em>+0.93%</em></div>
              <div className="chain-node node-meg"><strong>MEG</strong><span>4,671</span><em>+0.24%</em></div>
              <div className="chain-node node-poy"><strong>POY150D</strong><span>7,180</span><em>+0.84%</em></div>
              <i className="solid s1" /><i className="solid s2" /><i className="solid s3" />
              <i className="dashed d1" /><i className="solid s4" /><i className="solid s5" />
            </div>
            <footer>
              <span><b /> 成本传导路径</span>
              <span><b className="is-dashed" /> 替代/影响路径</span>
              <span>单位：元/吨</span>
            </footer>
          </article>

          <article className="reference-card reference-events">
            <header>
              <strong>最新事件摘要</strong>
              <a>查看全部 ›</a>
            </header>
            <div className="reference-list">
              {events.map((event) => (
                <section key={event[1]}>
                  <span>{event[0]}</span>
                  <div>
                    <strong>{event[1]}</strong>
                    <p>{event[2]}</p>
                  </div>
                  <time>{event[3]}</time>
                </section>
              ))}
            </div>
          </article>

          <article className="reference-card reference-evidence">
            <header>
              <strong>证据摘要</strong>
              <a>查看全部 ›</a>
            </header>
            <div className="reference-evidence-list">
              {evidence.map((item) => (
                <section key={String(item[0])}>
                  <i className={`is-${item[3]}`}>{item[4]}</i>
                  <strong>{item[0]}</strong>
                  <span>{item[1]}</span>
                  <small>{item[2]}</small>
                </section>
              ))}
            </div>
          </article>
        </section>
      </main>
    </div>
  );
}

function OverviewModule({
  data,
  live,
  onNavigate,
  sourceStatus,
  sourceChecking,
  sourceCheckError,
  onSourceCheck
}: {
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  onNavigate: (module: ModuleId, options?: { reportView?: ReportWorkspace; reportType?: ReportType }) => void;
  sourceStatus?: DeliverySourceAutomation;
  sourceChecking?: boolean;
  sourceCheckError?: string;
  onSourceCheck: () => void;
}) {
  const currentEventsReady = live?.eventLibrary.ok === true;
  const events = currentEventsReady ? buildBusinessEvents(data, live) : [];
  const conclusionReady = hasFormalConclusion(data, live);
  const lowConfidencePrediction = !conclusionReady ? data?.low_confidence_prediction : undefined;
  const dailyObservation = lowConfidencePrediction?.source === "daily_report";
  const rawRisks = buildRiskItems(data, live);
  const eventRisks = events.filter(event => event.analysisReady === true).slice(0, 3).map((event) => {
    // 聚合器标题常是 URL slug（无空格英文连字符）；这类标题不能直接上屏，
    // 用"类别+事实摘要"生成可读标题，原文保留在依据里。
    const rawTitle = cleanBusinessText(event.title, "");
    const readableTitle = rawTitle && !isMostlyEnglishText(rawTitle) && !/^[a-z0-9]+(-[a-z0-9]+)+$/i.test(rawTitle)
      ? rawTitle
      : `${event.category}动态：${compactText(cleanBusinessText(event.overviewText || event.impact, ""), 40) || "详见风险依据"}`;
    return {
    title: readableTitle,
    detail: event.overviewText || event.impact,
    impact: compactText(event.impact || event.title, 64) || "需结合价格与供需变化继续观察",
    action: `核验${event.affectedProducts?.join("、") || "相关品种"}后续同口径价格与供应公告`,
    level: event.tone === "danger" ? "高" : event.tone === "warning" ? "中" : "低",
    tone: event.tone
    };
  });
  const risks = [...latestPriceRisks(live), ...eventRisks, ...rawRisks]
    .filter((risk, index, all) => all.findIndex(item => item.title === risk.title) === index).slice(0, 5);
  const overview = live?.overview.ok ? live.overview.value : undefined;
  const conclusionConfidence = live?.ragVisual.ok ? (live.ragVisual.value.conclusion_confidence ?? 0) : 0;
  const tone: Tone = conclusionReady ? (conclusionConfidence >= 0.7 ? "success" : "info") : "warning";
  const statusLabel = conclusionReady
    ? "正式辅助判断"
    : dailyObservation ? "观察级 · 非正式" : lowConfidencePrediction ? "低置信 · 非正式" : "暂无方向信号";
  const evidenceRows = evidenceSummaryRows(data, live);
  const fullChainRows = fullChainSummaryRows(live);
  const bossView = bossConclusionView(data, live);
  const sourceView = sourceAutomationView(sourceStatus ?? data?.source_automation);

  return (
    <PageFrame
      title="总览看板"
      eyebrow="今日研判状态"
      description="展示上游成本压力方向、指数、数据新鲜度、证据可信度、数据缺口和观察信号。"
      className="overview-page"
    >
      <div className="overview-layout">
        <div className="metric-grid">
          <MetricCard
            title="判断性质"
            value={statusLabel}
            detail={primaryConclusion(data, live)}
            tone={tone}
            icon={<CheckCircleOutlined />}
          />
          <MetricCard
            title={data?.prediction_authority === "seven_product_ledger" ? "POY/DTY 价格方向" : "成本压力方向"}
            value={conclusionReady && overview
              ? `${cleanBusinessText(overview.status)} ${formatInteger(overview.cost_pressure_index)}`
              : lowConfidencePrediction ? `${lowConfidencePrediction.direction}${dailyObservation ? "（观察级）" : "（低置信）"}` : "未形成"}
            detail={conclusionReady && overview
              ? `1日 ${cleanBusinessText(overview.trend_1d)}，7日 ${cleanBusinessText(overview.trend_7d)}`
              : lowConfidencePrediction
                ? `${lowConfidencePrediction.horizon_days} 天主预测 · 尚未取得正式资格`
                : "正式门禁未通过，暂无可展示的观察级方向。"}
            tone={conclusionReady && overview ? "info" : "warning"}
            icon={<ThunderboltOutlined />}
          />
          <MetricCard
            title="数据新鲜度"
            value={freshnessLabel(data, live)}
            detail={freshnessView(live).detail}
            tone={freshnessView(live).tone}
            icon={<CloudSyncOutlined />}
          />
          <MetricCard
            title={data?.prediction_authority === "seven_product_ledger" ? "参考评分（非正确率）" : "结论置信度"}
            value={conclusionReady
              ? formatPercent(live?.ragVisual.ok ? live.ragVisual.value.conclusion_confidence : null)
              : lowConfidencePrediction ? `${formatPercent(lowConfidencePrediction.confidence)}（未校准）` : "未形成"}
            detail={`覆盖置信 ${formatPercent(overview?.coverage_confidence)} · 检索相关性 ${formatPercent(live?.ragVisual.ok ? live.ragVisual.value.retrieval_confidence : null)}`}
            tone={conclusionReady && (live?.ragVisual.ok ? (live.ragVisual.value.conclusion_confidence ?? 0) : 0) >= 0.7 ? "success" : "warning"}
            icon={<SafetyCertificateOutlined />}
          />
        </div>

        <Panel title="判断依据与核验" subtitle="解释当前方向、关键风险与推翻条件" icon={<BarChartOutlined />} className="overview-decision" extra={<EvidenceDossierButton label="查看判断证明" initialView="issued" context={data?.main_prediction ? {batch_id: data.main_prediction.batch_id} : undefined} />}>
          <div className="overview-decision-narrative">
            <div className="decision-copy">
              <div className="decision-heading">
                <FieldTag tone={conclusionReady ? tone : "warning"}>{conclusionReady ? (tone === "success" ? "可作为辅助" : "观察") : dailyObservation ? "观察级方向 · 非正式" : lowConfidencePrediction ? "低置信预测 · 非正式" : "不形成结论"}</FieldTag>
                <Title level={2}>{conclusionReady
                  ? `${bossView.direction}：${bossView.action}`
                  : lowConfidencePrediction
                    ? dailyObservation ? `${lowConfidencePrediction.direction}：观察级方向已形成` : `${lowConfidencePrediction.direction}：仅供观察`
                    : "当前证据不足，正式结论尚未形成"}</Title>
              </div>
              <div className="decision-primary">
                <span>当前结论</span>
                <strong className="decision-lead">{primaryConclusion(data, live)}</strong>
              </div>
            </div>
            <div className="decision-stats">
              <div><span>观察周期</span><strong>{lowConfidencePrediction && !conclusionReady ? `${lowConfidencePrediction.horizon_days} 日` : "当前研判"}</strong></div>
              <div><span>{data?.prediction_authority === "seven_product_ledger" ? "参考评分" : "置信度"}</span><strong>{conclusionReady ? "正式证据已确认" : lowConfidencePrediction ? formatPercent(lowConfidencePrediction.confidence) : "暂无方向信号"}</strong></div>
              <div><span>使用边界</span><strong>{conclusionReady ? "业务辅助研判" : "非正式 · 不进入报告"}</strong></div>
            </div>
            {(() => {
              // 结论行已展示 rationale 时不再原样重复；改列证据缺口与核验信号。
              const conclusionText = primaryConclusion(data, live);
              const rawBasis = conclusionReady
                ? bossView.why.join("；")
                : lowConfidencePrediction
                  ? formatDateRangesInText(lowConfidencePrediction.rationale)
                  : "";
              const deduped = rawBasis !== conclusionText;
              const basis = rawBasis && deduped
                ? compactText(rawBasis, 180)
                : lowConfidencePrediction && ((lowConfidencePrediction.verification_signals?.length ?? 0) + (lowConfidencePrediction.evidence_gaps?.length ?? 0) > 0)
                  ? compactText([
                      ...(lowConfidencePrediction.evidence_gaps ?? []).slice(0, 2),
                      ...(lowConfidencePrediction.verification_signals ?? []).slice(0, 1)
                    ].join("；"), 180)
                  : "当前证据角色尚未齐全，暂不形成正式结论。";
              return <div className="decision-evidence">
              <span>主要依据{deduped ? "" : "（结论原文见上；此处列缺口与核验信号）"}</span>
              <p>{basis}</p>
            </div>;
            })()}
            {(conclusionReady || lowConfidencePrediction) ? (
              <details className="text-disclosure">
                <summary>展开模型与计算详情</summary>
                <Paragraph>
                  {conclusionReady
                    ? `正式映射依据：${bossView.why.join("；")}。`
                    : `${dailyObservation ? "日报研判依据" : "模型依据"}：${formatDateRangesInText(cleanBusinessText(lowConfidencePrediction?.rationale))}。${dailyObservation ? "正式门禁未通过，本方向仅供观察。" : ""}`}
                </Paragraph>
                {lowConfidencePrediction?.batch_id ? <p>预测批次：<code>{lowConfidencePrediction.batch_id}</code></p> : null}
              </details>
            ) : null}
            <div className="decision-boundary-note">
              <InfoCircleOutlined />
              <span>会推翻当前判断的信号：{bossView.reversal}</span>
              <Button size="small" type="link" onClick={() => onNavigate("reports")}>查看研判报告</Button>
            </div>
            <div className="decision-quick-links">
              <Button size="small" onClick={() => onNavigate("evidence")}>核对证据图谱</Button>
              <Button size="small" onClick={() => onNavigate("market")}>看品种行情</Button>
              <Button size="small" onClick={() => onNavigate("reports", { reportView: "ledger" })}>七品种预测与到期复盘</Button>
            </div>
          </div>
          {fullChainRows.length ? (
            <div className="full-chain-summary-list" aria-label="全链路预测摘要">
              {fullChainRows.map((item) => (
                <article key={item.id}>
                  <span>{item.label}</span>
                  <strong>{item.state}</strong>
                  <em>{item.value}</em>
                  <small>{item.detail}</small>
                </article>
              ))}
            </div>
          ) : null}
        </Panel>

        <Panel title="关键风险与核验信号" subtitle="每一项说明影响与核验信号" icon={<WarningOutlined />} className="overview-risk">
          {risks.length ? (
            <div className="risk-action-list" aria-label="关键风险与核验清单">
              {risks.map((risk, index) => (
                <article className={`risk-action-item is-${risk.tone}`} key={`${index}-${risk.title}-${risk.detail}`}>
                  <header>
                    <FieldTag tone={risk.tone}>{risk.level}风险</FieldTag>
                    <strong>{risk.title}</strong>
                    <FieldTag tone={risk.level === "高" ? "warning" : "info"}>{index < 2 ? "重点跟踪" : "持续观察"}</FieldTag>
                  </header>
                  <div className="risk-action-content">
                    <div><span>可能影响</span><p>{compactText(risk.impact, 96)}</p></div>
                    <div><span>下一步核验</span><p>{compactText(risk.action, 96)}</p></div>
                  </div>
                  {risk.detail && risk.detail !== risk.impact ? (
                    <details className="text-disclosure is-inline">
                      <summary>查看风险依据</summary>
                      <p>{risk.detail}</p>
                    </details>
                  ) : null}
                </article>
              ))}
            </div>
          ) : (
            <div className="risk-list">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有可核验的关键风险信号" />
            </div>
          )}
        </Panel>

        <div className="overview-bottom">
          <WorkbenchTabs labels={["最新事件", "证据摘要"]} className="review-overview-feed">
          <Panel title="最新事件摘要" subtitle="新闻、公告和突发事件" icon={<AlertOutlined />}>
            <div className="overview-event-list">
              {events.length ? events.slice(0, 8).map((event) => {
                // 弱标题（英文/聚合器 slug）优先用中文概览补位；没有概览时
                // 保留原英文标题（slug 去连字符），门禁说明只出现在正文位，
                // 不能冒充标题。
                const rawTitle = cleanBusinessText(event.title, "");
                const titleIsWeak = !rawTitle || isMostlyEnglishText(rawTitle)
                  || /^[a-z0-9]+(-[a-z0-9]+)+$/i.test(rawTitle);
                const slugTitle = /^[a-z0-9]+(-[a-z0-9]+)+$/i.test(rawTitle)
                  ? rawTitle.replace(/-/g, " ")
                  : rawTitle;
                const displayTitle = !titleIsWeak
                  ? rawTitle
                  : compactText(cleanBusinessText(event.overviewText, ""), 44)
                    || compactText(slugTitle, 60)
                    || "详见事件详情";
                return (
                <article key={event.id}>
                  <FieldTag tone={event.tone}>{event.category}</FieldTag>
                  <div>
                    <strong>{displayTitle.replace(/\uFFFD+/g, "")}</strong>
                    <span>{(event.overviewText || event.impact || "").replace(/\uFFFD+/g, "")}</span>
                  </div>
                  <time>{event.time}</time>
                </article>
                );
              }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={currentEventsReady
                ? "暂无新增事件"
                : live && !live.eventLibrary.ok && live.eventLibrary.pending
                  ? "正在读取当前事件"
                  : "当前事件未能读取，请使用顶部重新读取；历史快照不作为最新事件。"} />}
            </div>
            <Button type="link" className="overview-panel-link" onClick={() => onNavigate("intelligence")}>进入全球雷达</Button>
          </Panel>
          <Panel title="证据摘要" subtitle="进入图谱可查看完整路径" icon={<NodeIndexOutlined />}>
            <div className="evidence-source-rows">
              {evidenceRows.map((row) => (
                <article key={row.label}>
                  <div>
                    <strong>{row.label}</strong>
                    <span>{row.detail}</span>
                  </div>
                  <b>{formatInteger(row.count)}</b>
                </article>
              ))}
            </div>
            <Button type="link" className="overview-panel-link" onClick={() => onNavigate("evidence")}>进入证据图谱</Button>
          </Panel>
          </WorkbenchTabs>
        </div>
      </div>
    </PageFrame>
  );
}

function MarketChainModule({ live, liveLoading }: { data?: AgentWorkbenchData; live?: WorkbenchLiveData; liveLoading?: boolean }) {
  const [product, setProduct] = useState("POY");
  const [priceWindow, setPriceWindow] = useState<"30" | "90" | "all">("all");
  const [priceDetailState, setPriceDetailState] = useState<DataStatus<ForecastPricePoint[]>>({ ok: false, reason: "价格明细正在读取" });
  const [priceDetailProduct, setPriceDetailProduct] = useState("POY");
  const products = ["POY", "DTY", "PX", "PTA", "MEG", "石脑油", "原油"];
  const chainProducts = ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"];
  const localProductView = useCallback((item: string) => {
    return marketProductView(item, live);
  }, [live]);
  const productView = localProductView(product);
  const canonicalUnitFor = useCallback((item: string) => item === "原油" ? "USD/bbl" : "CNY/mt", []);
  const viewDisplayPriceFor = useCallback((item: string): LatestPriceItem | undefined => {
    const view = localProductView(item);
    const display = view?.latest_display_price;
    const observation = view?.intraday_observation;
    if (display?.status === "available" && display.value != null && (display.observed_at || display.date)) {
      const observedAt = display.observed_at || display.date!;
      const observedRaw = observation?.raw;
      const observedConversion = observedRaw && typeof observedRaw.currency_conversion === "object"
        ? observedRaw.currency_conversion as Record<string, unknown>
        : undefined;
      return {
        instrument: productKey(item),
        label: item,
        latest: {
          observation_id: display.observation_id || `market-display-${productKey(item)}-${observedAt}`,
          created_at: observedAt,
          instrument: productKey(item),
          symbol: "",
          observed_at: observedAt,
          interval_seconds: 0,
          price_type: display.price_type || "daily_reference",
          last: display.value,
          open: null,
          high: null,
          low: null,
          volume: null,
          change_pct: null,
          unit: display.unit || canonicalUnitFor(item),
          source_id: display.source_id || "unknown",
          source_url: display.source_url || "",
          quality: display.quality || display.quality_label || "audited_display",
          notes: display.detail,
          raw: {
            original_value: display.original_value ?? observedConversion?.original_value,
            original_unit: display.original_unit ?? observedConversion?.original_unit,
            fx_rate: display.fx_rate ?? observedConversion?.fx_rate,
            fx_date: display.fx_date ?? observedConversion?.fx_date,
            fx_source_id: display.fx_source_id ?? observedConversion?.fx_source_id,
            fx_lag_days: display.fx_lag_days ?? observedConversion?.fx_lag_days,
            conversion_status: display.conversion_status ?? observedConversion?.conversion_status,
            comparison_basis: display.comparison_basis,
            source_kind: display.source_kind,
            evidence_tier: display.evidence_tier,
            formal_eligible: display.formal_eligible,
            usage_limits: display.usage_limits,
            quote_type: display.quote_type
          }
        },
        freshness: "valuation",
        freshness_label: display.quality_label || "最新展示价",
        quote_type_label: display.original_unit ? "汇率折算展示价" : "最新展示价",
        is_transaction_price: display.price_type === "exchange_proxy",
        gap_reason: ""
      };
    }
    if (observation?.last != null && observation.observed_at) {
      return {
        instrument: observation.instrument,
        label: item,
        latest: observation,
        freshness: observation.price_type === "spot_public_valuation" ? "valuation" : "near_realtime",
        freshness_label: observation.price_type === "spot_public_valuation" ? "公开现货评估" : "最新观测",
        quote_type_label: observation.price_type === "exchange_proxy" ? "交易所代理行情" : "最新展示价",
        is_transaction_price: observation.price_type === "exchange_proxy",
        gap_reason: ""
      };
    }
    return undefined;
  }, [canonicalUnitFor, localProductView]);
  const currentPriceFor = useCallback((item: string) => {
    const viewDisplay = viewDisplayPriceFor(item);
    const candidates = live?.latestPrices.ok ? live.latestPrices.value.items : [];
    const liveDisplay = preferredLatestPriceForProduct(item, candidates, canonicalUnitFor(item));
    const resolved = !viewDisplay?.latest
      ? liveDisplay
      : !liveDisplay?.latest
        ? viewDisplay
        : Date.parse(liveDisplay.latest.observed_at) > Date.parse(viewDisplay.latest.observed_at)
          ? liveDisplay
          : viewDisplay;
    const previous = lastDisplayedMarketPriceByProduct.get(item);
    if (!resolved?.latest) return previous;
    if (previous?.latest && Date.parse(previous.latest.observed_at) > Date.parse(resolved.latest.observed_at)) {
      return previous;
    }
    lastDisplayedMarketPriceByProduct.set(item, resolved);
    return resolved;
  }, [canonicalUnitFor, live, viewDisplayPriceFor]);
  const displayPriceFor = useCallback((item: string) => {
    const latest = currentPriceFor(item);
    if (!latest?.latest || item === "原油" || latest.latest.unit !== "USD/mt") return latest;
    const observedDate = latest.latest.observed_at.slice(0, 10);
    const converted = localProductView(item)?.price_series.find((point) => (
      point.date.slice(0, 10) === observedDate
      && point.unit === "CNY/mt"
      && point.original_unit === "USD/mt"
      && point.original_value != null
      && Math.abs(point.original_value - latest.latest!.last!) < 0.01
      && Boolean(point.fx_rate)
    ));
    if (!converted) return latest;
    return {
      ...latest,
      latest: {
        ...latest.latest,
        last: Number((latest.latest.last! * (converted.fx_rate ?? 0)).toFixed(2)),
        unit: converted.unit,
        raw: {
          ...latest.latest.raw,
          original_value: latest.latest.last,
          original_unit: latest.latest.unit,
          fx_rate: converted.fx_rate,
          fx_date: converted.fx_date,
          fx_source_id: converted.fx_source_id,
          conversion_status: converted.conversion_status
        }
      },
      quote_type_label: `${latest.quote_type_label} · 汇率折算价`
    };
  }, [currentPriceFor, localProductView]);
  const priceItem = displayPriceFor(product);
  const latestObservationId = priceItem?.latest?.observation_id;
  const isRealtimePrice = Boolean(latestObservationId && !snapshotSeededLiveKeys.has("latestPrices"));
  const latestSourceLabel = isRealtimePrice ? (priceItem?.quote_type_label || "实时行情") : "日报快照";
  const hasCurrentPrice = Boolean(priceItem?.latest?.last != null && priceItem.latest.observed_at);
  const hasDisplayedPrice = hasCurrentPrice || productView?.latest_price.status === "available";
  const displayFreshness = productView?.latest_display_freshness;
  const priceIsStale = hasCurrentPrice
    ? displayFreshness && priceItem?.latest?.observed_at === productView?.latest_display_price?.date
      ? displayFreshness.status === "stale" || displayFreshness.status === "missing"
      : isLatestPriceStale(priceItem)
    : productView?.data_freshness.categories?.price?.status !== "fresh";
  const historicalPricePoints = marketTrendPointsFromView(product, productView, live, "all").length;
  const currentPriceSummary: MarketChainProductView["spread_summary"] | undefined = hasCurrentPrice
    ? {
        status: "available",
        metric_label: `${product} 最新价格`,
        quality_label: priceItem?.freshness_label ?? "已返回",
        tag: "已返回",
        tone: "info",
        date: priceItem?.latest?.observed_at,
        value: priceItem!.latest!.last!,
        unit: priceItem?.latest?.unit,
        points: Math.max(1, historicalPricePoints),
        detail: historicalPricePoints > 1
          ? `${product} 最新价格为 ${latestPriceLabel(priceItem)}，时间 ${latestPriceDate(priceItem)}；趋势图同时展示 ${formatInteger(historicalPricePoints)} 个观测日的同口径历史序列。`
          : `${product} 最新价格为 ${latestPriceLabel(priceItem)}，时间 ${latestPriceDate(priceItem)}。当前只有单点观测，仅展示现价，不据此计算趋势。`
      }
    : productView?.spread_summary;
  const summaryItems = [
    {
      title: "价格",
      summary: currentPriceSummary,
      fallbackTag: "未返回",
      fallbackTone: "muted" as Tone,
      fallbackBody: "当前品种价格或价差观测尚未返回。",
      sourceLabel: latestSourceLabel,
      scopeLabel: "当前品种",
      scopeNote: `${product} 最新展示价`,
      asOf: priceItem?.latest?.observed_at ?? productView?.latest_price.date,
      softRemoved: false,
      stale: priceIsStale
    },
    {
      title: "加工差",
      summary: productView?.profit_summary,
      fallbackTag: "未返回",
      fallbackTone: "warning" as Tone,
      fallbackBody: "系统暂未返回加工差参考序列；加工差不等同于净利润。",
      sourceLabel: "日报快照",
      scopeLabel: product === "POY" || product === "DTY" ? "当前品种加工差" : "下游聚酯链",
      scopeNote: product === "POY" || product === "DTY"
        ? `${product} 加工差指标（非净利润）`
        : `聚酯链加工差参考，不代表 ${product} 自身利润`,
      asOf: productView?.profit_summary.date,
      softRemoved: false,
      stale: productView?.data_freshness.categories?.profit?.status !== "fresh"
    }
  ];
  const staleMetricCount = summaryItems.filter((item) => item.stale).length;
  // The high-frequency observation is the latest observation layer. The
  // audited daily chain remains the fallback and the source of trend history.
  const latestPriceText = hasCurrentPrice
    ? latestPriceLabel(priceItem)
    : productView?.latest_price.status === "available"
      ? formatPrice(productView.latest_price.value, productView.latest_price.unit)
      : "未返回";
  const latestDateText = hasCurrentPrice
    ? latestPriceDate(priceItem)
    : productView?.latest_price.date
      ? formatDate(productView.latest_price.date)
      : "暂无时间";
  const observationHint = priceIsStale
    ? "等待更新后的价格链信号"
    : productView?.spread_summary.status === "available"
    ? productView.spread_summary.tag
    : "等待价格链信号";
  const confidenceText = marketFreshnessSummary(productView, priceIsStale);
  const explainText = productView?.latest_price.detail
      ? `${cleanBusinessText(productView.latest_price.detail)}。完整判断仍需结合上游成本与加工差变化。`
      : "需要结合上游成本与聚酯供需判断传导强弱。若价格序列或行业指标覆盖不足，系统会降低当前结论置信度。";
  const chainNodes = chainProducts.map((item) => {
    const view = localProductView(item);
    const latest = displayPriceFor(item);
    const hasLatest = Boolean(latest?.latest?.last != null && latest.latest.observed_at);
    const latestText = hasLatest
      ? latestPriceLabel(latest)
      : view?.latest_price.status === "available"
        ? formatPrice(view.latest_price.value, view.latest_price.unit)
        : "未返回";
    const latestDisplayFreshness = view?.latest_display_freshness;
    const latestIsStale = latestDisplayFreshness && latest?.latest?.observed_at === view?.latest_display_price?.date
      ? latestDisplayFreshness.status === "stale" || latestDisplayFreshness.status === "missing"
      : isLatestPriceStale(latest);
    const deltaText = hasLatest
      ? `${latestPriceDate(latest)}${latestIsStale ? " · 数据滞后" : ""}`
      : view?.latest_price.date
        ? `${formatDate(view.latest_price.date)}${view.data_freshness.categories?.price?.status !== "fresh" ? " · 数据滞后" : ""}`
        : "暂无时间";
    const tone: Tone = hasLatest
      ? (latestIsStale ? "warning" : "info")
      : view?.latest_price.status === "available" && view.data_freshness.categories?.price?.status === "fresh"
        ? "info"
        : "warning";
    return { item, latestText, deltaText, tone };
  });
  const priceProductKey = product === "石脑油" ? "NAPHTHA" : product === "原油" ? "Brent" : product;
  const selectProduct = useCallback((nextProduct: string) => {
    setPriceDetailProduct("");
    setPriceDetailState({ ok: false, reason: "价格明细正在读取" });
    setProduct(nextProduct);
  }, []);
  useEffect(() => {
    let cancelled = false;
    setPriceDetailState({ ok: false, reason: "价格明细正在读取" });
    api.upstreamPriceObservations({ product: priceProductKey, limit: 160 })
      .then((items) => {
        if (!cancelled) {
          setPriceDetailProduct(priceProductKey);
          setPriceDetailState({ ok: true, value: items });
        }
      })
      .catch((error) => {
        if (!cancelled) setPriceDetailState({ ok: false, reason: error instanceof Error ? error.message : "价格明细暂未返回" });
      });
    return () => {
      cancelled = true;
    };
  }, [priceProductKey, latestObservationId]);
  const priceDetailRows = priceDetailState.ok && priceDetailProduct === priceProductKey ? priceDetailState.value : [];
  const auditedTrendPoints = marketTrendPointsFromView(product, productView, live, "all");
  const fallbackTrendPoints = auditedTrendPoints.length
    ? auditedTrendPoints
    : trendPointsFromPriceDetails(priceDetailRows, "all");
  const curveCutoff = fallbackTrendPoints.map(point => point.date).sort().reverse()[0];
  const canonicalTrendUnit = (fallbackTrendPoints[0] && "unit" in fallbackTrendPoints[0] ? String(fallbackTrendPoints[0].unit) : undefined) || productView?.latest_price.unit || canonicalUnitFor(product);
  const canonicalSeries = productView?.price_series.filter((point) => point.trend_eligible !== false) ?? [];
  const hasFxConversion = canonicalSeries.some((point) => point.original_unit === "USD/mt");
  const latestConvertedPoint = [...canonicalSeries]
    .reverse()
    .find((point) => point.original_unit === "USD/mt" && point.fx_date);
  const canonicalBasis = auditedTrendPoints[0] && "comparisonBasis" in auditedTrendPoints[0]
    ? auditedTrendPoints[0].comparisonBasis as MarketChainProductView["price_series"][number]["comparison_basis"]
    : undefined;
  const latestRaw = priceItem?.latest?.raw;
  const latestBasis = latestRaw && typeof latestRaw.comparison_basis === "object"
    ? latestRaw.comparison_basis as MarketChainProductView["price_series"][number]["comparison_basis"]
    : productView?.latest_display_price?.comparison_basis;
  const seriesBasisMismatch = Boolean(hasCurrentPrice && (
    readableUnit(priceItem?.latest?.unit) !== readableUnit(canonicalTrendUnit)
    || (canonicalBasis && (!latestBasis
    || [canonicalBasis.product, canonicalBasis.market, canonicalBasis.spec, canonicalBasis.quote_type, canonicalBasis.source_basis]
      .map((value) => String(value ?? "").trim().toUpperCase()).join("|")
      !== [latestBasis.product, latestBasis.market, latestBasis.spec, latestBasis.quote_type, latestBasis.source_basis]
        .map((value) => String(value ?? "").trim().toUpperCase()).join("|")
  ))));
  const latestBasisLabel = priceItem?.latest?.price_type === "exchange_proxy"
    ? "期货代理价" : priceItem?.latest?.price_type === "spot_public_valuation"
      ? "现货评估价" : marketSeriesLabel(latestBasis) === "历史报价"
        ? "公开参考价" : marketSeriesLabel(latestBasis);
  const trendBasisLabel = marketSeriesLabel(canonicalBasis);
  const trendSourceLabel = priceSourceLabel(canonicalBasis?.source_basis);
  const currentTrendPoint = hasCurrentPrice
    && !priceIsStale
    && priceItem?.latest
    && (!canonicalTrendUnit || priceItem.latest.unit === canonicalTrendUnit)
    && !seriesBasisMismatch
    ? {
        date: priceItem.latest.observed_at.slice(0, 10),
        value: priceItem.latest.last!
      }
    : undefined;
  const mergedTrendPoints = currentTrendPoint
    ? [
        ...fallbackTrendPoints.filter((point) => point.date !== currentTrendPoint.date),
        currentTrendPoint
      ]
    : fallbackTrendPoints;
  const trendPoints = calendarWindow(mergedTrendPoints, priceWindow);
  const historicalTrendLastDate = fallbackTrendPoints[fallbackTrendPoints.length - 1]?.date;
  const windowEnd = trendPoints[trendPoints.length - 1]?.date;
  const requestedStart = windowEnd && priceWindow !== "all"
    ? marketDay(windowEnd) - Number(priceWindow) * MARKET_DAY_MS : null;
  const shortHistory = requestedStart != null && fallbackTrendPoints.length > 0
    && marketDay(fallbackTrendPoints[0].date) > requestedStart;
  const displayedCoverageText = `${trendSourceLabel} · ${formatDisplayDate(trendPoints[0]?.date)} 至 ${formatDisplayDate(trendPoints[trendPoints.length - 1]?.date)}`;
  const displayedObservationHint = seriesBasisMismatch
    ? "不同报价基准分开观察"
    : observationHint;
  const displayedCurrentPriceSummary = currentPriceSummary && seriesBasisMismatch
    ? {
        ...currentPriceSummary,
        detail: `${product} 最新${latestBasisLabel}为 ${latestPriceLabel(priceItem)}（报价来源：${priceSourceLabel(priceItem?.latest?.source_id, priceItem?.latest?.source_url)}），时间 ${latestPriceDate(priceItem)}；${trendBasisLabel}曲线来源：${trendSourceLabel}，截至 ${formatDisplayDate(historicalTrendLastDate)}，单位为 ${readableUnit(canonicalTrendUnit)}。报价基准不同，分别保留原始单位和日期，不直接拼接或计算涨跌。`
      }
    : currentPriceSummary;
  const displayedSummaryItems = summaryItems.filter(item => !item.softRemoved).map((item) => (
    item.title === "价格" ? { ...item, summary: displayedCurrentPriceSummary } : item
  ));
  const marketDataPending = Boolean((liveLoading && !live) || (!productView && !priceDetailState.ok));
  const priceDetailGroups = Array.from(new Map(
    [...priceDetailRows]
      .sort((left, right) => Date.parse(left.observed_at || left.created_at) - Date.parse(right.observed_at || right.created_at))
      .map((row) => [`${row.series}-${row.spec}-${row.company}`, row])
  ).values()).slice(0, 8);
  const latestDetail = priceItem?.latest?.last != null && priceItem.latest.observed_at ? priceItem.latest : undefined;
  const priceDetailCount = priceDetailGroups.length + (latestDetail ? 1 : 0);

  return (
    <PageFrame
      title="行情与原料链"
      eyebrow="价格与成本传导"
      description="用业务链路说明价格传导与加工差状态。"
      className="market-page"

    >
      <div className="market-layout">
        <Panel title="上游原料链" subtitle="从能源端到聚酯长丝" icon={<PartitionOutlined />} className="market-chain-panel">
          <div className="review-material-flow" aria-label="原油经石脑油、PX制成PTA；PTA与MEG聚合纺丝生成POY，再加弹生成DTY">
            {[["原油"], ["石脑油"], ["PX"], ["PTA", "MEG"], ["POY"], ["DTY"]].map((group, index) => (
              <div className={`review-material-stage ${group.length > 1 ? "is-pair" : ""} ${index === 3 || index === 4 ? "has-process" : ""}`} key={group.join("+")}>
                <div className="review-material-products">
                  {group.map((name) => {
                    const entry = chainNodes.find(({ item }) => item === name);
                    if (!entry) return null;
                    return <button key={name} type="button" aria-pressed={product === name}
                      className={`review-material-price ${product === name ? "is-active" : ""}`}
                      onClick={() => selectProduct(name)}>
                      <span className="review-material-name"><ProductSymbol product={name} /><b>{name}</b></span>
                      <strong>{entry.latestText}</strong>
                      <small>{cleanBusinessText(entry.deltaText)}</small>
                    </button>;
                  })}
                </div>
                {index < 5 ? <span className="review-material-link" aria-hidden="true">{index === 3 || index === 4 ? <small>{index === 3 ? "聚合纺丝" : "加弹"}</small> : null}<i className="review-transfer-arrow" /></span> : null}
              </div>
            ))}
          </div>
        </Panel>

        <Panel
          title={`${product} 价格趋势（${readableUnit(canonicalTrendUnit)}）${curveCutoff ? ` · 截止 ${curveCutoff}` : ""}`}
          subtitle={`${hasFxConversion ? "美元历史报价按逐日汇率折算为元/吨" : "日度价格趋势与数据边界"}${curveCutoff ? ` · 曲线截止 ${curveCutoff}` : ""}`}
          icon={<BarChartOutlined />}
          className="trend-panel"
          extra={(
        <>
          <Segmented options={products} value={product} onChange={(value) => selectProduct(String(value))} />
          <Segmented
            options={[
              { label: "末30日数据", value: "30" },
              { label: "末90日数据", value: "90" },
              { label: "全部", value: "all" }
            ]}
            value={priceWindow}
            onChange={(value) => setPriceWindow(value as "30" | "90" | "all")}
          />
        </>
      )}
        >
          <div
            className="chart-shell"
            role="img"
            aria-label={`${product} 日度价格趋势，单位${readableUnit(canonicalTrendUnit)}，${formatDisplayDate(trendPoints[0]?.date)} 至 ${formatDisplayDate(trendPoints[trendPoints.length - 1]?.date)}，共 ${trendPoints.length} 个观测日`}
          >
            {trendPoints.length ? (
              <Suspense fallback={<div className="chart-loading">趋势图加载中…</div>}>
                <MarketTrendChart
                  data={trendPoints}
                  window={priceWindow}
                  unitLabel={readableUnit(canonicalTrendUnit)}
                  currentDate={currentTrendPoint?.date}
                  formatPrice={formatPrice}
                  formatDisplayDate={formatDisplayDate}
                  firstDate={trendPoints[0]?.date}
                  lastDate={trendPoints[trendPoints.length - 1]?.date}
                  pointCount={trendPoints.length}
                />
              </Suspense>
            ) : null}
            {!trendPoints.length ? (
              <div className="chart-empty">
                <BarChartOutlined />
                <strong>{marketDataPending ? "价格序列正在读取" : "系统未返回价格序列"}</strong>
                <span>{marketDataPending ? "正在读取真实行情与链路指标。" : "当前品种暂时没有可绘制的日度趋势，已作为数据边界提示。"}</span>
              </div>
            ) : null}
          </div>
          <div className="price-snapshot-grid">
            <article>
              <ClockCircleOutlined />
              <span>最新价格</span>
              <strong>{latestPriceText}</strong>
              <small>{latestBasisLabel} · {priceSourceLabel(priceItem?.latest?.source_id, priceItem?.latest?.source_url)} · {latestDateText}{priceIsStale ? " · 数据滞后" : ""}</small>
            </article>
            <article>
              <SafetyCertificateOutlined />
              <span>曲线 · {trendBasisLabel}{productView?.data_freshness.categories?.price?.status === "stale" ? " · 历史序列滞后" : ""}</span>
              <strong>{!trendPoints.length ? "所选区间无历史点" : shortHistory ? `历史不足${priceWindow}日 · ${trendPoints.length}个观测日` : `${trendPoints.length}个观测日`}</strong>
              <small>{displayedCoverageText}</small>
              <small>相邻观测日直接连线；休市及未发布日不画点</small>
            </article>
            <article>
              <RiseOutlined />
              <span>观察信号</span>
              <strong>{cleanBusinessText(displayedObservationHint)}</strong>
              <small>{confidenceText}</small>
            </article>
          </div>
        </Panel>

        <WorkbenchTabs labels={["品种解读", "近期报价", "价格与加工差"]} className="review-market-side">
        <Panel title="当前品种解释" subtitle="业务化说明" icon={<FileTextOutlined />} className="product-explain">
          <div className="product-explain-heading">
            <div>
              <span>当前品种</span>
              <Title level={3}>{product} 查看重点</Title>
              <EvidenceDossierButton key={product} label="品种正反证" initialTarget={evidenceTarget(product)} />
            </div>
            <FieldTag tone={hasDisplayedPrice && !priceIsStale ? "success" : "warning"}>
              {!hasDisplayedPrice ? "价格待更新" : priceIsStale ? "价格数据滞后" : "价格已更新"}
            </FieldTag>
          </div>
          <div className="product-freshness-row" aria-label={`${product} 数据更新时间`}>
            {summaryItems.filter(item => !item.softRemoved).map((item) => {
              return (
                <div className={item.stale ? "is-stale" : ""} key={`freshness-${item.title}`}>
                  <span>{item.title}</span>
                  <strong>{formatDisplayDate(item.asOf)}</strong>
                  {item.stale ? <FieldTag tone="warning">数据滞后</FieldTag> : null}
                </div>
              );
            })}
          </div>
          <div className="explain-list">
            <article><strong>成本观察</strong><span>{compactText(displayedCurrentPriceSummary?.detail, 100) || "等待该品种价格与价差观测。"}</span></article>
            <article><strong>置信边界</strong><span>{staleMetricCount ? `${staleMetricCount} 项数据滞后` : cleanBusinessText(confidenceText)}。重点核验成本传导是否被需求或库存抵消。</span></article>
          </div>
          <details className="text-disclosure product-explain-detail">
            <summary>展开完整品种说明</summary>
            {displayedCurrentPriceSummary?.detail ? <Paragraph>{displayedCurrentPriceSummary.detail}</Paragraph> : null}
            <Paragraph>{explainText}</Paragraph>
          </details>
          <div className="price-detail-list" data-testid="price-detail-list">
            <div className="price-detail-head">
              <strong>多规格价格明细</strong>
              <FieldTag tone={priceDetailState.ok && priceDetailCount ? "success" : "warning"}>
                {priceDetailState.ok ? `${formatInteger(priceDetailCount)} 项报价记录` : "读取中"}
              </FieldTag>
            </div>
            {priceDetailState.ok && priceDetailCount ? (
              <>
                {latestDetail ? <article key={`latest-${product}-${latestDetail.observation_id}`}>
                  <div>
                    <strong>{product} 最新行情</strong>
                    <span>{priceItem?.quote_type_label ?? "最新行情"} · {formatDate(latestDetail.observed_at)}</span>
                  </div>
                  <b>{formatPrice(latestDetail.last, latestDetail.unit)}</b>
                </article> : null}
                {priceDetailGroups.map((row) => (
                  <article key={row.point_id}>
                    <div>
                      <strong>{cleanBusinessText(row.spec || row.series || row.product)}</strong>
                      <span>{cleanBusinessText(row.series || row.product)} · {formatDate(row.observed_at)}{Date.now() - Date.parse(row.observed_at) > 14 * 86400000 ? " · 历史报价" : ""}</span>
                    </div>
                    <b>{formatPrice(row.price, row.unit)}</b>
                  </article>
                ))}
              </>
            ) : (
              <div className="detail-empty">
                <span>{priceDetailState.ok ? "当前品种未返回最新价格明细" : cleanBusinessText(priceDetailState.reason)}</span>
              </div>
            )}
          </div>
        </Panel>

        <PublicQuoteHistory instrument={priceProductKey} refreshKey={latestObservationId} />
        <div className="market-summary-grid">
          {displayedSummaryItems.map((item) => (
            <Panel key={item.title} title={item.title} subtitle={item.stale ? "历史参考" : "当前观测"} className="market-small-panel">
              <article className="summary-card">
                <div className="summary-card-head">
                  <div>
                    <strong>{cleanBusinessText(item.summary?.metric_label || item.title)}</strong>
                    <small className="summary-card-scope">{item.scopeLabel}</small>
                  </div>
                  <FieldTag tone={item.softRemoved ? "info" : item.stale ? "warning" : item.summary ? marketSummaryTone(item.summary.tone) : item.fallbackTone}>
                    {item.stale ? "数据滞后" : item.summary ? "已更新" : cleanBusinessText(item.fallbackTag)}
                  </FieldTag>
                </div>
                {item.summary?.value != null ? (
                  <b className="summary-card-value">{formatPrice(item.summary.value, item.summary.unit)}</b>
                ) : null}
                <span>{item.stale
                  ? historicalMarketSummaryBody(item.title, item.asOf, item.summary)
                  : item.summary
                    ? compactText(marketSummaryBody(item.summary), 92)
                    : cleanBusinessText(item.fallbackBody)}</span>
                {item.title !== "价格" ? <em className="summary-card-ownership">{item.scopeNote}</em> : null}
                <small>{item.sourceLabel} · {item.asOf ? `截至 ${formatDate(item.asOf)}` : "暂无截至时间"}</small>
              </article>
            </Panel>
          ))}
        </div>
        </WorkbenchTabs>
      </div>
    </PageFrame>
  );
}


function normalizeTone(value?: string): Tone {
  return value === "success" || value === "warning" || value === "danger" || value === "info" || value === "muted" || value === "normal"
    ? value
    : "info";
}

function EvidenceGraphModule({ data, live }: {
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  liveLoading?: boolean;
  onReload: () => void;
}) {
  // 保留 PageFrame（evidence-page 容器）：固定视口外壳依赖它提供页面主体的内部滚动。
  return (
    <PageFrame title="证据图谱" eyebrow="证据链路" className="evidence-page" hideHeader>
      <EvidenceVerificationBoard data={data} ragVisual={live?.ragVisual.ok ? live.ragVisual.value : undefined} />
    </PageFrame>
  );
}

// ---------------------------------------------------------------------------
// Agent 系统（批次3重建）：11 节点混合管线图 + 右侧详情抽屉。
// 节点身份与状态全部来自 GET /api/v1/pipeline/graph（服务端只读聚合）；
// 抽屉四块与 Agent 两块来自 GET /api/v1/pipeline/nodes/{id} 懒加载。
// 旧 12 角色静态视图已退役（批次6）：/agent-runs* 后端数据保留只读。
// ---------------------------------------------------------------------------

const pipelineStatusLabels: Record<PipelineNodeStatus, string> = {
  ok: "成功",
  degraded: "降级",
  waiting: "外部等待",
  idle: "空闲"
};

const pipelineStatusTone: Record<PipelineNodeStatus, Tone> = {
  ok: "success",
  degraded: "warning",
  waiting: "info",
  idle: "muted"
};

// 仅展示坐标：首行六卡、主线八卡、末行复盘三卡 + 记忆舱 + 链外助手。
// 行首尾对齐；不同分区卡宽匹配原型，保持图坐标与泳道包围盒一致。
const pipelineNodeLayout: Record<string, { x: number; y: number }> = {
  collect: { x: 0, y: 0 }, clean: { x: 400, y: 0 }, index: { x: 800, y: 0 },
  event_summary: { x: 1200, y: 0 }, event_overview: { x: 1600, y: 0 }, factor_score: { x: 2000, y: 0 },
  event_signal: { x: 0, y: pipelineRowGeometry.prediction.y }, political_analysis: { x: 300, y: pipelineRowGeometry.prediction.y }, historical_analog: { x: 600, y: pipelineRowGeometry.prediction.y }, product_synthesis: { x: 900, y: pipelineRowGeometry.prediction.y },
  skeptic_review: { x: 1200, y: pipelineRowGeometry.prediction.y }, event_fusion: { x: 1500, y: pipelineRowGeometry.prediction.y }, seven_product: { x: 1800, y: pipelineRowGeometry.prediction.y }, shadow_eval: { x: 2100, y: pipelineRowGeometry.prediction.y },
  counter_scan: { x: 0, y: pipelineRowGeometry.review.y }, daily_interpretation: { x: 400, y: pipelineRowGeometry.review.y }, report_assembly: { x: 800, y: pipelineRowGeometry.review.y },
  unified_memory: { x: 1250, y: pipelineRowGeometry.review.y }, assistant: { x: 2000, y: pipelineRowGeometry.review.y },
};
function pipelineCardWidth(nodeId: string): number {
  return nodeId === "unified_memory" ? 664 : pipelineLane(nodeId) === "prediction" ? 264 : 364;
}

// 泳道分组带（对照原型图左上角分组芯片）：纯视觉分组层，不改变节点身份与
// palette 配色；预测泳道用 prediction 蓝 #0ea5e9 强调。keywords = 分区头部的
// 技术关键词章（解剖画布：让 RAG/多Agent/通信/上下文/记忆/门禁 3 秒可寻）。
// 记忆泳道是 ADR-10 资源观测（统一记忆舱），不新增后端节点。
const pipelineLaneGroups: { id: string; label: string; color: string; emphasis?: boolean; nodeIds: string[]; keywords: string[] }[] = [
  { id: "data", label: "数据 · 采集与治理", color: "#16a34a", nodeIds: ["collect", "clean", "index"], keywords: ["RAG 检索增强", "上下文管理"] },
  { id: "event", label: "事件理解 · 结构化与事件", color: "#7c3aed", nodeIds: ["event_summary", "event_overview", "factor_score"], keywords: ["多 Agent 接力"] },
  {
    id: "prediction", label: "预测推演 · 单一主线", color: "#2563eb", emphasis: true,
    nodeIds: ["event_signal", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion", "seven_product", "shadow_eval"],
    keywords: ["多 Agent 接力", "Agent 通信", "上下文管理", "融合与门禁"]
  },
  { id: "review", label: "复盘 · 校验与沉淀", color: "#7c3aed", nodeIds: ["counter_scan", "daily_interpretation", "report_assembly"], keywords: ["融合与门禁"] },
  { id: "memory", label: "记忆 · 召回与互证（ADR-10）", color: "#d97706", nodeIds: ["unified_memory"], keywords: ["记忆模块", "RAG 检索增强"] },
  { id: "assistant", label: "链外 · 随时可问", color: "#64748b", nodeIds: ["assistant"], keywords: [] }
];

// 十六进制 → rgba 字符串：泳道带底/边框用同色低透明度，浅色主题下保持安静。
function pipelineLaneRgba(hex: string, alpha: number): string {
  const value = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

function pipelineLaneBandStyle(group: (typeof pipelineLaneGroups)[number], compact: boolean): CSSProperties {
  const scale = compact ? 0.7 : 1;
  const points = group.nodeIds.map((nodeId) => pipelineNodeLayout[nodeId]).filter(Boolean);
  if (!points.length) return { display: "none" };
  const minX = Math.min(...points.map((point) => point.x));
  const minY = Math.min(...points.map((point) => point.y));
  const maxX = Math.max(...group.nodeIds.map((id) => pipelineNodeLayout[id].x + pipelineCardWidth(id)));
  const maxY = Math.max(...group.nodeIds.map((id) => pipelineNodeLayout[id].y + pipelineCardGeometry(id).height));
  return {
    left: minX * scale - 10,
    top: minY - pipelineLaneGeometry.header,
    width: (maxX - minX) * scale + 20,
    height: maxY - minY + pipelineLaneGeometry.header + pipelineLaneGeometry.footer,
    background: pipelineLaneRgba(group.color, group.emphasis ? 0.07 : 0.045),
    borderColor: pipelineLaneRgba(group.color, group.id === "memory" ? 0.65 : 0.22)
  } as CSSProperties;
}

// API 不可用时的诚实降级骨架：拓扑固定（CONSENSUS 定死不改），状态明确标为未知，
// 绝不把未知推断为成功。
const pipelineSkeletonNodes: PipelineGraphNode[] = [
  { id: "collect", kind: "code", name: "采集", status: "waiting", status_detail: "", timestamp: "", edge_group: "data" },
  { id: "clean", kind: "code", name: "清洗去重门禁", status: "waiting", status_detail: "", timestamp: "", edge_group: "data" },
  { id: "index", kind: "code", name: "语义索引", status: "waiting", status_detail: "", timestamp: "", edge_group: "data" },
  { id: "event_summary", kind: "agent", name: "事件摘要", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "event_overview", kind: "agent", name: "事件总览", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "factor_score", kind: "code", name: "因子打分", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "event_signal", kind: "code", name: "当日事件精选", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "political_analysis", kind: "agent", name: "政局解读", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "historical_analog", kind: "agent", name: "历史经验", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "product_synthesis", kind: "agent", name: "品种研判", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "skeptic_review", kind: "agent", name: "交叉质证", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "event_fusion", kind: "code", name: "预测定案", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "seven_product", kind: "code", name: "七产品预测", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "shadow_eval", kind: "code", name: "复盘校准", status: "waiting", status_detail: "", timestamp: "", edge_group: "prediction" },
  { id: "counter_scan", kind: "agent", name: "反证扫描", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "daily_interpretation", kind: "agent", name: "日报解读", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "report_assembly", kind: "code", name: "研报组装", status: "waiting", status_detail: "", timestamp: "", edge_group: "judgement" },
  { id: "assistant", kind: "agent", name: "研判助手", status: "waiting", status_detail: "", timestamp: "", edge_group: "assistant" }
];

// 主链兜进边（降级骨架使用；正常路径以 API 返回的 flow 边为准）。
const pipelineSkeletonFlowEdges: PipelineGraphEdge[] = [
  { from: "collect", to: "clean", kind: "flow" },
  { from: "clean", to: "index", kind: "flow" },
  { from: "index", to: "event_summary", kind: "flow" },
  { from: "event_summary", to: "event_overview", kind: "flow" },
  { from: "event_overview", to: "factor_score", kind: "flow" },
  { from: "factor_score", to: "event_signal", kind: "flow" },
  { from: "event_signal", to: "political_analysis", kind: "flow" },
  { from: "political_analysis", to: "historical_analog", kind: "flow" },
  { from: "historical_analog", to: "product_synthesis", kind: "flow" },
  { from: "product_synthesis", to: "skeptic_review", kind: "flow" },
  { from: "skeptic_review", to: "event_fusion", kind: "flow" },
  { from: "event_fusion", to: "seven_product", kind: "flow" },
  { from: "seven_product", to: "shadow_eval", kind: "flow" },
  { from: "shadow_eval", to: "counter_scan", kind: "flow" },
  { from: "counter_scan", to: "daily_interpretation", kind: "flow" },
  { from: "daily_interpretation", to: "report_assembly", kind: "flow" }
];

// 交互入口（虚线）=「随时可问」语义（2026-09-16 共识，OPEN-1 已收口）：
// 研判助手可基于事件摘要与日报解读随时提问。API /pipeline/graph 的
// dashed 边已与本常量对齐（事件摘要⇢研判助手、日报解读⇢研判助手）。
// 交互入口边可携带专属手柄，让虚线沿列间隙走廊走、不穿卡片：
// 事件摘要⇢助手走右侧竖走廊进助手顶；质疑检验从上方走廊进助手顶。
const pipelineInteractionEdges: { from: string; to: string; label: string; fromHandle?: string; toHandle?: string }[] = [
  { from: "event_summary", to: "assistant", label: "事件与摘要", fromHandle: "right", toHandle: "top" },
  { from: "daily_interpretation", to: "assistant", label: "解读叙述" },
  { from: "skeptic_review", to: "assistant", label: "质疑追问", fromHandle: "bottom", toHandle: "top" }
];

// 经验回灌（ADR-9 学习闭环，蓝色虚线）：复盘校准把结算样本周蒸馏为教训，
// 回灌到政局解读与历史经验的下一轮推理；API /pipeline/graph 的 feedback 边
// 已与本常量对齐（复盘校准⇢政局解读、复盘校准⇢历史经验）。
const pipelineFeedbackEdges: { from: string; to: string; label: string }[] = [
  { from: "shadow_eval", to: "political_analysis", label: "经验回灌" },
  { from: "shadow_eval", to: "historical_analog", label: "经验回灌" }
];

// 连线流转物小字（固定拓扑的流转语义，非实时数值）。
const pipelineFlowEdgeLabels: Record<string, string> = {
  "collect-clean": "原始文章与价格",
  "clean-index": "已筛选事件",
  "index-event_summary": "全文与索引",
  "event_summary-event_overview": "事件标题",
  "event_overview-factor_score": "事件与价格系列",
  "factor_score-seven_product": "因子分",
  "seven_product-counter_scan": "21 格判断",
  "counter_scan-daily_interpretation": "反证发现",
  "daily_interpretation-report_assembly": "四节叙述"
};

// 展示词典：抽屉输入/输出摘要的键必须全部映射为中文业务口径；未收录的键
// 不渲染（页面契约禁止暴露内部 snake_case/camelCase 字段名）。
const pipelineSummaryLabels: Record<string, string> = {
  runs_today: "今日采集运行",
  automation_status: "自动采集状态",
  articles_today: "今日文章",
  clusters_today: "今日事件簇",
  quality_gate_rejected_today: "质量门拒收",
  documents: "文档数",
  chunks: "分块数",
  vectors: "向量数",
  queue: "摘要队列",
  daily_reserved_requests: "今日预约请求",
  limit_microusd: "预算上限（微美元）",
  used_microusd: "已用（微美元）",
  failures_today: "今日失败",
  factors_total: "因子总数",
  factors_ready: "就绪因子",
  factor_names: "因子列表",
  factor_details: "逐项输入依据",
  data_status: "数据状态",
  observed_at: "观察时间",
  cells: "判断格数",
  batch: "最新批次",
  snapshot_id: "快照标识",
  payload_sha256: "快照校验值",
  events: "事件数",
  evidence_documents: "证据文档数",
  counter_scan_status: "反证扫描状态",
  overall_status: "日度链状态",
  warnings_count: "警告数",
  question: "最近问题",
  note: "说明",
  articles_found_note: "产出说明",
  completed: "完成",
  rejected: "拒收",
  failed: "失败",
  settled_count: "已结算调用",
  formal: "正式格",
  reference: "参考格",
  unavailable: "不可用格",
  scan_outcome: "扫描结论",
  findings: "反证发现",
  stripped: "门禁剥离",
  sections: "章节数",
  number_refs: "数字引用",
  morning_brief_file: "晨报文件",
  recent_run_status: "最近运行状态"
};

// 嵌套对象（运行计数、批次摘要、队列计数）的内层键映射。
const pipelineNestedLabels: Record<string, string> = {
  name: "因子", data_status: "状态", reason: "依据", observed_at: "观察日", source_id: "来源",
  ok: "成功",
  no_relevant: "无相关",
  error: "异常",
  timeout: "超时",
  skipped: "跳过",
  pending: "排队中",
  completed: "完成",
  failed: "失败",
  rejected: "拒收",
  formal: "正式",
  reference: "参考",
  unavailable: "不可用",
  batch_id: "批次",
  business_date: "业务日",
  generated_at: "生成时间",
  formal_count: "正式",
  reference_count: "参考",
  unavailable_count: "不可用",
  snapshot_id: "快照"
};

// 枚举值的中文化；未收录值回落为去除下划线的原文（保持口径可见）。
const pipelineEnumLabels: Record<string, string> = {
  ready: "就绪",
  ready_with_warnings: "就绪（含警告）",
  blocked: "受阻",
  failed: "失败",
  completed: "已完成",
  degraded: "降级",
  skipped: "跳过",
  no_counter_evidence_found: "未发现反证",
  counter_evidence_found: "发现反证",
  insufficient_evidence: "证据不足",
  idle: "空闲", missing: "尚无记录", unknown: "暂无法确认", unavailable: "暂不可用"
};

const pipelineEvidenceKindLabels: Record<string, string> = {
  doc: "文档",
  snapshot_path: "快照路径",
  batch: "批次",
  state_source: "状态文件"
};

const pipelineNodePurpose: Record<string, string> = {
  collect: "读取公开价格、新闻与公告", clean: "去重并检查资料质量与可用性", index: "整理证据，供检索与引用",
  event_summary: "提炼单篇事实并生成中文摘要", event_overview: "聚合相关事件，整理产业影响", factor_score: "计算价格与事件的方向信号",
  seven_product: "输出七品种 × 三周期的唯一主线预测", counter_scan: "寻找可能推翻当前判断的证据", daily_interpretation: "解释当日方向、依据与风险",
  report_assembly: "汇总研判与证据，形成报告", assistant: "按问题检索证据并解释判断",
  event_signal: "挑出当日值得研判的事件并装配信号", political_analysis: "解读事件背后的政治与政策含义",
  historical_analog: "检索相似历史案例并提炼方向先验", product_synthesis: "把事件先验叠加到品种价格基准",
  skeptic_review: "对事件因子做反证与跨品种一致性校验", event_fusion: "按护栏规则为每格定案最终方向",
  shadow_eval: "结算历史判断并蒸馏经验教训回灌"
};
const pipelineBusinessNames: Record<string, string> = {
  collect: "采集", clean: "清洗去重门禁", index: "证据索引", event_summary: "事件摘要",
  event_overview: "事件总览", factor_score: "因子打分", seven_product: "七产品预测",
  event_signal: "当日事件精选", political_analysis: "政局解读", historical_analog: "历史经验",
  product_synthesis: "品种研判", skeptic_review: "交叉质证", event_fusion: "预测定案",
  shadow_eval: "复盘校准",
  counter_scan: "反证扫描", daily_interpretation: "日报解读", report_assembly: "研报组装", assistant: "研判助手",
  unified_memory: "统一记忆舱"
};

// 解剖画布常量（2026-10-02）：Agent 链预算 / 机制说明 / 数据出处。
// 全部为展示层静态口径，不改变 /pipeline/graph 契约与节点身份。
const pipelineChainDailyCap = 60;
const pipelineStageBudgets: Record<string, number> = {
  political_analysis: 16, historical_analog: 16, product_synthesis: 7, skeptic_review: 7
};
// 后端 /pipeline/graph 在链阶段节点上附带 metrics（calls/fallback/rejected），
// 但共享 TS 类型未声明；此处只读读取，不改共享契约类型。
type PipelineDisplayEdge = Omit<PipelineGraphEdge, "kind"> & { kind: "flow" | "dashed" | "feedback" };
type PipelineGraphNodeWithMetrics = PipelineGraphNode & { metrics?: Record<string, unknown> };
type PipelineGraphWithBudget = PipelineGraphResponse & { chain_budget?: {
  attempts_used: number | null; cap: number | null; business_date: string;
  basis: string; source: string; status: "ok" | "unknown";
} };
function pipelineDailyBudget(graph?: PipelineGraphResponse): {used?: number; cap?: number} {
  const budget = (graph as PipelineGraphWithBudget | undefined)?.chain_budget;
  if (!budget) return {cap: pipelineChainDailyCap};
  const cap = typeof budget.cap === "number" && Number.isInteger(budget.cap) && budget.cap > 0 ? budget.cap : undefined;
  const valid = budget.status === "ok" && budget.business_date === graph?.business_date
    && budget.basis === "HTTP 尝试级，含 schema 重试" && budget.source === "event-agent-chain-latest.json:budget"
    && cap !== undefined && typeof budget.attempts_used === "number"
    && Number.isInteger(budget.attempts_used) && budget.attempts_used >= 0 && budget.attempts_used <= cap;
  return {cap, used: valid ? budget.attempts_used! : undefined};
}
function pipelineChainBudget(node: PipelineGraphNode): { used?: number; cap: number; fallback?: number } | undefined {
  const cap = pipelineStageBudgets[node.id];
  if (!cap) return undefined;
  const metrics = (node as PipelineGraphNodeWithMetrics).metrics ?? {};
  const count = (value: unknown) => typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
  const reportedCap = count(metrics.call_cap);
  return { used: count(metrics.calls), cap: reportedCap && reportedCap > 0 ? reportedCap : cap, fallback: count(metrics.fallback) };
}
const pipelineNodeProvenance: Record<string, string> = {
  collect: "source-automation-latest.json · 来源目录 source_automation_policy",
  clean: "清洗质量门禁报告 · source-automation-latest.json",
  index: "semantic_chunks 表 · unified_retriever 语义索引",
  event_summary: "事件事实摘要（event_summary_quality） · llm_traces 台账",
  event_overview: "事件聚类 · 情报雷达快照",
  factor_score: "事件因子打分 · llm_traces 台账",
  event_signal: "event-signal-latest.json（冻结输入集 input_sha256）",
  political_analysis: "event_agent_analyses 表 · event-agent-chain-latest.json · llm_traces 成本台账",
  historical_analog: "political_case_memory 案例库 · event_agent_analyses 表 · event-agent-chain-latest.json",
  product_synthesis: "event_agent_analyses 表 · event-agent-chain-latest.json",
  skeptic_review: "event_agent_analyses 表 · event-agent-chain-latest.json（skeptic_verdict）",
  event_fusion: "latest-status.json（event_fusion） · forecast_event_factors 表",
  seven_product: "seven_product_forecast_batches / _cells 表（append-only 账本）",
  shadow_eval: "seven_product_forecast_outcomes · observation_ledger",
  counter_scan: "反证扫描报告 · llm_traces 台账",
  daily_interpretation: "information_reports 表（市场与产业研判日报）",
  report_assembly: "morning-brief/ · latest-status.json",
  assistant: "主链工件只读接口 · 引用事件 / 案例 / 教训标识",
  unified_memory: "规划中：political_case_memory + agent_lessons + 语义索引（ADR-10 统一记忆舱）"
};
const pipelineNodeMechanics: Record<string, { body: string; footnote?: string }> = {
  collect: { body: "按来源目录定时抓取公开文章与价格（43+ 源），07:50 进入事件管线，预测仅使用截至 08:00 北京时间可得的信息；数据只增不改，保证可回放。" },
  clean: { body: "清洗、去重与质量门禁：全文率、质量旗标逐篇标注；需人工核验的资料按观察级放行，不冒充合格证据。" },
  index: { body: "合格材料分块进入唯一语义索引；ADR-10 时点安全召回将片段与价格结果配对，满足互证条件的案例进入对应品种和期限的历史先验。当前数量与启用状态以接口为准。" },
  event_summary: { body: "LLM 为每篇合格文章生成事实摘要与结构化事件，作为全链共用的证据底座；摘要质量门禁不过关的不得进入下游。" },
  event_overview: { body: "事件聚类与总览，结果同步到情报雷达与证据页，供链外随时查证。" },
  factor_score: { body: "对事件做规则化因子打分；LLM 不可用时降级为模板级并在卡片上如实标注，不冒充推理结果。" },
  event_signal: { body: "从事件库按热度取前 16、7 天窗口，冻结输入集并计算 SHA-256——此后整条链只能引用这批事件。候选为 0 时 Agent 链整体跳过，预测照常纯价格发出。", footnote: "上下文管理：冻结 SHA 让每次研判可复现、可审计（ex-ante 事前纪律）" },
  political_analysis: { body: "对当日热度前 16 的事件逐个做政治推理：利益方分析、权力结构与制衡、话语行为定级（executed/formal_threat/声明/报道）、执行概率 0-1、传导路径与滞后；输出逐品种方向与置信度。执行概率 <0.3 且仅传闻级的事件被淘汰，不进历史经验。", footnote: "宪法 agent_chain.political_analysis.v1 · 事前防护：禁用训练记忆，引用必须落在冻结输入集内，违例拒收重试" },
  historical_analog: { body: "给存活事件在政治案例库（7,209 条案例，含利益图谱与事后实际轨迹）中找最多 3 个先例，输出分期限经验先验（方向/支持数/中位幅度）；无合适先例时如实标 no_prior，不编造。", footnote: "宪法 agent_chain.historical_analog.v1 · 候选案例之外不得编造 case_id" },
  product_synthesis: { body: "把品种价格基准预测与存活事件的政治分析、历史先验合成为该品种 D1/D7/D30 事件因子（方向/强度/置信），并给出依据链与支持事件编号；与基准反向时必须声明分歧。", footnote: "宪法 agent_chain.product_synthesis.v1 · supporting_event_ids 必须在冻结输入集内" },
  skeptic_review: { body: "对合成因子做反证检验与跨品种传导一致性检查（例：原油 up 但 PTA down 需要解释）；裁决 维持/降级/推翻，修正只允许向保守方向——方向只能转 neutral、置信不得升高。", footnote: "宪法 agent_chain.skeptic_review.v1 · 裁决修正仅限保守方向" },
  event_fusion: { body: "零 LLM 的护栏融合：R1 同向确认、R2 置信 ≥0.6、分歧且历史同向支持案例 ≥2 才切换、R3 分歧记录、R4 置信不足维持基准、R5 跨品种矛盾交裁决 Agent（≤2 次/日）后重跑规则；D1 恒用基准（O1 期限门控）。融合方向即发行方向。", footnote: "规则代码 event_fusion.py · 25 年回测 O1：事件信息不在 D+1 兑现" },
  seven_product: { body: "把融合后的 21 格（7 品种 × D1/D7/D30）落进不可变账本；被事件改写的格保留纯价格视图作存证，方向按单主线口径发行。", footnote: "ADR-9 单主线：融合方向就是发行方向，账本不可改写" },
  shadow_eval: { body: "到期格按 point-in-time 价格结算，方向命中率进入评估；已结算样本每周一 09:05 蒸馏为 lessons（≤30 条活跃），经蓝虚线回灌到政局解读与历史经验的下一轮推理。", footnote: "教训保留来源与样本引用，以背景假设参与下一轮研判；新教训只从到期改写格提炼，缺少合格样本时跳过。" },
  counter_scan: { body: "每日一次 DeepSeek 反证巡检，专门寻找可能推翻当前判断的新证据；0 反证是诚实的饥饿状态，不是故障。" },
  daily_interpretation: { body: "基于当日信息日报生成研判解读；日报停更时本节点如实降级，不用旧报告冒充当日解读。" },
  report_assembly: { body: "汇总当日研判与证据组装晨报/日报；待上游就绪后自动运行。" },
  assistant: { body: "主链工件只读接口：问答检索主链产物，每个回答必须携带可溯源引文；无方向计票权，不参与发牌。", footnote: "链外读取：不能用问答答案改写已发行账本或充当新方向先验" },
  unified_memory: { body: "ADR-10：索引召回配价格结果，同方向 ≥3 条独立互证才有计票权。未启用时保留规划态；已有案例库与教训单独运行，本卡聚合资源和真实启用状态。", footnote: "ADR-10 · 前端资源卡：启用状态以接口为准，不增加后端节点" }
};
function pipelineNodeResult(node: PipelineGraphNode): string {
  return pipelineNodeSummary(node).summary;
}
function pipelineBusinessText(value: string): string {
  return value
    .replace(/pending=(\d+)/g, "待处理 $1 篇").replace(/failed=(\d+)/g, "失败 $1 篇").replace(/质量旗标/g, "质量提示")
    .replace(/needs_human_review/g, "需人工核验")
    .replace(/observation_only/g, "仅用于观察")
    .replace(/warnings=(\d+)/g, "提示 $1 项")
    .replace(/quality_flags/g, "质量提示")
    .replace(/OOS evaluation blocked/g, "到期评估受阻")
    .replace(/exit[= ](\d+)/g, "执行返回码 $1")
    .replace(/critical:/g, "关键异常：")
    .replace(/automation=blocked/g, "自动采集受阻")
    .replace(/automation=missing/g, "暂无自动采集状态")
    .replace(/\bblocked\b/g, "受阻")
    .replace(/\bfailed\b/g, "失败")
    .replace(/\bunknown\b/g, "未知")
    .replace(/overall=ok/g, "质量检查通过")
    .replace(/\bwaiting\b/g, "等待中")
    .replace(/(\d+)\/(\d+)\s*micro-USD/gi, (_, used, limit) => `已用 $${(Number(used) / 1e6).toFixed(2)} / 上限 $${(Number(limit) / 1e6).toFixed(2)}`)
    .replace(/error\s*(\d+)\s*\+\s*timeout\s*(\d+)/gi, "失败 $1 次，超时 $2 次")
    .replace(/ok\s*率/gi, "成功率")
    .replace(/automation=degraded/gi, "自动采集降级")
    .replace(/automation=ok/gi, "自动采集正常")
    .replace(/unavailable=(\d+)/gi, "不可用 $1 项")
    .replace(/formal=(\d+)/gi, "正式 $1 项")
    .replace(/reference=(\d+)/gi, "观察 $1 项")
    .replace(/overall=/gi, "汇总状态：")
    .replace(/morning-brief/gi, "晨报")
    .replace(/completed/gi, "已完成")
    .replace(/degraded/gi, "降级")
    .replace(/heartbeat/gi, "最近心跳")
    .replace(/(\d+)s\b/g, "$1 秒")
    .replace(/\bclean\b/g, "清洗与质量检查").replace(/\bcollect\b/g, "采集").replace(/latest[-_]status/g, "最近状态").replace(/artifact/gi, "研判产出")
    .replace(/limit=0/gi, "可用预算为零").replace(/worker/gi, "执行任务").replace(/minit0/gi, "初始化限制")
    .replace(/fail[-_]closed/gi, "校验未通过时暂停").replace(/featured/gi, "已筛选")
    .replace(/ready_with_warnings/g, "已就绪，含提示").replace(/ready/g, "已就绪")
    .replace(/skipped/g, "已跳过").replace(/run\b/g, "运行记录")
    .replace(/OOS/g, "样本外检验").replace(/([a-z]+_[a-z_]+)/gi, "内部记录");
}

function pipelineEnumLabel(value: string): string {
  if (!value) return "—";
  return pipelineEnumLabels[value] ?? pipelineBusinessText(value);
}

// 内部标识符（状态源文件/表名等）去下划线，避免页面暴露内部字段拼写。
function pipelineIdentifierLabel(value: string): string {
  const sources: Record<string, string> = {
    news_fetch_runs: "新闻采集记录", price_intraday: "行情采集记录",
    "local-production/source-automation/source-automation-latest.json": "来源自动采集状态",
    "local-production/output-freshness/latest.json": "产出时效检查"
  };
  return sources[value] ?? pipelineSummaryLabels[value] ?? pipelineNestedLabels[value] ?? "运行记录";
}

function pipelineFormatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return Number.isInteger(value) ? formatInteger(value) : String(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return pipelineEnumLabel(value);
  if (Array.isArray(value)) {
    if (!value.length) return "0 项";
    const items = value.slice(0, 8).map((item) => pipelineFormatValue(item));
    return `${items.join("、")}${value.length > 8 ? ` 等 ${formatInteger(value.length)} 项` : ""}`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== null && item !== undefined && item !== "");
    const rendered = entries
      .slice(0, 6)
      .map(([key, item]) => `${pipelineNestedLabels[key] ?? pipelineIdentifierLabel(key)} ${key === "source_id" ? priceSourceLabel(String(item)) : pipelineFormatValue(item)}`);
    const suffix = entries.length > 6 ? `（另有 ${entries.length - 6} 项）` : "";
    return rendered.length ? `${rendered.join(" · ")}${suffix}` : "—";
  }
  return String(value);
}

function pipelineSummaryRows(record: Record<string, unknown> | undefined): { label: string; value: string }[] {
  if (!record) return [];
  return Object.entries(record)
    .filter(([key, value]) => pipelineSummaryLabels[key] && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => ({ label: pipelineSummaryLabels[key], value: pipelineFormatValue(value) }));
}

function pipelineCostAmount(cost: PipelineDailyCost | undefined): { amount: number; hasDisplay: boolean } {
  if (!cost) return { amount: 0, hasDisplay: false };
  if (typeof cost.display_amount_micros === "number") {
    return { amount: cost.display_amount_micros / 1_000_000, hasDisplay: true };
  }
  return { amount: (cost.amount_micros ?? 0) / 1_000_000, hasDisplay: false };
}

function pipelineCostLabel(cost: PipelineDailyCost | undefined): string {
  if (!cost) return "暂无成本记录";
  const { amount } = pipelineCostAmount(cost);
  return `¥${amount.toFixed(4)} · ${formatInteger(cost.calls)} 次调用`;
}

function pipelineCostNote(cost: PipelineDailyCost | undefined): string {
  if (!cost) return "";
  const nativeCurrency = cost.native_currency ?? cost.currency ?? "CNY";
  const nativeAmount =
    typeof cost.native_amount_micros === "number" ? cost.native_amount_micros : cost.amount_micros;
  if (typeof nativeAmount !== "number") return "";
  if (nativeCurrency === "CNY" || (!cost.fx_rate && !cost.display_amount_micros)) return "";
  const symbol = nativeCurrency === "USD" ? "$" : "";
  const rate = cost.fx_rate ? ` · 汇率 ${cost.fx_rate}` : "";
  const version = cost.fx_version ? `（${cost.fx_version}）` : "";
  return `原生 ${symbol}${(nativeAmount / 1_000_000).toFixed(6)}${rate}${version}`;
}

function pipelineRunStatusLabel(status: string): string {
  if (!status) return "状态待记录";
  if (status === "ok" || status === "completed") return "成功";
  if (status === "degraded") return "降级";
  if (status === "needs_human_review") return "带质量旗标";
  if (status === "failed" || status === "cancelled") return "失败";
  return pipelineEnumLabel(status);
}

function pipelineLatencyLabel(latencyMs: number | undefined): string {
  if (!latencyMs || latencyMs <= 0) return "耗时未记录";
  if (latencyMs < 1000) return `${formatInteger(latencyMs)} 毫秒`;
  return `${(latencyMs / 1000).toFixed(1)} 秒`;
}

function PipelineDrawerSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pipeline-drawer-section" data-section={title}>
      <h4><FileTextOutlined aria-hidden />{title}</h4>
      <div className="pipeline-drawer-section-body">{children}</div>
    </section>
  );
}

// 泳道分组带：portal 进 .react-flow__viewport，让分组底带与节点共用同一
// 平移/缩放变换；层级压在连线与节点之下（styles.css 中 z-index 0/1），
// pointer-events 关闭，不参与点击与 18 节点拓扑契约。
function PipelineLaneBands({ calls, cap, compact, onLens, memoryPlanning }: { calls?: number; cap?: number; compact: boolean; onLens: (title: string) => void; memoryPlanning: boolean }) {
  const [viewportEl, setViewportEl] = useState<HTMLElement>();
  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const probe = () => {
      if (cancelled) return;
      const element = document.querySelector<HTMLElement>(".agent-flow-canvas .react-flow__viewport");
      if (element) {
        setViewportEl(element);
        return;
      }
      if (attempts++ < 40) window.setTimeout(probe, 150);
    };
    probe();
    return () => { cancelled = true; };
  }, []);
  if (!viewportEl) return null;
  return createPortal(
    pipelineLaneGroups.map((group) => (
      <div
        key={group.id}
        className={`pipeline-lane-band is-${group.id}${group.emphasis ? " is-emphasis" : ""}`}
        style={pipelineLaneBandStyle(group, compact)}
      >
        <div className="pipeline-lane-head">
          <span className="pipeline-lane-glyph" style={{ color: group.color }}>{group.id === "data" ? <DatabaseOutlined /> : group.id === "event" ? <ClusterOutlined /> : group.id === "prediction" ? <PartitionOutlined /> : group.id === "review" ? <ClockCircleOutlined /> : group.id === "memory" ? <CloudSyncOutlined /> : <FileTextOutlined />}</span>
          <strong className="pipeline-lane-title" style={{ color: group.color }}>{group.id === "memory" && !memoryPlanning ? "记忆 · 统一资源（ADR-10）" : group.label}</strong>
          {group.keywords.map((keyword) => (
            <button
              type="button"
              onClick={() => onLens(keyword)}
              key={keyword}
              className="pipeline-lane-keyword"
              data-testid="pipeline-lane-keyword"
              style={{ borderColor: pipelineLaneRgba(group.color, 0.5), color: group.color }}
            >
              {keyword}
            </button>
          ))}
          {group.emphasis ? <span className="pipeline-lane-budget">Agent 链 · {cap ?? "—"} 次/日硬顶 · 四阶段 {calls ?? "—"}</span> : null}
        </div>
      </div>
    )),
    viewportEl
  );
}

// 直达 ?module=workflow 时模块可能在应用外壳完成布局前挂载（画布零尺寸），
// ReactFlow 初始 fitView 会得到退化视口；节点数据到位后再补一次 fitView。
function PipelineRefitView({ ready, compact }: { ready: boolean; compact: boolean }) {
  const { setViewport } = useReactFlow();
  useEffect(() => {
    if (!ready) return;
    // 节点实测尺寸会随字体/徽章渲染多次变化，且内部 initialized 标志可能滞后；
    // 直接用 DOM 实测矩形反推图坐标，把 18 个节点全部收进画布可视框。
    const fit = () => {
      const canvas = document.querySelector<HTMLElement>(".agent-flow-canvas");
      const viewportEl = canvas?.querySelector<HTMLElement>(".react-flow__viewport");
      // 窄屏下 .react-flow 非绝对定位，画布 padding-top 会压缩真实可视区；
      // 必须按视口窗格（.react-flow 元素）矩形计算，而不是外层画布矩形。
      const paneEl = viewportEl?.parentElement;
      if (!canvas || !viewportEl || !paneEl || canvas.closest(".has-node-drawer")) return;
      const frame = paneEl.getBoundingClientRect();
      const pane = viewportEl.parentElement!.getBoundingClientRect();
      const transform = new DOMMatrixReadOnly(getComputedStyle(viewportEl).transform);
      const nodes = [...canvas.querySelectorAll<HTMLElement>(".react-flow__node")];
      if (!nodes.length) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const node of nodes) {
        const rect = node.getBoundingClientRect();
        const gx = (rect.left - pane.left - transform.e) / transform.a;
        const gy = (rect.top - pane.top - transform.f) / transform.d;
        const gw = rect.width / transform.a;
        const gh = rect.height / transform.d;
        minX = Math.min(minX, gx); minY = Math.min(minY, gy);
        maxX = Math.max(maxX, gx + gw); maxY = Math.max(maxY, gy + gh);
      }
      // 饱满取景：把标题带、左右跨行通道及底部助手入口一起计入包围盒。
      // 保留既有实测节点和贴边居中逻辑，避免抽屉压缩后裁掉外侧连线。
      minX -= 52; minY -= 112; maxX += 94; maxY += 70;
      const pad = 24;
      const zoom = Math.max(0.02, Math.min(1.35, Math.min(
        (frame.width - pad * 2) / Math.max(maxX - minX, 1),
        (frame.height - pad * 2) / Math.max(maxY - minY, 1)
      )));
      void setViewport({
        x: (frame.width - (maxX - minX) * zoom) / 2 - minX * zoom,
        y: (frame.height - (maxY - minY) * zoom) / 2 - minY * zoom,
        zoom,
      });
    };
    const timers = [60, 500, 1200, 2500, 4000, 5500, 7000, 8500].map((delay) => window.setTimeout(fit, delay));
    const canvas = document.querySelector<HTMLElement>(".agent-flow-canvas");
    const observer = new ResizeObserver(fit);
    if (canvas) observer.observe(canvas);
    return () => { timers.forEach((timer) => window.clearTimeout(timer)); observer.disconnect(); };
  }, [ready, compact, setViewport]);
  return null;
}

function PipelineSelectionView({ selectedNodeId }: { selectedNodeId?: string }) {
  const { getNode, getViewport, setViewport } = useReactFlow();
  useEffect(() => {
    if (!selectedNodeId) return;
    const node = getNode(selectedNodeId);
    const pane = document.querySelector<HTMLElement>(".agent-flow-canvas .react-flow");
    if (!node || !pane || pane.clientWidth < 900) return;
    const viewport = getViewport();
    const center = node.position.x + pipelineCardWidth(selectedNodeId) / 2;
    void setViewport({ ...viewport, x: (pane.clientWidth - 470) / 2 - center * viewport.zoom });
  }, [selectedNodeId, getNode, getViewport, setViewport]);
  return null;
}

function AgentWorkflowModule({ onNavigate }: { onNavigate: (module: ModuleId) => void }) {
  // 首屏一次聚合（/pipeline/graph），抽屉懒加载（/pipeline/nodes/{id}）。
  // 旧 12 角色账本视图已退役：本页不再读取 /agent-runs*，也不再渲染
  // 固化角色叙述；后端数据保留只读。
  const [graph, setGraph] = useState<PipelineGraphResponse>();
  const [graphError, setGraphError] = useState<string>();
  const [graphLoading, setGraphLoading] = useState(true);
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [nodeDetails, setNodeDetails] = useState<Record<string, PipelineNodeInspectionResponse | { error: string }>>({});
  const [detailLoading, setDetailLoading] = useState(false);
  const [lessonsRegistry, setLessonsRegistry] = useState<AgentLessonsResponse>();
  const [governance, setGovernance] = useState<GovernanceReportResponse>();
  const [technicalView, setTechnicalView] = useState<string>();
  const [activeLens, setActiveLens] = useState<string>();
  const drawerOpen = selectedNodeId !== undefined || technicalView !== undefined;
  const compact = false; // Drawer selection never changes graph geometry.
  const openLens = (title: string) => { setSelectedNodeId(undefined); setActiveLens(title); setTechnicalView(title); };
  const selectNode = (id: string) => { setTechnicalView(undefined); setSelectedNodeId(id); };

  const loadGraph = useCallback(async () => {
    setGraphLoading(true);
    try {
      const payload = await api.pipelineGraph();
      setGraph(payload);
      setGraphError(undefined);
    } catch (error) {
      // Keep the last observed payload; the warning marks it as stale.
      // A failed first read still renders unknown skeleton states.
      setGraphError(error instanceof Error ? error.message : "管道状态读取失败");
    } finally {
      setGraphLoading(false);
    }
  }, []);

  useEffect(() => { void loadGraph(); }, [loadGraph]);

  // 治理姿态（审计 A2）：日度治理报告此前只有端点无消费，工具栏徽章补上读侧。
const governanceDeliveryLabels: Record<string, string> = {
  low_confidence: "低置信投递",
  full: "全量投递",
  paused: "暂停投递",
};
  useEffect(() => {
    let cancelled = false;
    api.governanceReport()
      .then((report) => { if (!cancelled) setGovernance(report); })
      .catch(() => { /* 治理徽章缺席不阻塞画布 */ });
    return () => { cancelled = true; };
  }, []);

  // 教训库读侧（审计 A3）：复盘校准抽屉打开时拉取一次注册表。
  useEffect(() => {
    if (selectedNodeId !== "shadow_eval" || lessonsRegistry) return;
    let cancelled = false;
    api.agentLessons()
      .then((payload) => { if (!cancelled) setLessonsRegistry(payload); })
      .catch(() => { /* 抽屉内以空态呈现 */ });
    return () => { cancelled = true; };
  }, [selectedNodeId, lessonsRegistry]);

  useEffect(() => {
    if (!selectedNodeId || graphLoading) return;
    // 统一记忆舱是前端规划节点：后端无此节点详情，不发请求。
    if (selectedNodeId === "unified_memory") { setDetailLoading(false); return; }
    if (nodeDetails[selectedNodeId]) return;
    let cancelled = false;
    setDetailLoading(true);
    api.pipelineNodeDetail(selectedNodeId, graph?.business_date)
      .then((detail) => {
        if (!cancelled) setNodeDetails((previous) => ({ ...previous, [selectedNodeId]: detail }));
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setNodeDetails((previous) => ({
            ...previous,
            [selectedNodeId]: { error: error instanceof Error ? error.message : "节点详情读取失败" }
          }));
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => { cancelled = true; };
  }, [selectedNodeId, nodeDetails, graph?.business_date, graphLoading]);

  function refreshPipeline() {
    setNodeDetails({});
    void loadGraph();
  }

  const graphDegraded = Boolean(graphError) && !graph;
  const graphUnknown = !graph;
  const pipelineNodes = graph?.nodes ?? pipelineSkeletonNodes;
  const graphEdges = graph?.edges as PipelineDisplayEdge[] | undefined;
  const interactionEdges = graphEdges?.filter((edge) => edge.kind === "dashed").map((edge) => ({
    ...edge, ...pipelineInteractionEdges.find((item) => item.from === edge.from && item.to === edge.to)
  })) ?? pipelineInteractionEdges;
  const flowEdges = graphEdges?.filter((edge) => edge.kind === "flow") ?? pipelineSkeletonFlowEdges;
  // 经验回灌边（蓝色虚线）：API 优先，降级骨架用固定拓扑兜底。
  const feedbackEdges = graphEdges?.filter((edge) => edge.kind === "feedback")
    ?? pipelineFeedbackEdges.map((edge) => ({ from: edge.from, to: edge.to, kind: "feedback" }));
  const statusCounts = pipelineNodes.reduce((acc, node) => {
    acc[node.status] = (acc[node.status] ?? 0) + 1;
    return acc;
  }, {} as Record<PipelineNodeStatus, number>);
  const selectedGraphNode = pipelineNodes.find((node) => node.id === selectedNodeId);
  const selectedBudget = !graphUnknown && selectedGraphNode ? pipelineChainBudget(selectedGraphNode) : undefined;
  const selectedDetailEntry = selectedNodeId ? nodeDetails[selectedNodeId] : undefined;
  const selectedDetail = selectedDetailEntry && !("error" in selectedDetailEntry) ? selectedDetailEntry : undefined;
  const selectedDetailError = selectedDetailEntry && "error" in selectedDetailEntry ? selectedDetailEntry.error : undefined;
  // 四阶段调用小计：缺少任一阶段 metrics 时未知，不能冒充全链 HTTP 账本。
  const stageUsage = Object.keys(pipelineStageBudgets).map(id => pipelineNodes.find(node => node.id === id)).map(node => node ? pipelineChainBudget(node)?.used : undefined);
  const chainStageCalls = !graphUnknown && stageUsage.every(value => value !== undefined) ? stageUsage.reduce<number>((sum, value) => sum + (value ?? 0), 0) : undefined;
  const dailyBudget = pipelineDailyBudget(graph);
  const memoryObservation = useMemo(() => pipelineMemoryObservation(graphUnknown ? undefined : graph), [graph, graphUnknown]);

  const flowNodes = useMemo<Node<PipelineFlowNodeData>[]>(() => {
    const apiNodes: Node<PipelineFlowNodeData>[] = pipelineNodes.map((node) => ({
      id: node.id,
      type: "workflowAgent",
      position: { x: (pipelineNodeLayout[node.id]?.x ?? 0) * (compact ? 0.7 : 1), y: pipelineNodeLayout[node.id]?.y ?? 0 },
      selected: node.id === selectedNodeId,
      data: {
        nodeId: node.id,
        lensActive: pipelineLenses.find(lens => lens.title === activeLens)?.nodes.includes(node.id),
        compact,
        kind: node.kind,
        name: pipelineBusinessNames[node.id] ?? node.name,
        status: graphUnknown ? undefined : node.status,
        statusDetail: graphUnknown ? (graphLoading ? "正在读取管道状态" : "管道状态暂不可读") : pipelineNodeResult(node),
        timestamp: graphUnknown ? "" : node.timestamp,
        budget: graphUnknown ? undefined : pipelineChainBudget(node)
      }
    }));
    // 统一记忆舱是前端资源聚合卡，不增加后端节点；能力位与运行证据分开读取。
    apiNodes.push({
      id: "unified_memory",
      type: "workflowAgent",
      position: { x: pipelineNodeLayout.unified_memory.x * (compact ? 0.7 : 1), y: pipelineNodeLayout.unified_memory.y },
      selected: selectedNodeId === "unified_memory",
      data: {
        nodeId: "unified_memory",
        lensActive: pipelineLenses.find(lens => lens.title === activeLens)?.nodes.includes("unified_memory"),
        compact,
        kind: "agent",
        name: pipelineBusinessNames.unified_memory,
        status: memoryObservation.status,
        memoryPlanning: memoryObservation.planning,
        statusDetail: memoryObservation.summary,
        timestamp: memoryObservation.timestamp
      }
    });
    return apiNodes;
  }, [pipelineNodes, selectedNodeId, graphUnknown, graphLoading, compact, activeLens, memoryObservation]);

  const flowEdgeRecords: Edge[] = [
    ...flowEdges.map((edge) => {
      const edgeId = `${edge.from}-${edge.to}`;
      // 预测定案 → 七产品预测：定案方向是唯一主线（ADR-9），
      // 这条边是事件推演汇入主预测的唯一汇合点，按原型加粗蓝色强调。
      const isFusionJoinEdge = edge.from === "event_fusion" && edge.to === "seven_product";
      return {
        id: edgeId,
        source: edge.from,
        target: edge.to,
        type: "pipelineRouted",
        data: { compact, routeKind: "flow" },
        sourceHandle: "right",
        targetHandle: "left",
        label: "",
        className: isFusionJoinEdge ? "is-pipeline-flow is-pipeline-key" : "is-pipeline-flow",
        markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, markerUnits: "userSpaceOnUse", color: isFusionJoinEdge ? "#2563eb" : pipelinePalette[pipelineLane(edge.from)].color },
        style: isFusionJoinEdge
          ? { stroke: "#2563eb", strokeWidth: 3.5 }
          : { stroke: pipelinePalette[pipelineLane(edge.from)].color, strokeWidth: 2 }
      };
    }),
    ...interactionEdges.map((edge) => ({
      id: `interaction-${edge.from}-${edge.to}`,
      source: edge.from,
      target: edge.to,
      type: "pipelineRouted",
      data: { compact, routeKind: "interaction" },
      sourceHandle: "bottom",
      targetHandle: `entry-${edge.from}`,
      label: edge.label,
      className: "is-pipeline-interaction",
      markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, markerUnits: "userSpaceOnUse", color: "#64748b" },
      style: { stroke: "#64748b", strokeWidth: 1.5, strokeDasharray: "6 5" }
    })),
    ...feedbackEdges.map((edge) => ({
      id: `feedback-${edge.from}-${edge.to}`,
      source: edge.from,
      target: edge.to,
      type: "pipelineRouted",
      data: { compact, routeKind: "feedback" },
      // 回灌是逆向学习流：复盘校准与政局解读/历史经验同排，走 顶→顶
      // 过肩弧线沿上廊道回传，不穿卡片、不与正向主链缠绕。
      sourceHandle: "top",
      targetHandle: "top",
      label: "经验回灌",
      className: "is-pipeline-feedback",
      markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18, markerUnits: "userSpaceOnUse", color: "#2563eb" },
      style: { stroke: "#2563eb", strokeWidth: 2, strokeDasharray: "8 6", opacity: 0.85 }
    }))
  ];

  return (
    <PageFrame
      title="Agent 系统"
      eyebrow="研判流程与证据交接"
      description="以混合节点图查看每日研判执行链：确定性代码环节与固定工作流 Agent 环节同图，节点状态来自真实状态源；点击节点查看输入、输出与证据。"
      className={`workflow-page${drawerOpen ? " has-node-drawer" : ""}`}
    >
      <div className="workflow-layout">
        <Panel title="研判执行流程" icon={<ApartmentOutlined />} className="workflow-board-panel" extra={
          <div className="review-pipeline-toolbar" aria-label="节点运行统计">
            <span className="pipeline-mainline-pill">统一研判主线 · v2</span>
            <EvidenceDossierButton label="分析证据档案" />
            <span>节点总数 <b>{formatInteger(pipelineNodes.length)}</b><span className="is-planning-note"> · {memoryObservation.planning ? "规划" : "资源"} 1</span></span>
            <span className="is-chain-budget" data-testid="pipeline-chain-budget">四阶段调用 {chainStageCalls ?? "—"} · 全链已用 {dailyBudget.used ?? "—"}/{dailyBudget.cap ?? "—"}</span>
            <span className="is-ok">成功 <b>{graphUnknown ? "—" : formatInteger(statusCounts.ok ?? 0)}</b></span>
            <span className="is-degraded">降级 <b>{graphUnknown ? "—" : formatInteger(statusCounts.degraded ?? 0)}</b></span>
            <span>外部等待 <b>{graphUnknown ? "—" : formatInteger(statusCounts.waiting ?? 0)}</b></span>
            <span>空闲 <b>{graphUnknown ? "—" : formatInteger(statusCounts.idle ?? 0)}</b></span>
            {governance?.status === "ok" && governance.report ? (
              <span title={`治理姿态（评估于 ${formatDate(String(governance.report.evaluated_at ?? ""))}）`}>
                治理 · 投递 <b>{governanceDeliveryLabels[String(governance.report.delivery_mode ?? "")] ?? "见日报"}</b> · 置信上限 <b>{governance.report.confidence_cap ?? "—"}</b>
                {" · "}{governance.report.production_ready ? "生产就绪" : "未就绪"}
              </span>
            ) : null}
            <Button size="small" aria-label="刷新状态" icon={<ReloadOutlined />} onClick={refreshPipeline} loading={graphLoading}>刷新状态</Button>
          </div>
        }>

          <div className="pipeline-technical-toolbar" aria-label="技术剖面">
            {pipelineLenses.map(lens => <button key={lens.title} type="button" aria-pressed={activeLens === lens.title} onClick={() => openLens(lens.title)}>{lens.title}</button>)}
            <button type="button" onClick={() => openLens("验证证据")}>验证证据与边界 ↗</button>
          </div>
          <div className="pipeline-authority-rail" aria-label="定案输入与条件支路">
            <button type="button" onClick={() => openLens("融合与门禁")}><b>纯价格基准</b> → 品种研判 / 预测定案 · D1 恒用基准 · 链失败按基准发牌</button>
            <button type="button" onClick={() => openLens("多 Agent 接力")}><b>R5 条件支路</b> 矛盾 → 裁决 ≤2 次 → 规则重跑</button>
            <button type="button" onClick={() => openLens("记忆模块")}>复盘教训 → 政局解读 / 历史经验</button>
          </div>
          {graphError ? (
            <Alert
              type="warning"
              showIcon
              className="pipeline-degraded-banner"
              message={graph ? "状态刷新失败 · 当前显示上次读取结果" : "管道状态暂不可读"}
              description={graph
                ? `上次数据生成时间：${graph.generated_at}；本次未更新，不能视为当前状态。原因：${graphError}。`
                : `节点身份按固定拓扑展示，状态未知；原因：${graphError}。可点击“刷新状态”重试。`}
            />
          ) : null}
          <div
            className="agent-flow-canvas pipeline-graph-canvas"
            data-testid="agent-topology"
            onKeyDown={(event) => {
              if (event.key !== "Enter" && event.key !== " ") return;
              const nodeId = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node")?.dataset.id;
              if (!nodeId) return;
              event.preventDefault();
              selectNode(nodeId);
            }}
          >
            <div className="workflow-map-legend pipeline-map-legend">
              {pipelineLaneGroups.filter((group) => group.id !== "assistant").map((group) => (
                <span key={group.id}><i style={{ background: group.color, borderColor: group.color }} />{group.label.split(" · ")[0]}</span>
              ))}
              <span><i style={{ background: "#64748b", borderColor: "#64748b" }} />链外</span>
              <span><i className="is-pipeline-dashed" />虚线 = 交互入口</span>
              <span><i className="is-pipeline-feedback" />蓝虚线 = 经验回灌</span>
              <span><i className="is-pipeline-planning" />图纸上</span>
            </div>
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdgeRecords}
              nodeTypes={workflowNodeTypes}
              edgeTypes={pipelineEdgeTypes}
              style={{ height: "100%" }}
              minZoom={0.02}
              maxZoom={1.25}
              panOnScroll
              fitViewOptions={{ padding: { top: '48px', bottom: '48px', left: '24px', right: '24px' } }}
              proOptions={{ hideAttribution: true }}
              nodesDraggable={false}
              nodesConnectable={false}
              edgesReconnectable={false}
              deleteKeyCode={null}
              onNodeClick={(_, node) => selectNode(node.id)}
            >
              <PipelineRefitView ready compact={compact} />
              <PipelineSelectionView selectedNodeId={selectedNodeId} />
              <PipelineLaneBands calls={chainStageCalls} cap={dailyBudget.cap} compact={compact} onLens={openLens} memoryPlanning={memoryObservation.planning} />
              <Background color="#dbeafe" gap={24} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </Panel>
      </div>

      <PipelineTechnicalDrawer view={technicalView} onClose={() => { setTechnicalView(undefined); setActiveLens(undefined); }} onNode={selectNode} />
      <Drawer
        title={
          <div className="pipeline-drawer-title">
            <strong>{pipelineBusinessNames[selectedNodeId ?? ""] ?? selectedGraphNode?.name ?? "环节详情"}</strong>
            <span>{selectedNodeId === "unified_memory"
              ? memoryObservation.planning ? "ADR-10 · 规划说明" : "ADR-10 · 资源观测"
              : selectedGraphNode?.kind === "agent" ? "Agent · 调度说明" : "代码 · 环节说明"}</span>
          </div>
        }
        open={selectedNodeId !== undefined}
        onClose={() => setSelectedNodeId(undefined)}
        width={470}
        className="pipeline-node-drawer"
        mask={false}
        destroyOnClose
        footer={
          <div className="pipeline-drawer-footer">
            <Button size="small" aria-label="关闭" onClick={() => setSelectedNodeId(undefined)}>关闭</Button>
            <span className="pipeline-drawer-readonly">只读观测 · 不可编辑</span>
            <span className="pipeline-drawer-footer-spacer" />
          </div>
        }
      >
        <div className="pipeline-drawer-body" data-testid="pipeline-node-drawer">
          {graphError && graph ? <Alert type="warning" showIcon message="画布状态与预算为上次读取结果" description={`数据生成时间：${graph.generated_at}；刷新未成功，以下画布预算不能视为当前值。`} /> : null}
          {selectedNodeId === "unified_memory" ? (
            <Alert
              type="info"
              showIcon
              message={memoryObservation.planning ? "规划节点（ADR-10）" : "统一记忆资源（ADR-10）"}
              description={`${memoryObservation.summary} 同向 ≥3 条独立互证才有计票权；启用与效果验收分别观测。`}
            />
          ) : null}
          {selectedNodeId === "unified_memory" ? (
            <PipelineDrawerSection title="资源与核验">
              <div className="pipeline-drawer-budget" data-testid="pipeline-memory-resources">
                <div><span>索引文档</span><strong>{memoryObservation.indexDocs ?? "—"}</strong></div>
                <div><span>活跃教训</span><strong>{memoryObservation.lessonsActive ?? "—"}</strong></div>
                <div><span>召回能力</span><strong>{memoryObservation.capability}</strong></div>
              </div>
              <p>{memoryObservation.evidenceSummary}</p>
              <p>资源时刻：{memoryObservation.timestamp ? formatDate(memoryObservation.timestamp) : "未知"}</p>
              <small className="pipeline-drawer-footnote">索引数来自语义索引；教训数来自有效期内教训库；运行核验来自当日链报告。数量与启用位不能证明成功，本卡不属于后端节点，也不发详情请求。</small>
            </PipelineDrawerSection>
          ) : null}
          {selectedDetail?.kind === "agent" ? <AgentImplementationProfile profile={selectedDetail.implementation_profile} /> : null}
          <PipelineDrawerSection title="节点职责">
            <p>{selectedNodeId === "unified_memory" && !memoryObservation.planning ? "聚合索引、案例与复盘教训；召回按来源、时刻、品种和期限核对互证资格，合格案例按计票开关进入历史先验。" : (pipelineNodeMechanics[selectedNodeId ?? ""]?.body ?? pipelineNodePurpose[selectedNodeId ?? ""]) || "查看本环节的输入与产出"}</p>
            {pipelineNodeMechanics[selectedNodeId ?? ""]?.footnote ? (
              <small className="pipeline-drawer-footnote">{selectedNodeId === "unified_memory" && !memoryObservation.planning ? "ADR-10 · 能力、资源和运行证据分别观测，18 个后端节点身份不变。" : pipelineNodeMechanics[selectedNodeId ?? ""]?.footnote}</small>
            ) : null}
          </PipelineDrawerSection>
          {selectedNodeId === "shadow_eval" ? (
            <PipelineDrawerSection title="活跃教训（复盘校准记忆）">
              {!lessonsRegistry ? (
                <small>教训库读取中或暂不可读。</small>
              ) : lessonsRegistry.lessons.length === 0 ? (
                <small>教训库为空：等待周一蒸馏积累。</small>
              ) : (
                <div className="pipeline-drawer-lessons" data-testid="agent-lessons-list">
                  <small>共 {formatInteger(lessonsRegistry.total)} 条 · 活跃 {formatInteger(lessonsRegistry.active)} 条（注入政局解读/历史经验提示词）</small>
                  <ul>
                    {lessonsRegistry.lessons.slice(0, 30).map((lesson) => (
                      <li key={lesson.lesson_id}>
                        <span className="pipeline-lesson-source">
                          {lesson.lesson_id.startsWith("sim2025-") ? "模拟导入" : "蒸馏"} · {lesson.category}
                        </span>
                        <span className="pipeline-lesson-text">{lesson.lesson}</span>
                        <small>{formatDate(lesson.valid_from)}{lesson.status !== "active" ? ` · ${lesson.status}` : ""}</small>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </PipelineDrawerSection>
          ) : null}
          {selectedNodeId && pipelineStageBudgets[selectedNodeId] ? (
            <PipelineDrawerSection title="预算与调用（Agent 链）">
              <div className="pipeline-drawer-budget">
                <div><span>调用 / 上限</span><strong>{selectedBudget ? `${selectedBudget.used ?? "—"} / ${selectedBudget.cap}` : `— / ${pipelineStageBudgets[selectedNodeId]}`}</strong></div>
                <div><span>模板回退</span><strong>{selectedBudget?.fallback ?? "—"}</strong></div>
                <div><span>今日链累计</span><strong>{dailyBudget.used ?? "—"}/{dailyBudget.cap ?? "—"}</strong></div>
              </div>
              <small className="pipeline-drawer-footnote">预算为 HTTP 尝试级硬顶：schema 修复重试同样计费；耗尽自动回退模板路径，不阻塞发牌。全链用量只认当日链报告硬顶快照；快照缺失时未知，四阶段小计不代替含 R5 的全链账本。</small>
            </PipelineDrawerSection>
          ) : null}
          {detailLoading && selectedNodeId !== "unified_memory" && !selectedDetailEntry ? <div className="pipeline-drawer-loading"><Spin /><span>正在读取节点详情</span></div> : null}
          {selectedDetailError && selectedNodeId !== "unified_memory" ? (
            <Alert
              type="error"
              showIcon
              message="节点详情暂不可读"
              description={selectedDetailError}
              action={<Button size="small" onClick={() => { if (selectedNodeId) setNodeDetails((previous) => { const next = { ...previous }; delete next[selectedNodeId]; return next; }); }}>重试</Button>}
            />
          ) : null}
          {selectedDetail ? (
            <>
              <PipelineDrawerSection title="今日状态与时间戳">
                <div className="pipeline-drawer-status">
                  <FieldTag tone={pipelineStatusTone[selectedDetail.status_block.status] ?? "muted"}>
                    {pipelineStatusLabels[selectedDetail.status_block.status] ?? selectedDetail.status_block.status}
                  </FieldTag>
                  <strong>{pipelineBusinessText(selectedDetail.status_block.status_detail) || "状态详情待返回"}</strong>
                  <span>{selectedDetail.status_block.timestamp ? `数据源时刻：${formatDate(selectedDetail.status_block.timestamp)}` : "数据源时刻待记录"}</span>
                  {selectedDetail.status_block.sources.length ? (
                    <small>状态源：{selectedDetail.status_block.sources.slice(0, 4).map(pipelineIdentifierLabel).join("、")}</small>
                  ) : null}
                </div>
              </PipelineDrawerSection>
              <PipelineDrawerSection title="输入摘要">
                {pipelineSummaryRows(selectedDetail.input_summary).length ? (
                  <dl className="pipeline-drawer-list">
                    {pipelineSummaryRows(selectedDetail.input_summary).map((row) => (
                      <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>
                    ))}
                  </dl>
                ) : <p className="pipeline-drawer-empty">该节点暂无输入摘要。</p>}
                <details className="pipeline-contract-record"><summary>查看输入原始字段（只读）</summary><pre>{JSON.stringify(selectedDetail.input_summary, null, 2)}</pre></details>
              </PipelineDrawerSection>
              <PipelineDrawerSection title="输出摘要">
                {pipelineSummaryRows(selectedDetail.output_summary).length ? (
                  <dl className="pipeline-drawer-list">
                    {pipelineSummaryRows(selectedDetail.output_summary).map((row) => (
                      <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>
                    ))}
                  </dl>
                ) : <p className="pipeline-drawer-empty">该节点暂无输出摘要。</p>}
                <details className="pipeline-contract-record"><summary>查看输出原始字段（只读）</summary><pre>{JSON.stringify(selectedDetail.output_summary, null, 2)}</pre></details>
              </PipelineDrawerSection>
              <PipelineDrawerSection title="证据入口">
                <EvidenceDossierButton label="主预测发行时证明" initialView="issued" />
                {selectedDetail.evidence_entries.length ? (
                  <ul className="pipeline-evidence-list">
                    {selectedDetail.evidence_entries.slice(0, 8).map((entry, index) => (
                      <li key={`${entry.kind}-${entry.id}-${index}`}>
                        <FieldTag tone="info">{pipelineEvidenceKindLabels[entry.kind] ?? pipelineIdentifierLabel(entry.kind)}</FieldTag>
                        <div>
                          <strong>{entry.id || "—"}</strong>
                          {entry.note ? <span>{pipelineEnumLabel(entry.note)}</span> : null}
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : <p className="pipeline-drawer-empty">该节点暂无独立证据入口。</p>}
              </PipelineDrawerSection>
              <PipelineDrawerSection title="数据出处">
                <code className="pipeline-drawer-provenance">{pipelineNodeProvenance[selectedNodeId ?? ""] ?? "状态源清单见「今日状态与时间戳」。"}</code>
              </PipelineDrawerSection>
              {selectedDetail.agent_extra ? (
                <>
                  <PipelineDrawerSection title="最近运行记录">
                    {selectedDetail.agent_extra.recent_runs.length ? (
                      <ul className="pipeline-run-list">
                        {selectedDetail.agent_extra.recent_runs.slice(0, 5).map((run) => (
                          <li key={run.run_id || run.started_at}>
                            <div>
                              <strong>{run.started_at ? formatDate(run.started_at) : "时间待记录"}</strong>
                              <FieldTag tone={run.status === "ok" || run.status === "completed" ? "success" : run.status === "degraded" || run.status === "needs_human_review" ? "warning" : "muted"}>
                                {pipelineRunStatusLabel(run.status)}
                              </FieldTag>
                              {run.derived_status ? <FieldTag tone="info">历史口径换算</FieldTag> : null}
                              <span>{pipelineLatencyLabel(run.latency_ms)}</span>
                            </div>
                            {run.question ? <p>{cleanBusinessText(run.question)}</p> : null}
                            {run.gate_result ? <p>门禁：{pipelineEnumLabel(run.gate_result)}</p> : null}
                          </li>
                        ))}
                      </ul>
                    ) : <p className="pipeline-drawer-empty">该 Agent 今日暂无运行记录。</p>}
                  </PipelineDrawerSection>
                  <PipelineDrawerSection title="当日成本">
                    <div className="pipeline-cost-block" data-testid="pipeline-cost-block">
                      <strong>{pipelineCostLabel(selectedDetail.agent_extra.daily_cost)}</strong>
                      {pipelineCostNote(selectedDetail.agent_extra.daily_cost) ? (
                        <span
                          className="pipeline-cost-native"
                          title={pipelineCostNote(selectedDetail.agent_extra.daily_cost)}
                        >
                          {pipelineCostNote(selectedDetail.agent_extra.daily_cost)}
                        </span>
                      ) : null}
                      {selectedDetail.agent_extra.daily_cost.note ? (
                        <span>口径说明：{pipelineIdentifierLabel(selectedDetail.agent_extra.daily_cost.note)}</span>
                      ) : null}
                    </div>
                  </PipelineDrawerSection>
                </>
              ) : null}
              {selectedDetail.node_id === "assistant" ? (
                <div className="pipeline-assistant-entry">
                  <Button
                    type="primary"
                    icon={<CommentOutlined />}
                    onClick={() => onNavigate("assistant")}
                  >
                    去研判助手提问
                  </Button>
                  <span>研判助手为链外交互入口：只读检索主链工件，无方向计票权，不改变主链判断。</span>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      </Drawer>
    </PageFrame>
  );
}

function assistantEvidenceFromView(item: AssistantEvidenceView, index: number): AssistantEvidence {
  return {
    id: item.id || `assistant-evidence-${index}`,
    category: cleanBusinessText(item.category, "业务证据"),
    title: cleanBusinessText(item.title, `证据 ${index + 1}`),
    reason: cleanBusinessText(item.summary, "被本次回答引用。"),
    tone: normalizeTone(item.tone),
    observed: cleanBusinessText(item.observed_label, ""),
    url: item.url && /^https?:\/\//i.test(item.url) ? item.url : undefined
  };
}

function assistantEvidenceFallback(payload: AssistantChatResponse): AssistantEvidence[] {
  return (payload.display_evidence ?? []).slice(0, 8).map(assistantEvidenceFromView);
}

function assistantFallbackLabel(reason?: string | null) {
  if (!reason) return "";
  if (/missing_api_key|provider_auth_error/.test(reason)) return "模型服务配置未通过，本次使用已检索证据形成的本地回答。";
  if (/provider_rate_limited/.test(reason)) return "模型服务暂时限流，本次使用本地证据回答。";
  if (/provider_request_rejected/.test(reason)) return "模型服务未接受本次请求，本次使用本地证据回答。";
  if (/provider_unavailable|timeout|Error/i.test(reason)) return "模型服务暂时不可用，本次使用本地证据回答，可稍后重试。";
  if (/invalid_model_output|Validation/.test(reason)) return "模型输出未通过结构校验，本次使用本地证据回答。";
  if (/indirect_prompt_injection/.test(reason)) return "检索材料触发内容核验，本次使用本地证据回答。";
  return "本次使用本地证据回答；具体证据和使用边界见上方。";
}

function assistantEvidenceGroupsFromPayload(payload: AssistantChatResponse): ChatMessage["evidenceGroups"] {
  const groups = payload.evidence_groups;
  const fallback = assistantEvidenceFallback(payload);
  if (!groups) {
    return { adopted: [], referenceMaterials: fallback, excluded: [], conflicts: [] };
  }
  return {
    adopted: groups.adopted.map(assistantEvidenceFromView),
    referenceMaterials: (groups.reference_materials ?? payload.display_evidence ?? []).map(assistantEvidenceFromView),
    excluded: groups.excluded.map(assistantEvidenceFromView),
    conflicts: groups.conflicts.map(assistantEvidenceFromView)
  };
}

function assistantAnswerFromPayload(payload: AssistantChatResponse, formalReady: boolean) {
  // An explicit N-sentence request is answered with just the conclusion and
  // its confidence boundary; rendering the full section stack would ignore
  // the user's brevity constraint.
  if (payload.length_constraint_sentences) {
    const sections = payload.answer_sections;
    return [
      `结论：${sections?.conclusion || cleanBusinessText(payload.answer, "暂未生成结论。")}`,
      `可信边界：${sections?.confidence_boundary || "回答已被约束为简短形式，未展开完整证据与风险结构。"}`
    ].join("\n");
  }
  if (!formalReady) {
    const sections = payload.answer_sections;
    return [
      `观察方向（非正式）：${sections?.conclusion || cleanBusinessText(payload.answer, "当前证据尚不足以形成观察方向。")}`,
      "正式结论：未形成；本次回答不会进入正式报告或预测账本。",
      `依据：${sections?.evidence_points?.length ? sections.evidence_points.join("；") : "正式研判门禁尚未满足，当前仅展示已返回证据。"}`,
      `反证：${sections?.counter_evidence?.length ? sections.counter_evidence.join("；") : "后端未返回明确反证。"}`,
      `风险：${sections?.risks?.length ? sections.risks.join("；") : "当前证据覆盖不足，不能升级为正式经营判断。"}`,
      `下一步：${sections?.next_steps?.length ? sections.next_steps.join("；") : "查看数据状态和证据图谱，等待真实证据更新。"}`,
      `可信边界：${sections?.confidence_boundary || "当前回答为观察级解释，不构成经营指令。"}`
    ].join("\n");
  }
  if (payload.answer_sections) {
    const sections = payload.answer_sections;
    return [
      `结论：${sections.conclusion}`,
      `依据：${sections.evidence_points.length ? sections.evidence_points.join("；") : "当前业务证据不足。"}`,
      `反证：${sections.counter_evidence.length ? sections.counter_evidence.join("；") : "暂无明确反证，但仍需持续复核。"}`,
      `风险：${sections.risks.length ? sections.risks.join("；") : "问答结果仅作为研判辅助。"}`,
      `下一步：${sections.next_steps.length ? sections.next_steps.join("；") : "查看证据图谱与研判报告。"}`,
      `可信边界：${sections.confidence_boundary}`
    ].map((line) => cleanBusinessText(line, "")).filter(Boolean).join("\n");
  }
  const answer = cleanBusinessText(payload.answer || "助手已完成回答。");
  if (/结论：/.test(answer)) return answer;
  return [
    `结论：${answer}`,
    "依据：回答已结合当前证据库返回，具体证据可在右侧证据栏和证据图谱核对。",
    "反证：若价格链、事件链或供需链出现反向信号，应降低判断等级。",
    "风险：问答用于解释研判，不替代最终经营确认。",
    "下一步：先看证据图谱，再看研判报告。"
  ].join("\n");
}

function ObservationConclusionSummary({ data, compact = false }: { data?: AgentWorkbenchData; compact?: boolean }) {
  const observation = data?.low_confidence_prediction;
  if (!observation) return null;
  const direction = cleanBusinessText(observation.direction, "暂无方向信号");
  const risks = observation.key_risks.length ? observation.key_risks : observation.evidence_gaps;
  const signals = observation.verification_signals;
  const invalidations = observation.invalidation_conditions.length
    ? observation.invalidation_conditions
    : observation.reversal_condition ? [observation.reversal_condition] : [];
  return (
    <section className={`observation-conclusion-summary${compact ? " is-compact" : ""}`} data-testid="shared-observation-conclusion">
      <header>
        <div><FieldTag tone="warning">低置信 · 非正式</FieldTag><strong>本轮成本压力：{direction}</strong></div>
        <span>置信度 {formatPercent(observation.confidence)} · {formatInteger(observation.horizon_days)} 日观察</span>
      </header>
      <div className="observation-rationale">
        <span>主要依据</span>
        <p>{compactText(formatDateRangesInText(observation.rationale), 180) || "当前方向由已就绪的真实价格与事件输入形成；数据缺口已计入置信度。"}</p>
      </div>
      {cleanBusinessText(observation.rationale, "").length > 180 ? (
        <details className="text-disclosure">
          <summary>展开模型与计算详情</summary>
          <p>{formatDateRangesInText(cleanBusinessText(observation.rationale))}</p>
        </details>
      ) : null}
      <div className="observation-conclusion-grid">
        {[
          { label: "关键风险", items: risks.length ? risks : ["当前未返回额外风险说明。"], key: "risk" },
          { label: "核验信号", items: signals.length ? signals : ["下一观察日复核价格链与事件链是否延续当前方向。"], key: "signal" },
          { label: "推翻条件", items: invalidations.length ? invalidations : ["已计分品种的净趋势转向时，重新计算本轮判断。"], key: "invalidation" }
        ].map((group) => (
          <article key={group.key}>
            <strong>{group.label}</strong>
            {group.items.slice(0, 2).map((item, index) => <p key={`${group.key}-${index}-${item}`}>{index + 1}. {compactText(item, 72)}</p>)}
            {group.items.length > 2 ? (
              <details className="text-disclosure is-inline">
                <summary>查看其余 {group.items.length - 2} 项</summary>
                {group.items.slice(2).map((item, index) => <p key={`${group.key}-more-${index}-${item}`}>{index + 3}. {cleanBusinessText(item)}</p>)}
              </details>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function AssistantStructuredAnswer({ item }: { item: ChatMessage }) {
  const references = [...(item.evidence ?? []), ...Object.values(item.evidenceGroups ?? {}).flat()];
  const renderText = (value: string) => splitAssistantCitations(value, references).map((part, index) => {
    if ("text" in part) return <span key={index}>{cleanBusinessText(part.text, "")}</span>;
    if ("unmatched" in part) return <span key={index} className="assistant-citation-unmatched">[引用未匹配]</span>;
    const { reference, number } = part;
    return reference.url && /^https?:\/\//i.test(reference.url)
      ? <a key={index} href={reference.url} target="_blank" rel="noopener noreferrer"
          title={reference.title} aria-label={`来源 ${number}：${reference.title}`}>[来源 {number}]</a>
      : <span key={index} title={reference.title}>[证据 {number}：{reference.title}]</span>;
  });
  if (!item.sections) {
    return (
      <>
        {item.content.split("\n").map((line, index) => <p key={`${item.id}-line-${index}`}>{renderText(line || " ")}</p>)}
      </>
    );
  }
  const nonFormal = item.status === "degraded";
  const sections = [
    { label: nonFormal ? "观察方向（非正式）" : "正式结论", values: [item.sections.conclusion] },
    ...(nonFormal ? [{ label: "正式结论", values: ["未形成；本次回答不会进入正式报告或预测账本。"] }] : []),
    { label: "依据", values: item.sections.evidence_points },
    { label: "反证", values: item.sections.counter_evidence },
    { label: "风险", values: item.sections.risks },
    { label: "下一步", values: item.sections.next_steps },
    { label: "可信边界", values: [item.sections.confidence_boundary] }
  ];
  return (
    <div className="assistant-answer-sections">
      {sections.map((section) => (
        <section key={`${item.id}-${section.label}`}>
          <FieldTag tone={section.label === "风险" || section.label === "反证" || section.label.startsWith("观察方向") ? "warning" : section.label === "正式结论" ? "success" : "info"}>{section.label}</FieldTag>
          <div>
            {section.values.length ? section.values.map((value, index) => (
              <p key={`${item.id}-${section.label}-${index}`}>{renderText(value)}</p>
            )) : <p>暂无补充说明。</p>}
          </div>
        </section>
      ))}
    </div>
  );
}

function assistantReferenceText(value: string): string {
  // Known source field values only; leave source IDs and links untouched.
  return value.replace(/public recent average/g, "公开近期均价")
    .replace(/China polyester public assessment/g, "中国聚酯公开报价")
    .replace(/published_day/g, "按发布日")
    .replace(/CNY\/mt/g, "元/吨")
    .replace(/^(poy|dty) (?=涤纶)/i, "");
}

function AiAssistantModule({
  data,
  live,
  onNavigate
}: {
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  onNavigate: (module: ModuleId) => void;
}) {
  const { message } = AntApp.useApp();
  const [selectedEvidence, setSelectedEvidence] = useState<{ group: string; item: AssistantEvidence } | null>(null);
  const [assistantSideTab, setAssistantSideTab] = useState(0);
  const initialEvidence = useMemo<AssistantEvidence[]>(() => {
    const retrieval = evidenceFromRetrieval(live);
    return retrieval.length
      ? retrieval.map(item => ({ ...item, title: assistantReferenceText(item.title), reason: assistantReferenceText(item.reason) }))
      : businessSources(data, live).map((source) => ({ category: source, title: source, reason: "用于回答时的证据类别。" }));
  }, [data, live]);
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingStage, setLoadingStage] = useState("正在检索证据");
  const [lastQuestion, setLastQuestion] = useState("");
  const sendingRef = useRef(false);
  const messageIdRef = useRef(0);
  const stageTimersRef = useRef<number[]>([]);
  const chatMessagesRef = useRef<HTMLDivElement | null>(null);
  const nextMessageId = useCallback((prefix: string) => {
    messageIdRef.current += 1;
    return `${prefix}-${messageIdRef.current}`;
  }, []);
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    {
      id: "welcome",
      role: "assistant",
      content: "选择一个问题，或输入你关心的事件与品种。回答会区分依据、相反材料和待核验条件，并提供原文出处。问答用于解释依据，不构成经营指令。",
      evidence: initialEvidence,
      evidenceGroups: { adopted: [], referenceMaterials: initialEvidence, excluded: [], conflicts: [] }
    }
  ]);
  const recommendations = ["今天上游成本压力怎么看？", "哪些证据支持当前结论？", "什么信号会推翻判断？", "哪些风险今天最要盯？"];
  const latestAnswer = [...messages].reverse().find(item => item.role === "assistant" && item.id !== "welcome");
  const latestEvidenceGroups = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const item = messages[index];
      if (item.id === "welcome") return { adopted: [], referenceMaterials: initialEvidence, excluded: [], conflicts: [] };
      if (item.evidenceGroups) return item.evidenceGroups;
      if (item.evidence?.length) return { adopted: [], referenceMaterials: item.evidence, excluded: [], conflicts: [] };
    }
    return { adopted: [] as AssistantEvidence[], referenceMaterials: [] as AssistantEvidence[], excluded: [] as AssistantEvidence[], conflicts: [] as AssistantEvidence[] };
  }, [messages, initialEvidence]);
  const referencesAreSourceCategories = latestEvidenceGroups.referenceMaterials.length > 0
    && latestEvidenceGroups.referenceMaterials.every((item) => item.reason === "用于回答时的证据类别。");

  useEffect(() => () => {
    stageTimersRef.current.forEach((timer) => window.clearTimeout(timer));
  }, []);

  useEffect(() => {
    chatMessagesRef.current?.scrollTo({ top: chatMessagesRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending, loadingStage]);

  function startLoadingStages() {
    setLoadingStage("正在检索证据");
    stageTimersRef.current.forEach((timer) => window.clearTimeout(timer));
    stageTimersRef.current = [
      window.setTimeout(() => setLoadingStage("正在整理回答"), 1200),
      window.setTimeout(() => setLoadingStage("仍在生成，可稍后查看或重试"), 15_000)
    ];
  }

  function stopLoadingStages() {
    stageTimersRef.current.forEach((timer) => window.clearTimeout(timer));
    stageTimersRef.current = [];
  }

  async function ask(nextQuestion = question) {
    const trimmed = nextQuestion.trim();
    if (!trimmed) return;
    if (sendingRef.current) {
      // 不丢弃新问题：明确提示在途，输入框内容保留，回答完成后再发送。
      message.info("上一问题仍在回答中；请稍候，输入内容已保留。");
      return;
    }
    sendingRef.current = true;
    setQuestion("");
    setLastQuestion(trimmed);
    const userMessage: ChatMessage = { id: nextMessageId("user"), role: "user", content: trimmed, question: trimmed };
    setMessages((items) => [...items, userMessage]);
    setSending(true);
    startLoadingStages();
    try {
      const response = await api.chat(trimmed);
      const groups = assistantEvidenceGroupsFromPayload(response);
      const formalReady = hasFormalConclusion(data, live);
      const assistant: ChatMessage = {
        id: nextMessageId("assistant"),
        role: "assistant",
        content: assistantAnswerFromPayload(response, formalReady),
        evidence: assistantEvidenceFallback(response),
        evidenceGroups: groups,
        contextPackId: response.context_pack_id,
        // A sentence-constrained answer is carried by content alone; keeping
        // sections here would let the structured renderer re-insert the full
        // section stack over the compact conclusion.
        sections: response.length_constraint_sentences
          ? undefined
          : (response.answer_sections ?? undefined),
        status: formalReady ? (response.status ?? "success") : "degraded",
        fallbackReason: assistantFallbackLabel(response.fallback_reason),
        question: trimmed,
        qualityGates: response.quality?.gates?.length ? response.quality.gates : undefined,
        qualityOverall: response.quality?.overall
      };
      setMessages((items) => [...items, assistant]);
    } catch (error) {
      const body = error instanceof Error ? error.message : "问答服务暂时不可用。";
      setMessages((items) => [...items, {
        id: nextMessageId("assistant-error"),
        role: "assistant",
        content: `结论：暂时无法完成回答。\n依据：${cleanBusinessText(body)}\n反证：请稍后重试或查看研判报告。\n风险：当前不生成新的业务建议。\n建议查看：总览看板与证据图谱。\n可信边界：本次回答未完成生成，不能作为新的业务判断依据。`,
        evidence: [],
        evidenceGroups: { adopted: [], referenceMaterials: [], excluded: [], conflicts: [{ category: "需处理", title: "问答暂不可用", reason: "请稍后重试，或先查看证据图谱与研判报告。", tone: "warning" }] },
        status: "failed",
        question: trimmed
      }]);
      message.warning("问答服务暂时不可用，已保留当前问题。");
    } finally {
      stopLoadingStages();
      sendingRef.current = false;
      setSending(false);
    }
  }

  return (
    <PageFrame
      title="AI 研判助手"
      eyebrow="证据问答"
      description="面向客户的业务问答入口，回答会尽量引用当前证据和报告，不在前端直连外部模型。"
      className="assistant-page"
    >
      <div className="assistant-layout">
        <Panel title="对话区" icon={<CommentOutlined />} className="assistant-chat-panel">
          <div className="chat-messages" data-testid="assistant-messages" ref={chatMessagesRef}>
            {messages.map((item) => (
              <article key={item.id} className={`chat-message is-${item.role}`}>
                <div className="chat-message-head">
                  <strong>{item.role === "assistant" ? "研判助手" : "你"}</strong>
                  {item.role === "assistant" && item.status ? (
                    <FieldTag
                      tone={
                        item.status === "failed"
                          ? "danger"
                          : item.qualityOverall === "passed_with_flags" || item.status !== "success"
                            ? "warning"
                            : "success"
                      }
                    >
                      {item.status === "failed" ? "需重试" : item.qualityOverall === "passed_with_flags" ? "已回答 · 带质量旗标" : "已回答"}
                    </FieldTag>
                  ) : null}
                </div>
                {item.role === "assistant" && item.qualityGates?.length ? (
                  <div className="assistant-quality-gates" data-testid="assistant-quality-gates">
                    {item.qualityGates.map((gate) => (
                      <span
                        key={gate.name}
                        className={`assistant-quality-gate${gate.passed ? " is-passed" : " is-flagged"}`}
                        title={gate.passed ? `${gate.label}通过` : gate.reason || `${gate.label}未通过`}
                      >
                        {gate.passed ? `${gate.label}通过` : `${gate.label}未过`}
                      </span>
                    ))}
                  </div>
                ) : null}
                {item.role === "assistant" && item.qualityGates?.some((gate) => !gate.passed && gate.reason) ? (
                  <ul className="assistant-quality-reasons" data-testid="assistant-quality-reasons">
                    {item.qualityGates.filter((gate) => !gate.passed && gate.reason).map((gate) => (
                      <li key={`${gate.name}-reason`}>{gate.reason}</li>
                    ))}
                  </ul>
                ) : null}
                <AssistantStructuredAnswer item={item} />
                {item.fallbackReason ? <p className="assistant-fallback-note">{item.fallbackReason}</p> : null}
                {item.status === "failed" ? (
                  <div className="assistant-message-actions">
                    <Button size="small" icon={<ReloadOutlined />} onClick={() => ask(item.question || lastQuestion)}>重试</Button>
                    <Button size="small" onClick={() => onNavigate("evidence")}>查看证据图谱</Button>
                  </div>
                ) : null}
              </article>
            ))}
            {sending ? (
              <article className="chat-message is-assistant is-loading">
                <Spin size="small" />
                <strong>{loadingStage}</strong>
              </article>
            ) : null}
          </div>
          {!latestAnswer && !sending && assistantSideTab !== 1 ? <div className="question-chips" aria-label="开始提问">
            {recommendations.slice(0, 3).map(item => <Button key={item} onClick={() => setQuestion(item)}>填入：{item}</Button>)}
          </div> : null}
          <div className="chat-input">
            <Input.TextArea
              aria-label="输入研判问题"
              autoComplete="off"
              name="assistant-question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onPressEnter={(event) => {
                if (!event.shiftKey) {
                  event.preventDefault();
                  ask();
                }
              }}
              placeholder="输入关于今日研判、证据、风险或报告的问题…"
              autoSize={{ minRows: 1, maxRows: 3 }}
              disabled={sending}
            />
            <Button type="primary" icon={<SendOutlined />} loading={sending} disabled={sending || !question.trim()} onClick={() => ask()}>发送</Button>
          </div>
        </Panel>
        <WorkbenchTabs labels={["引用证据", "推荐问题", "当前观察"]} className="review-assistant-side" onChange={setAssistantSideTab}>
        <Panel title="本次回答引用的证据" icon={<NodeIndexOutlined />} className="assistant-evidence-panel">
          {latestAnswer?.contextPackId ?
            <EvidenceDossierButton key={latestAnswer.id} label="本次回答证明"
              initialTarget={evidenceTargetFromQuestion(latestAnswer.question ?? "")}
              context={{context_pack_id: latestAnswer.contextPackId}} /> : null}
          {!latestAnswer ? <p className="assistant-evidence-before-answer">提问并获得回答后展示采用证据、反证与冲突、已排除材料。</p> : null}
          <div className="assistant-evidence-list">
            {[
              ...(latestAnswer ? [{ title: "正式结论采用证据", items: latestEvidenceGroups.adopted, tone: "success" as Tone }] : []),
              {
                title: referencesAreSourceCategories ? "可供问答检索的来源类别" : "问答参考材料",
                items: latestEvidenceGroups.referenceMaterials,
                tone: "info" as Tone,
                unit: referencesAreSourceCategories ? "类来源" : "条"
              },
              ...(latestAnswer ? [
                { title: "反证与冲突", items: latestEvidenceGroups.conflicts, tone: "warning" as Tone },
                { title: "已排除材料", items: latestEvidenceGroups.excluded, tone: "muted" as Tone }
              ] : [])
            ].map((group) => (
              <section key={group.title} className="assistant-evidence-group">
                <div className="assistant-evidence-group-title"><FieldTag tone={group.tone}>{group.title}</FieldTag><span>{formatInteger(group.items.length)} {"unit" in group ? group.unit : "条"}</span></div>
                {group.items.length ? group.items.map((item, index) => (
                  <button type="button" key={`${group.title}-${item.id ?? item.category}-${item.title}-${index}`} onClick={() => setSelectedEvidence({ group: group.title, item })}>
                    <FieldTag tone={item.tone ?? "info"}>{item.category}</FieldTag>
                    <strong>{item.title}</strong>
                    <span>{item.reason}</span>
                    {item.observed ? <small>{item.observed}</small> : null}
                  </button>
                )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`${group.title}暂无记录`} />}
              </section>
            ))}
          </div>
        </Panel>        <Panel title="推荐问题" className="assistant-question-panel">
          <div className="question-chips">
            {recommendations.map((item) => (
              <Button key={item} onClick={() => ask(item)} disabled={sending}>{item}</Button>
            ))}
          </div>
        </Panel>
        {data?.low_confidence_prediction ? <ObservationConclusionSummary data={data} compact /> : <Empty description="当前尚无观察结论" />}
        </WorkbenchTabs>
      </div>
      <Drawer title="问答证据详情" open={selectedEvidence !== null} onClose={() => setSelectedEvidence(null)} width={480}>
        {selectedEvidence ? <article>
          <FieldTag tone={selectedEvidence.item.tone ?? "info"}>{selectedEvidence.group}</FieldTag>
          <h3>{selectedEvidence.item.title}</h3>
          <p>{selectedEvidence.item.category}</p>
          <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{selectedEvidence.item.reason}</p>
          {selectedEvidence.item.observed ? <p>观察时间：{formatTimestampsInText(selectedEvidence.item.observed)}</p> : null}
          {selectedEvidence.item.id ? <p style={{ overflowWrap: "anywhere" }}>证据编号：{selectedEvidence.item.id}</p> : null}
          {selectedEvidence.item.url ? <p><a href={selectedEvidence.item.url} target="_blank" rel="noopener noreferrer">查看原始来源</a></p> : null}
          <Button onClick={() => { setSelectedEvidence(null); onNavigate("evidence"); }}>进入证据图谱</Button>
        </article> : null}
      </Drawer>
    </PageFrame>
  );
}

function buildReports(data?: AgentWorkbenchData): DeliveryClientReport[] {
  if (!data?.client_reports.length) return [];
  return data.client_reports.filter(isFormalCustomerReport);
}

const internalReportPattern = /验收|手册|补数|质量门禁|质量报告|运维|审计|更新说明|runbook|acceptance|backtest|quality|audit/i;

function isFormalCustomerReport(report: DeliveryClientReport) {
  const text = `${report.id} ${report.title} ${report.summary} ${report.audience} ${report.report_type ?? ""}`;
  if (internalReportPattern.test(text)) return false;
  return /(日报|周报|复盘报告|专题报告)/.test(text);
}

function reportMatchesType(report: DeliveryClientReport, type: string) {
  const declaredType = cleanBusinessText(report.report_type, "");
  const text = `${declaredType} ${report.title}`.toLowerCase();
  if (type === "日报") return declaredType === "日报" || text.includes("日报");
  if (type === "周报") return declaredType === "周报" || text.includes("周报");
  if (type === "复盘报告") return declaredType === "复盘报告" || text.includes("复盘");
  if (type === "专题报告") return declaredType === "专题报告" || text.includes("专题");
  return false;
}

function reportDownloadUrl(report?: DeliveryClientReport) {
  return report?.id && report.status === "ready" && report.content_status === "ready" && report.download_available === true
    ? api.clientReportDownloadUrl(report.id)
    : "";
}

function reportSnapshotBinding(report: DeliveryClientReport | undefined, data?: AgentWorkbenchData, live?: WorkbenchLiveData) {
  const current = formalConclusionSnapshot(data, live);
  if (!current) return { matched: false, reason: "当前正式研判快照尚未形成，报告操作已禁用。" };
  if (!report?.data_snapshot_id || !report.as_of_time) {
    return { matched: false, reason: "报告缺少数据快照标识或研判时点，无法确认与当前正式结论一致。" };
  }
  if (report.data_snapshot_id !== current.dataSnapshotId || report.as_of_time !== current.asOfTime) {
    return { matched: false, reason: "报告快照与当前正式研判不一致，请重新生成报告后再预览、复制或下载。" };
  }
  return { matched: true, reason: "报告已绑定当前正式研判快照。" };
}

function horizonLabel(horizon: StoredPrediction["horizon"]) {
  if (horizon === "1d") return "1 天";
  if (horizon === "7d") return "7 天";
  if (horizon === "30d") return "30 天";
  return "兼容记录";
}

function predictionDueAt(prediction: StoredPrediction) {
  if (prediction.due_at) return formatDate(prediction.due_at);
  const days = Number.parseInt(prediction.horizon, 10);
  const created = new Date(prediction.created_at);
  if (!Number.isFinite(days) || Number.isNaN(created.getTime())) return "到期时间未返回";
  created.setUTCDate(created.getUTCDate() + days);
  return formatDate(created.toISOString());
}

function SevenProductOperationalSummary({
  batch,
  data,
  onNavigate
}: {
  batch: SevenProductForecastBatch;
  data?: AgentWorkbenchData;
  onNavigate: (module: ModuleId) => void;
}) {
  const targetCells = batch.cells.filter((cell) => cell.horizon_days === 1);
  const freshTargets = targetCells.filter((cell) => cell.data_status === "fresh").length;
  const staleOrMissing = targetCells.filter((cell) => cell.data_status !== "fresh").length;
  const scheduleTotal = data?.update_schedule.length ?? 0;
  const scheduleReady = data?.update_schedule.filter((item) =>
    ["ready", "success", "enabled", "active", "scheduled"].includes(String(item.status).toLowerCase())
  ).length ?? 0;
  const automationReady = data?.source_automation?.automation_ready;
  const backupRequired = data?.source_automation?.guards?.["backup_required_before_apply"] === true;
  const modelVersions = Array.from(new Set(batch.cells.map((cell) => cell.model_version)));
  const registryRevisions = Array.from(new Set(batch.cells.map((cell) => cell.model_registry_revision)));
  const operationalStatus = data?.operational_status === "ready"
    ? "系统就绪"
    : data?.operational_status === "ready_with_warnings"
      ? "带告警运行"
      : data?.operational_status === "blocked"
        ? "系统阻断"
        : "健康状态未返回";

  return (
    <section className="seven-product-operations" data-testid="seven-product-operations">
      <header>
        <div>
          <strong>运行、更新与模型状态</strong>
          <span>以下状态来自当前预测合同与服务端交付状态，不以浏览器本地状态推断。</span>
        </div>
        <Button size="small" onClick={() => onNavigate("workflow")}>查看 Agent 系统</Button>
      </header>
      <div>
        <article>
          <span>本预测采用基准的新鲜度</span>
          <strong>{freshTargets}/7 新鲜</strong>
          <small>{staleOrMissing ? `${staleOrMissing} 个基准滞后、代理、历史不足或缺失` : "本预测采用的七个基准在时效内；不代表全部来源或历史序列齐全。"} 行情页可能采用不同基准，请分别核对单位、来源和观察日。</small>
        </article>
        <article>
          <span>来源自动化 / 调度</span>
          <strong>{typeof automationReady === "number" ? `${automationReady} 个自动就绪` : `${scheduleReady}/${scheduleTotal} 条计划就绪`}</strong>
          <small>{scheduleTotal ? `服务端返回 ${scheduleTotal} 条更新计划` : "更新计划尚未返回"}</small>
        </article>
        <article>
          <span>备份与系统健康</span>
          <strong>{operationalStatus}</strong>
          <small>{backupRequired ? "生产写入前备份门禁已启用" : "备份门禁状态待服务端确认"}</small>
        </article>
        <article>
          <span>数值模型 / 注册表</span>
          <strong>{modelVersions.join("、") || "模型未返回"}</strong>
          <small>{registryRevisions.join("、") || "注册表修订未返回"}</small>
        </article>
      </div>
    </section>
  );
}

function PredictionLedgerView({ data, live, onNavigate }: { data?: AgentWorkbenchData; live?: WorkbenchLiveData; onNavigate: (module: ModuleId) => void }) {
  const [ledgerState, setLedgerState] = useState<DataStatus<StoredPrediction[]>>({ ok: false, reason: "预测账本正在读取" });
  const [reviewState, setReviewState] = useState<DataStatus<PredictionReview[]>>({ ok: false, reason: "到期复盘正在读取" });
  const [forecastState, setForecastState] = useState<DataStatus<SevenProductForecastBatch>>({ ok: false, reason: "七产品预测正在读取", pending: true });
  const [evaluationState, setEvaluationState] = useState<DataStatus<SevenProductEvaluationBatch>>({ ok: false, reason: "样本外评测正在读取", pending: true });
  const [forecastHistory, setForecastHistory] = useState<SevenProductForecastLedgerBatch[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [observationStatus, setObservationStatus] = useState("可基于当前真实数据生成非正式观察记录");
  const [observationBusy, setObservationBusy] = useState(false);
  const observationBusyRef = useRef(false);
  const [observationRecords, setObservationRecords] = useState<PredictionObservation[]>([]);
  const [observationReadError, setObservationReadError] = useState("");
  const loadObservationRecords = useCallback(async () => {
    try {
      const result = await api.predictionObservations();
      setObservationRecords(result.items);
      setObservationReadError("");
    } catch {
      setObservationReadError("观察记录暂未读取成功，请稍后重新读取。");
    }
  }, []);
  useEffect(() => { void loadObservationRecords(); }, [loadObservationRecords]);

  const createObservation = async () => {
    if (observationBusyRef.current) return;
    observationBusyRef.current = true;
    setObservationBusy(true);
    setObservationStatus("正在读取真实模型、证据与全链路快照");
    try {
      const record = await api.createPredictionObservation();
      setObservationStatus(`已生成非正式观察记录 ${record.observation_id}：${cleanBusinessText(record.direction)} · ${formatPercent(record.confidence)}。不会进入正式报告。`);
    } catch {
      setObservationStatus("尚未确认生成结果。请先重新读取下方观察记录，核对生成时间后再决定是否重试。");
    } finally {
      await loadObservationRecords();
      observationBusyRef.current = false;
      setObservationBusy(false);
    }
  };

  const loadLedger = useCallback(() => {
    setLedgerState({ ok: false, reason: "预测账本正在读取" });
    setReviewState({ ok: false, reason: "到期复盘正在读取" });
    Promise.allSettled([
      uiRequestTimeout(api.predictions(), 10_000, "预测账本"),
      uiRequestTimeout(api.predictionReviews(), 10_000, "到期复盘")
    ]).then(([ledger, reviews]) => {
      setLedgerState(ledger.status === "fulfilled"
        ? { ok: true, value: ledger.value as StoredPrediction[] }
        : { ok: false, reason: "预测账本读取失败，请使用上方“重新读取”；如持续存在，请联系系统管理员或服务人员。" });
      setReviewState(reviews.status === "fulfilled"
        ? { ok: true, value: reviews.value }
        : { ok: false, reason: "到期复盘读取失败，请使用上方“重新读取”；如持续存在，请联系系统管理员或服务人员。" });
    });
  }, []);

  const loadCurrentForecast = useCallback(() => {
    setForecastState({ ok: false, reason: "七产品预测正在读取", pending: true });
    setEvaluationState({ ok: false, reason: "样本外评测正在读取", pending: true });
    Promise.allSettled([
      data?.main_prediction ? Promise.resolve(data.main_prediction) : uiRequestTimeout(api.sevenProductForecast(), 15_000, "七产品预测"),
      uiRequestTimeout(api.sevenProductEvaluation(), 15_000, "样本外评测"),
      uiRequestTimeout(api.sevenProductHistory(), 15_000, "真实发布账本")
    ]).then(([forecast, evaluation, history]) => {
      setForecastState(forecast.status === "fulfilled"
        ? { ok: true, value: forecast.value }
        : { ok: false, reason: "七产品预测读取失败；不会用旧账本或模拟数值补齐。" });
      setEvaluationState(evaluation.status === "fulfilled"
        ? { ok: true, value: evaluation.value }
        : { ok: false, reason: "样本外评测读取失败；当前结果不能晋级正式状态。" });
      setForecastHistory(history.status === "fulfilled" ? history.value : []);
    });
  }, [data?.main_prediction]);

  useEffect(() => {
    loadLedger();
    loadCurrentForecast();
  }, [loadCurrentForecast, loadLedger]);

  const predictions = ledgerState.ok
    ? ledgerState.value.filter((item) => item.target.includes("上游") || item.target.includes("成本压力"))
    : [];
  const reviews = reviewState.ok ? reviewState.value : [];
  const selected = predictions.find((item) => item.prediction_id === selectedId) ?? predictions[0];
  const selectedReview = selected ? reviews.find((item) => item.prediction_id === selected.prediction_id) : undefined;
  const reviewedCount = predictions.filter((item) => item.review_status === "reviewed").length;
  const formalPredictionReason = live && !live.formalPredictions.ok
    ? live.formalPredictions.reason
    : "正式预测批次尚未进入日度快照";

  return (
    <div className="ledger-workspace">
      <WorkbenchTabs labels={["七品种预测", "历史正式批次", "观察与到期复盘"]} className="review-ledger-tabs">
      <Panel
        title="当前七产品 × 三周期预测"
        subtitle="原油、石脑油、PX、PTA、MEG、POY、DTY；逐格区分正式、参考、降级与不可用"
        icon={<BarChartOutlined />}
        className="ledger-current-forecast-panel"
        extra={(
          <div className="seven-product-actions">
            <a className="seven-product-export-link" href={api.sevenProductExportUrl("csv")}>导出 CSV</a>
            <a className="seven-product-export-link" href={api.sevenProductExportUrl("json")}>导出 JSON</a>
            <Button icon={<ReloadOutlined />} onClick={loadCurrentForecast}>刷新预测</Button>
          </div>
        )}
      >
        {forecastState.ok ? (
          <>
            <SevenProductForecastGrid
              batch={forecastState.value}
              evaluation={evaluationState.ok ? evaluationState.value : null}
            />
            <SevenProductIssuedHistory history={forecastHistory} />
            <SevenProductOperationalSummary batch={forecastState.value} data={data} onNavigate={onNavigate} />
            {!evaluationState.ok ? (
              <section className="ledger-boundary-note is-pending-confirmation">
                <WarningOutlined />
                <div><strong>样本外评测不可用</strong><span>{cleanBusinessText(evaluationState.reason)}</span></div>
              </section>
            ) : null}
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={cleanBusinessText(forecastState.reason)} />
        )}
      </Panel>
      <Panel
        title="历史合同正式批次（只读审计）"
        subtitle="旧 Phase A 批次不属于当前 seven-product-forecast.v1 合同"
        icon={<SafetyCertificateOutlined />}
        className="ledger-formal-panel"
      >
        {live?.formalPredictions.ok ? (
          <FormalPredictionBatchView batches={live.formalPredictions.value} />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={cleanBusinessText(formalPredictionReason)} />
        )}
        {live?.eventFactors.ok ? (
          <EventEvidenceCard data={live.eventFactors.value} />
        ) : null}
      </Panel>
      <div className="review-ledger-history">
      <section className="ledger-boundary-note">
        <InfoCircleOutlined />
        <div>
          <strong>下方为历史兼容预测复盘（上游原料成本压力判断）</strong>
          <span>旧账本只保留历史审计与到期复盘；它不等于上方正式批次，也不提供采购、报价、接单或库存执行指令。</span>
        </div>
        <Button icon={<ReloadOutlined />} onClick={loadLedger}>重新读取</Button>
        <Button loading={observationBusy} disabled={observationBusy} onClick={() => void createObservation()}>生成观察级材料</Button>
      </section>
      <section className="ledger-boundary-note is-pending-confirmation">
        <InfoCircleOutlined /><div><strong>观察级材料</strong><span>{observationStatus}</span></div>
      </section>
      <Panel title="已生成的观察记录" extra={<Button onClick={() => void loadObservationRecords()}>重新读取观察记录</Button>}>
        {observationReadError ? <Alert type="warning" message={observationReadError} /> : null}
        {observationRecords.length ? observationRecords.slice(0, 5).map(record => <article key={record.observation_id}>
          <strong>{record.observation_id} · {formatDate(record.created_at)} · {cleanBusinessText(record.direction)} · {formatPercent(record.confidence)}</strong>
          <p>{record.rationale}</p>
          <small>非正式观察记录 · 数据时点 {formatDate(record.as_of_time)}</small>
        </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未读取到观察记录" />}
      </Panel>
      <ObservationConclusionSummary data={data} compact />
      {ledgerState.ok && predictions.length === 0 ? (
        <section className="ledger-boundary-note is-pending-confirmation" data-testid="ledger-pending-confirmation">
          <ClockCircleOutlined />
          <div>
            <strong>当前研判待确认，尚未落账</strong>
            <span>页面中的观察级方向不等于正式预测；只有满足正式门禁并关联同一数据快照后，才会写入账本。</span>
          </div>
        </section>
      ) : null}

      <div className="ledger-summary-strip">
        <article><span>判断记录</span><strong>{ledgerState.ok ? predictions.length : "—"}</strong></article>
        <article><span>已到期复盘</span><strong>{ledgerState.ok ? reviewedCount : "—"}</strong></article>
        <article><span>等待复盘</span><strong>{ledgerState.ok ? Math.max(0, predictions.length - reviewedCount) : "—"}</strong></article>
        <article><span>正式周期</span><strong>1 / 7 / 30 天</strong></article>
      </div>

      <div className="ledger-main-grid">
        <Panel title="历史兼容预测账本" subtitle="选择旧记录查看判断依据、反证和复盘状态" icon={<ClockCircleOutlined />} className="ledger-list-panel">
          {ledgerState.ok ? predictions.length ? (
            <div className="ledger-record-list">
              {predictions.map((item) => (
                <button key={item.prediction_id} type="button" className={selected?.prediction_id === item.prediction_id ? "is-active" : ""} onClick={() => setSelectedId(item.prediction_id)}>
                  <div><FieldTag tone={item.review_status === "reviewed" ? "success" : "warning"}>{item.review_status === "reviewed" ? "已复盘" : item.lifecycle_status === "due_pending_data" ? "到期待数据" : "等待到期"}</FieldTag><time>{formatDate(item.created_at)}</time></div>
                  <strong>{cleanBusinessText(item.direction, "观察")}</strong>
                  <span>{horizonLabel(item.horizon)} · 置信度 {formatPercent(item.confidence)} · 到期 {predictionDueAt(item)}</span>
                </button>
              ))}
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无上游成本压力判断记录" />
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={cleanBusinessText(ledgerState.reason)} />}
        </Panel>

        <Panel title="判断详情" subtitle="保留形成判断时的数据边界" icon={<FileTextOutlined />} className="ledger-detail-panel">
          {selected ? (
            <div className="ledger-detail-stack">
              <header><FieldTag tone="info">{horizonLabel(selected.horizon)}</FieldTag><strong>{cleanBusinessText(selected.direction, "观察")}</strong><span>置信度 {formatPercent(selected.confidence)}</span></header>
              <section><strong>判断目标</strong><p>{cleanBusinessText(selected.target)}</p></section>
              <section><strong>判断依据</strong><p>{cleanBusinessText(selected.rationale, "本条记录未返回判断依据。")}</p></section>
              <section><strong>反证</strong><p>{cleanBusinessText(selected.counter_evidence, "本条记录未返回反证。")}</p></section>
              <section className="ledger-metadata">
                <span>数据快照 <b>{selected.data_snapshot_id ? "已关联" : "未关联"}</b></span>
                <span>来源状态 <b>{cleanBusinessText(selected.source_status, "未返回")}</b></span>
                <span>复盘状态 <b>{selected.review_status === "reviewed" ? "已复盘" : "等待到期"}</b></span>
                <span>到期时间 <b>{predictionDueAt(selected)}</b></span>
              </section>
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一条判断记录后查看详情" />}
        </Panel>

        <Panel title="到期复盘" subtitle="用于识别判断偏差，不代表系统全局准确率" icon={<SafetyCertificateOutlined />} className="ledger-review-panel">
          {selectedReview ? (
            <div className="ledger-review-detail">
              <FieldTag tone={selectedReview.verdict.includes("命中") ? "success" : "warning"}>{cleanBusinessText(selectedReview.verdict, "已复盘")}</FieldTag>
              <div><span>预期方向</span><strong>{cleanBusinessText(selectedReview.expected_direction)}</strong></div>
              <div><span>实际指数</span><strong>{formatInteger(selectedReview.actual_index)}</strong></div>
              <div><span>偏差</span><strong>{selectedReview.deviation === null ? "未返回" : String(selectedReview.deviation)}</strong></div>
              <div><span>可评分状态</span><strong>{selectedReview.scoreability === "scored" ? "已评分" : selectedReview.scoreability === "waiting_for_data" ? "等待后验数据" : selectedReview.scoreability === "not_due" ? "尚未到期" : "暂不可评分"}</strong></div>
              <div><span>样本覆盖</span><strong>{formatInteger(selectedReview.scored_count)} / {formatInteger(selectedReview.total_count)} · {formatPercent(selectedReview.coverage)}</strong></div>
              <div><span>时点泄漏检查</span><strong>{selectedReview.leakage_check === "passed" ? "通过" : selectedReview.leakage_check === "failed" ? "未通过" : "尚未执行"}</strong></div>
              <section><strong>偏差原因与学习</strong><p>{cleanBusinessText(selectedReview.learning, "本轮未返回偏差说明。")}</p></section>
              <section><strong>权重调整观察</strong><p>{selectedReview.weight_adjustments.length ? selectedReview.weight_adjustments.map((item) => cleanBusinessText(item)).join("；") : "暂无调整观察。"}</p><small>以上仅为复盘学习建议，系统不会在客户页面自动修改模型权重。</small></section>
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={reviewState.ok ? "该判断尚未形成到期复盘" : cleanBusinessText(reviewState.reason)} />}
          <div className="ledger-next-links">
            <span>需要核验判断依据时，可继续查看：</span>
            <Button onClick={() => onNavigate("evidence")}>证据图谱</Button>
            <Button onClick={() => onNavigate("assistant")}>AI 研判助手</Button>
          </div>
        </Panel>
      </div>
      </div>
      </WorkbenchTabs>
    </div>
  );
}

function ReportModule({ data, live, onNavigate, workspace, type, onWorkspaceChange, onTypeChange }: {
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  onNavigate: (module: ModuleId) => void;
  workspace: ReportWorkspace;
  type: ReportType;
  onWorkspaceChange: (workspace: ReportWorkspace) => void;
  onTypeChange: (type: ReportType) => void;
}) {
  const { message } = AntApp.useApp();
  const reports = buildReports(data);
  const filteredReports = reports.filter((report) => reportMatchesType(report, type));
  const visibleReports = filteredReports;
  const pipelineStall = reportPipelineStall(visibleReports);
  const [selectedId, setSelectedId] = useState(visibleReports[0]?.id ?? "");
  const selected = visibleReports.find((item) => item.id === selectedId) ?? visibleReports[0];
  const [reportContentState, setReportContentState] = useState<DataStatus<{ content: string; content_type: string; filename: string }>>({
    ok: false,
    reason: "报告正文正在读取"
  });
  const snapshotBinding = reportSnapshotBinding(selected, data, live);
  const fileReady = Boolean(selected?.status === "ready" && selected.content_status === "ready" && selected.download_available === true);
  const downloadUrl = snapshotBinding.matched ? reportDownloadUrl(selected) : "";
  const bossView = bossConclusionView(data, live);
  const formalReportReady = Boolean(fileReady && snapshotBinding.matched && reportContentState.ok);
  const reportRisks = buildRiskItems(data).slice(0, 3);
  const reportEvents = buildBusinessEvents(data, live);
  const bossActions = Array.from(new Set([
    bossView.action,
    bossView.waiting,
    bossView.reversal
  ].filter(Boolean)));

  useEffect(() => {
    if (!visibleReports.some((report) => report.id === selectedId)) {
      setSelectedId(visibleReports[0]?.id ?? "");
    }
  }, [selectedId, visibleReports]);

  useEffect(() => {
    if (!selected?.id || !fileReady || !snapshotBinding.matched) {
      setReportContentState({ ok: false, reason: fileReady ? snapshotBinding.reason : "报告文件尚未生成" });
      return;
    }
    let cancelled = false;
    setReportContentState({ ok: false, reason: "报告正文正在读取" });
    api.clientReportContent(selected.id)
      .then((content) => {
        if (!cancelled) setReportContentState({ ok: true, value: content });
      })
      .catch((error) => {
        if (!cancelled) setReportContentState({ ok: false, reason: error instanceof Error ? error.message : "报告正文暂未返回" });
      });
    return () => {
      cancelled = true;
    };
  }, [fileReady, selected?.id, snapshotBinding.matched, snapshotBinding.reason]);

  async function copySummary() {
    if (!selected || !formalReportReady) {
      message.warning("当前没有可复制的正式报告摘要");
      return;
    }
    const text = `${cleanBusinessText(selected?.title)}\n${cleanBusinessText(selected?.summary)}\n${primaryConclusion(data, live)}`;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      message.success("摘要已复制");
    } catch {
      message.warning("浏览器暂时无法写入剪贴板，请手动复制报告摘要");
    }
  }

  function openReportExport() {
    if (!downloadUrl) {
      message.warning(cleanBusinessText(snapshotBinding.matched ? selected?.disabled_reason || "报告文件尚未生成或未开放客户下载" : snapshotBinding.reason));
      return;
    }
    window.open(downloadUrl, "_blank", "noopener,noreferrer");
  }

  function focusReportPreview() {
    document.querySelector("[data-testid='report-source-content']")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    message.success(reportContentState.ok ? "报告预览已定位" : cleanBusinessText(reportContentState.reason, "当前展示摘要预览"));
  }

  const reportControls = (
        <div className="report-workspace-switch" data-testid="report-workspace-switch">
          <Segmented
            options={[{ label: "研判报告", value: "reports" }, { label: "预测账本与到期复盘", value: "ledger" }]}
            value={workspace}
            onChange={(value) => onWorkspaceChange(value as ReportWorkspace)}
          />
          {workspace === "reports" ? <Segmented options={reportTypes} value={type} onChange={(value) => onTypeChange(value as ReportType)} /> : null}
        </div>
  );

  return (
    <PageFrame
      title={workspace === "reports" ? "研判报告" : "预测账本与到期复盘"}
      eyebrow="客户交付材料"
      description={workspace === "reports" ? "沉淀日报、周报、复盘和专题报告，报告正文只保留客户可读业务结论。" : "查看上游成本压力判断、证据快照、到期状态和偏差原因。"}
      className="report-page"
      actions={workspace === "ledger" ? reportControls : undefined}
    >
      {workspace === "ledger" ? <PredictionLedgerView data={data} live={live} onNavigate={onNavigate} /> : (
      <>
      <InformationReports kind={type} headerActions={reportControls} />
      <details><summary>正式研判报告与历史交付</summary>
      <div className="report-layout">
        <Panel title="报告列表" subtitle="按类型筛选" icon={<FileTextOutlined />} className="report-list-panel">
          <div className="report-list">
            {visibleReports.length ? visibleReports.map((report) => {
              const binding = reportSnapshotBinding(report, data, live);
              const reportFileReady = Boolean(report.status === "ready" && report.content_status === "ready" && report.download_available === true);
              const displayStatus = pipelineStall ?? (reportFileReady && binding.matched ? "可查看" : reportFileReady ? "快照待校验" : reportStatus(report));
              return (
                <button key={report.id} className={selected?.id === report.id ? "is-active" : ""} onClick={() => setSelectedId(report.id)}>
                  <FieldTag tone={reportFileReady && binding.matched ? "success" : "warning"}>{displayStatus}</FieldTag>
                  <strong>{cleanBusinessText(report.title)}</strong>
                  <span>{cleanBusinessText(report.audience, "业务负责人")}</span>
                </button>
              );
            }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前类型暂无正式客户报告" />}
          </div>
        </Panel>
        <Panel
          title="报告预览"
          subtitle="正式交付文档结构"
          icon={<FileTextOutlined />}
          className="report-preview-panel"
          extra={
            <div className="report-actions">
              <Button disabled={!formalReportReady} onClick={focusReportPreview}>预览</Button>
              <Typography.Text type="secondary">历史交付保留原证据链；统一证明见上方信息报告，旧记录不补写新资料。</Typography.Text>
              <Button disabled={!downloadUrl} onClick={openReportExport}>{downloadUrl ? "下载原文" : fileReady && !snapshotBinding.matched ? "报告快照未通过" : "报告文件尚未生成"}</Button>
              <Button disabled={!formalReportReady} onClick={copySummary}>复制摘要</Button>
              <Button onClick={() => onNavigate("evidence")}>查看当前证据资料</Button>
            </div>
          }
        >
          <article className="report-document" data-testid="report-source-content">
            <header>
              <FieldTag tone={formalReportReady && selected ? statusTone[statusFromText(selected.status)] : "warning"}>{formalReportReady && selected ? type : "摘要预览·非正式报告"}</FieldTag>
              <Title level={2}>{formalReportReady ? cleanBusinessText(selected?.title, type) : `${type}观察摘要`}</Title>
              <p>{pipelineStall ?? (formalReportReady
                ? cleanBusinessText(selected?.summary, "正式报告已生成。")
                : "当前仅展示本轮真实输入形成的观察摘要；正式报告文件尚未生成。")}</p>
            </header>
            {formalReportReady && selected && hasFormalConclusion(data, live) ? <section className="boss-report-digest">
              <div>
                <span>300 字日报</span>
                <p>{shortBossDailyReport(data, live)}</p>
              </div>
              <div className="boss-report-side">
                <article>
                  <strong>3 条风险</strong>
                  {(reportRisks.length ? reportRisks : reportEvents.slice(0, 3).map((event) => ({
                    title: `${event.category}事件`,
                    impact: event.impact
                  }))).slice(0, 3).map((risk, index) => (
                    <p key={`report-risk-${index}-${risk.title}`}>{index + 1}. {risk.title}：{compactText("impact" in risk ? risk.impact : "", 58)}</p>
                  ))}
                </article>
                <article>
                  <strong>3 条观察信号</strong>
                  {bossActions.map((action, index) => (
                    <p key={`report-action-${index}-${action}`}>{index + 1}. {action}</p>
                  ))}
                </article>
              </div>
              <div className="boss-report-chain">
                {["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"].map((item, index, list) => {
                  const product = chainNodeProduct(item, live);
                  const available = product?.latest_price.status === "available";
                  return (
                    <span key={`report-chain-${item}`} className={available ? "has-data" : ""}>
                      <b>{item}</b>
                      <small>{available ? formatPrice(product.latest_price.value, product.latest_price.unit) : "暂无数据"}</small>
                      {index < list.length - 1 ? <i /> : null}
                    </span>
                  );
                })}
              </div>
              <ChainPressureRail live={live} />
            </section> : (
              <>
                <ObservationConclusionSummary data={data} />
                <section className="report-unavailable-state">
                  <InfoCircleOutlined />
                  <strong>{!fileReady ? "正式报告尚未生成" : !snapshotBinding.matched ? "正式报告快照尚未通过一致性校验" : "正式报告正文尚未就绪"}</strong>
                  <p>{!fileReady ? "上方为本轮真实输入形成的非正式观察摘要；正式报告仍需通过门禁并绑定同一数据快照后生成。" : !snapshotBinding.matched ? snapshotBinding.reason : ("reason" in reportContentState ? reportContentState.reason : "正式报告正文暂未返回")}</p>
                </section>
              </>
            )}
            {formalReportReady && selected && reportContentState.ok ? (
              <section className="report-source-content is-business-only">
                <h3>报告已生成，可直接转发</h3>
                <small>页面默认展示成本压力日报、风险和观察信号；原始证据材料已留档，需要时可下载核对。</small>
                <div className="report-readiness-grid">
                  <article>
                    <strong>正文状态</strong>
                    <span>{reportPreviewText(reportContentState.value.content, reportContentState.value.content_type)}</span>
                  </article>
                  <article>
                    <strong>转发重点</strong>
                    <span>方向、风险、观察信号和全链路传导已经整理成业务版摘要。</span>
                  </article>
                  <article>
                    <strong>核对方式</strong>
                    <span>{downloadUrl ? "如需原始材料，请使用下载原文按钮。" : "原始文件尚未开放下载，当前以页面摘要为准。"}</span>
                  </article>
                </div>
              </section>
            ) : selected ? <section><h3>读取状态</h3><p>{cleanBusinessText("reason" in reportContentState ? reportContentState.reason : "正式报告正文暂未返回")}</p></section> : null}
          </article>
        </Panel>
        <Panel title="报告信息" subtitle="状态、范围和可信边界" className="report-info-panel">
          <div className="decision-stats is-vertical">
            <span>报告状态</span><strong>{formalReportReady ? "可查看" : fileReady && !snapshotBinding.matched ? "快照待校验" : fileReady ? "正文未就绪" : reportStatus(selected)}</strong>
            <span>导出状态</span><strong>{downloadUrl ? "可下载" : cleanBusinessText(snapshotBinding.matched ? selected?.disabled_reason : snapshotBinding.reason, "报告文件尚未生成")}</strong>
            <span>下一步</span><strong>{cleanBusinessText(selected?.next_step, downloadUrl ? "可直接查看或下载" : "等待报告生成")}</strong>
            <span>最近更新</span><strong>{formatDate(data?.generated_at)}</strong>
            <span>报告类型</span><strong>{type}</strong>
            <span>使用边界</span><strong>仅展示客户正式报告与证据状态</strong>
            <span>报告范围</span><strong>原油、石脑油、PX、PTA、MEG、POY、DTY</strong>
            <span>证据类别</span><strong>{selected?.source_categories?.map((item) => cleanBusinessText(item)).join("、") || "随报告生成"}</strong>
          </div>
        </Panel>
      </div>
      </details>
      </>
      )}
    </PageFrame>
  );
}

function ActiveModule({
  active,
  data,
  live,
  liveLoading,
  refreshToken,
  onNavigate,
  onReload,
  sourceStatus,
  sourceChecking,
  sourceCheckError,
  onSourceCheck,
  reportWorkspace,
  reportType,
  onReportWorkspaceChange,
  onReportTypeChange
}: {
  active: ModuleId;
  data?: AgentWorkbenchData;
  live?: WorkbenchLiveData;
  liveLoading?: boolean;
  refreshToken: number;
  onNavigate: (module: ModuleId) => void;
  onReload: () => void;
  sourceStatus?: DeliverySourceAutomation;
  sourceChecking?: boolean;
  sourceCheckError?: string;
  onSourceCheck: () => void;
  reportWorkspace: ReportWorkspace;
  reportType: ReportType;
  onReportWorkspaceChange: (workspace: ReportWorkspace) => void;
  onReportTypeChange: (type: ReportType) => void;
}) {
  if (active === "overview") {
    return (
      <OverviewModule
        data={data}
        live={live}
        onNavigate={onNavigate}
        sourceStatus={sourceStatus}
        sourceChecking={sourceChecking}
        sourceCheckError={sourceCheckError}
        onSourceCheck={onSourceCheck}
      />
    );
  }
  if (active === "market") return <MarketChainModule data={data} live={live} liveLoading={liveLoading} />;
  if (active === "evidence") return <EvidenceGraphModule data={data} live={live} liveLoading={liveLoading} onReload={onReload} />;
  if (active === "workflow") return <AgentWorkflowModule onNavigate={onNavigate} />;
  if (active === "assistant") return <AiAssistantModule data={data} live={live} onNavigate={onNavigate} />;
  if (active === "intelligence") {
    return (
      <Suspense fallback={<LoadingSurface onReload={onReload} />}>
        <IntelligenceCenterPage />
      </Suspense>
    );
  }
  return <ReportModule
    data={data}
    live={live}
    onNavigate={onNavigate}
    workspace={reportWorkspace}
    type={reportType}
    onWorkspaceChange={onReportWorkspaceChange}
    onTypeChange={onReportTypeChange}
  />;
}

export function AgentWorkbenchPage() {
  const referenceMode = import.meta.env.DEV
    && typeof window !== "undefined"
    && new URLSearchParams(window.location.search).get("reference") === "overview";
  const initialNavigation = useMemo(() => readNavigationState(), []);
  const [activeModule, setActiveModule] = useState<ModuleId>(initialNavigation.module);
  const [reportWorkspace, setReportWorkspace] = useState<ReportWorkspace>(initialNavigation.reportView);
  const [reportType, setReportType] = useState<ReportType>(initialNavigation.reportType);
  const [data, setData] = useState<AgentWorkbenchData | undefined>(() => cachedWorkbenchData);
  const [liveData, setLiveData] = useState<WorkbenchLiveData | undefined>(() => cachedWorkbenchData ? cachedLiveData : undefined);
  const [liveLoading, setLiveLoading] = useState(false);
  const [loading, setLoading] = useState(!cachedWorkbenchData);
  const [snapshotResolved, setSnapshotResolved] = useState(false);
  const [error, setError] = useState<string>();
  const [lastRefreshAt, setLastRefreshAt] = useState<string>();
  const [moduleRefreshToken, setModuleRefreshToken] = useState(0);
  const [sourceStatus, setSourceStatus] = useState<DeliverySourceAutomation>();
  const [sourceChecking, setSourceChecking] = useState(false);
  const [sourceCheckError, setSourceCheckError] = useState<string>();
  const [accessMode, setAccessMode] = useState<"public" | "single_user_password" | "unknown">("unknown");
  useEffect(() => { void api.accessMode().then(setAccessMode); }, []);
  const didInitialLoad = useRef(false);
  const loadRequestSequence = useRef(0);
  const liveRequestSequence = useRef(0);
  const activeModuleRef = useRef(activeModule);
  const dataRef = useRef(data);
  const liveDataRef = useRef(liveData);

  useEffect(() => {
    activeModuleRef.current = activeModule;
  }, [activeModule]);

  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  function commitLiveData(next: WorkbenchLiveData) {
    // Keep the refresh rollback reference in lockstep with the state update;
    // a passive effect can lag one paint behind a quote that is already
    // visible and let an immediate operator click capture the older snapshot.
    liveDataRef.current = next;
    setLiveData(next);
  }

  useEffect(() => {
    if (referenceMode) return;
    const canonicalHref = navigationHref(initialNavigation.module, initialNavigation.reportView, initialNavigation.reportType);
    const currentHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (currentHref !== canonicalHref) {
      window.history.replaceState(null, "", canonicalHref);
    }
  }, [initialNavigation, referenceMode]);

  const navigateToModule = useCallback((module: ModuleId, options?: { reportView?: ReportWorkspace; reportType?: ReportType }) => {
    const nextView = options?.reportView ?? reportWorkspace;
    writeNavigationState(module, nextView, options?.reportType ?? reportType, "push");
    if (options?.reportView && options.reportView !== reportWorkspace) setReportWorkspace(options.reportView);
    if (options?.reportType && options.reportType !== reportType) setReportType(options.reportType);
    // Invalidate the previous module's background request immediately. Its
    // successful fields may still populate the shared cache, but it must not
    // commit UI/loading state for the newly selected module.
    liveRequestSequence.current += 1;
    setLiveLoading(true);
    setActiveModule(module);
  }, [reportType, reportWorkspace]);

  const changeReportWorkspace = useCallback((workspace: ReportWorkspace) => {
    writeNavigationState("reports", workspace, reportType, "push");
    setActiveModule("reports");
    setReportWorkspace(workspace);
  }, [reportType]);

  const changeReportType = useCallback((type: ReportType) => {
    writeNavigationState("reports", "reports", type, "push");
    setActiveModule("reports");
    setReportWorkspace("reports");
    setReportType(type);
  }, []);

  async function loadWorkbench(force = false) {
    if (!force && cachedWorkbenchData) return cachedWorkbenchData;
    if (workbenchRequest) {
      if (!force) return workbenchRequest;
      try {
        await workbenchRequest;
      } catch {
        // A forced refresh must issue a new request even when the initial
        // background request failed while the audited snapshot stayed visible.
      }
    }
    workbenchRequest = (async () => {
      const payload = await api.agentWorkbench();
      cachedWorkbenchData = payload;
      return payload;
    })();
    try {
      return await workbenchRequest;
    } finally {
      workbenchRequest = undefined;
    }
  }

  async function loadModule(
    module: ModuleId,
    force = false,
    committedFallback?: WorkbenchLiveData,
    perFieldCommit = false
  ) {
    const requestSequence = ++liveRequestSequence.current;
    setLiveLoading(true);
    try {
      const live = await fetchLiveDataForModule(module, force, committedFallback, perFieldCommit, (progress) => {
        if (requestSequence === liveRequestSequence.current) commitLiveData(progress);
      });
      if (requestSequence === liveRequestSequence.current) commitLiveData({ ...live });
    } finally {
      if (requestSequence === liveRequestSequence.current) setLiveLoading(false);
    }
  }

  async function load(force = false, refreshLists = true) {
    const requestSequence = ++loadRequestSequence.current;
    if (force) {
      if (refreshLists) setModuleRefreshToken((token) => token + 1);
      // Revoke any initial/background module request before the first await.
      // Otherwise it can commit between the operator click and the forced
      // module request, changing the rollback baseline mid-refresh.
      liveRequestSequence.current += 1;
    }
    const committedLiveData = liveDataRef.current;
    // Refresh an already displayed module in place, even when it is using
    // runtime data without a daily snapshot; unmounting loses its filters.
    setLoading(!dataRef.current && (!force || loading));
    setError(undefined);
    try {
      // Independent reads start together; a missing daily snapshot must not
      // add 15 seconds before the runtime workbench can even begin.
      const runtimeRequest = Promise.all([
        loadWorkbench(force), api.sourceAutomationStatus().catch(() => undefined)
      ]);
      void runtimeRequest.catch(() => undefined);
      const snapshotAvailable = await loadServerSnapshot(force);
      if (requestSequence !== loadRequestSequence.current) return;
      setSnapshotResolved(true);
      if (snapshotAvailable) {
        // Render the immutable daily evidence immediately. The heavier runtime
        // workbench and live evidence requests continue in the background.
        commitLiveData({ ...cachedLiveData });
        setLoading(false);
        setLastRefreshAt(new Date().toISOString());
        void loadModule(activeModuleRef.current, force, force ? committedLiveData : undefined);
      }
      const previousWorkbenchData = dataRef.current ?? cachedWorkbenchData;
      const [payload, latestSourceStatus] = await runtimeRequest;
      if (requestSequence !== loadRequestSequence.current) return;
      // Any failed bundle sub-request counts as a degraded refresh: when the
      // delivery endpoint is healthy its server-side source_mode ("live")
      // overrides the client-computed partial marker, so gating on mode alone
      // would silently skip recovery while one sub-request failed.
      if (payload.errors.length) {
        if (previousWorkbenchData) {
          cachedWorkbenchData = previousWorkbenchData;
          if (!dataRef.current) setData(previousWorkbenchData);
        } else {
          // A partial response is still real data. Do not discard all successful
          // fields just because an auxiliary endpoint failed during cold boot.
          setData(payload);
          setLoading(false);
        }
        // A partial fallback must still let the module's own live requests run:
        // market prices and the chain are the page's primary data and one slow
        // auxiliary sub-request must not freeze them on a degraded banner. The
        // recovery pass forces real requests but commits per field — a failed
        // sibling keeps its last good value instead of rolling back the whole
        // module. Run it inside this load() call and commit under its sequence
        // guard: a fire-and-forget loadModule would lose a race against the
        // effect-driven non-forced module load invalidating its sequence.
        try {
          const recovered = await fetchLiveDataForModule(activeModuleRef.current, true, committedLiveData, true);
          if (requestSequence === loadRequestSequence.current) commitLiveData(recovered);
        } catch {
          // keep the last good module view
        }
        setError("本次刷新未能读取完整后端数据");
        setLoading(false);
        setLastRefreshAt(new Date().toISOString());
        return;
      }
      setData(payload);
      commitLiveData({ ...cachedLiveData });
      if (latestSourceStatus) setSourceStatus(latestSourceStatus);
      setLastRefreshAt(new Date().toISOString());
      setLoading(false);
    } catch (nextError) {
      if (requestSequence !== loadRequestSequence.current) return;
      setError(nextError instanceof Error ? nextError.message : "后端服务暂时不可用。");
      setLastRefreshAt(new Date().toISOString());
      setLoading(false);
    }
  }

  async function checkSources() {
    setSourceChecking(true);
    setSourceCheckError(undefined);
    try {
      const status = await api.sourceAutomationStatus();
      setSourceStatus(status);
      await load();
    } catch (nextError) {
      setSourceCheckError(nextError instanceof Error ? nextError.message : "信息源状态暂时无法读取。");
    } finally {
      setSourceChecking(false);
    }
  }

  useEffect(() => {
    if (referenceMode) {
      setLoading(false);
      return;
    }
    if (didInitialLoad.current) return;
    didInitialLoad.current = true;
    void load();
  }, [referenceMode]);

  // Overview keeps its existing aggregate readiness rule. Other consumers can
  // start once the canonical snapshot resolves; evidence keeps its early path.
  const moduleBootstrapReady = activeModule === "overview"
    ? Boolean(data || cachedWorkbenchData)
    : snapshotResolved || activeModule === "evidence";
  useEffect(() => {
    if (referenceMode || !didInitialLoad.current || !moduleBootstrapReady) return;
    commitLiveData({ ...cachedLiveData });
    void loadModule(activeModule);
  }, [activeModule, data, referenceMode, moduleBootstrapReady]);

  useEffect(() => {
    if (referenceMode) return undefined;
    writeNavigationState(activeModule, reportWorkspace, reportType);
    return undefined;
  }, [activeModule, referenceMode, reportType, reportWorkspace]);

  useEffect(() => {
    if (referenceMode) return undefined;
    const restoreFromUrl = () => {
      const next = readNavigationState();
      liveRequestSequence.current += 1;
      setLiveLoading(true);
      setActiveModule(next.module);
      setReportWorkspace(next.reportView);
      setReportType(next.reportType);
    };
    window.addEventListener("popstate", restoreFromUrl);
    return () => window.removeEventListener("popstate", restoreFromUrl);
  }, [referenceMode]);

  useEffect(() => {
    if (referenceMode) return undefined;
    const timer = window.setInterval(() => {
      void load(true, false);
    }, 5 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [referenceMode]);

  const current = navigationItems.find((item) => item.id === activeModule) ?? navigationItems[0];
  const todayLabel = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", weekday: "short" }).format(new Date());

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#2563eb",
          colorSuccess: "#16a34a",
          colorWarning: "#d97706",
          colorError: "#dc2626",
          borderRadius: 8,
          fontSize: 13,
          fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"
        }
      }}
    >
      <AntApp>
        {referenceMode ? <ReferenceOverviewDashboard /> : (
        <div className="delivery-workbench">
          <a className="skip-link" href="#main-content">跳到主内容</a>
          <aside className="delivery-sidebar" aria-label="客户模块导航">
            <WorkbenchBrand />
            <nav className="delivery-nav">
              {navigationItems.map((item) => (
                <a
                  key={item.id}
                  href={navigationHref(item.id, reportWorkspace, reportType)}
                  className={item.id === activeModule ? "is-active" : ""}
                  onClick={(event) => {
                    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                    event.preventDefault();
                    navigateToModule(item.id);
                  }}
                  aria-label={item.label}
                  aria-current={item.id === activeModule ? "page" : undefined}
                >
                  <i>{item.icon}</i>
                  <span>{item.label}</span>
                  <small>{item.description}</small>
                </a>
              ))}
            </nav>
            <div className="delivery-side-status review-sidebar-footer">
              <div className="review-sidebar-state"><FieldTag tone="info">{todayLabel}</FieldTag><FieldTag tone={loading ? "info" : error ? "danger" : operationalTone(data)}>{loading ? "读取中" : error ? "读取失败" : data?.operational_status === "ready" ? "运行正常" : "运行需关注"}</FieldTag></div>
              <dl><div><dt>页面快照</dt><dd>{formatDate(updatedAt(data))}</dd></div><div><dt>本次读取</dt><dd>{lastRefreshAt ? formatDate(lastRefreshAt) : "等待首次读取"}</dd></div></dl>
              <Button size="small" icon={<ReloadOutlined />} onClick={() => void load(true)} loading={loading}>刷新数据</Button>
              {accessMode === "single_user_password" ? <Button size="small" icon={<LogoutOutlined />} onClick={() => void api.logout()}>退出</Button> : null}
            </div>
          </aside>
          <div className="delivery-shell">
            <main id="main-content" tabIndex={-1} aria-busy={liveLoading} className={`delivery-content ${((loading || error) && !data) ? "has-status" : ""}`}>
              {liveLoading && data ? (
                <div className="module-sync-indicator" role="status" aria-live="polite">
                  <span aria-hidden="true" />
                  正在同步“{current.label}”最新数据，已有内容可继续查看
                </div>
              ) : null}
              {loading && !data && activeModule !== "intelligence" && activeModule !== "reports" && activeModule !== "workflow" && activeModule !== "evidence" ? <LoadingSurface onReload={() => void load(true)} /> : null}
              {!loading && error && !data && activeModule !== "intelligence" && activeModule !== "reports" && activeModule !== "workflow" && activeModule !== "evidence" ? <ErrorSurface message={error} onReload={() => void load(true)} /> : null}
              <WorkbenchDegradedBanner data={data} live={liveData} refreshError={error} onReload={() => void load(true)} />
              {data || activeModule === "intelligence" || activeModule === "reports" || activeModule === "workflow" || activeModule === "evidence" || (!loading && !error) ? <ActiveModule
                active={activeModule}
                data={data}
                live={liveData}
                liveLoading={liveLoading}
                refreshToken={moduleRefreshToken}
                onNavigate={navigateToModule}
                onReload={() => void load(true)}
                sourceStatus={sourceStatus}
                sourceChecking={sourceChecking}
                sourceCheckError={sourceCheckError}
                onSourceCheck={checkSources}
                reportWorkspace={reportWorkspace}
                reportType={reportType}
                onReportWorkspaceChange={changeReportWorkspace}
                onReportTypeChange={changeReportType}
              /> : null}
            </main>
          </div>
        </div>
        )}
      </AntApp>
    </ConfigProvider>
  );
}
