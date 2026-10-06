import type { EvidenceSemanticReview } from "../evidence-system/types";

/**
 * Isolated API client for the industrial intelligence module.
 *
 * Deliberately does not import `services/api.ts`: the intelligence module
 * keeps its own types, client, and cache so the seven existing modules'
 * refresh graph and snapshot keys are untouched (spec 13.1). Conventions
 * mirror the shared client: same-origin `/api/v1`, cookie credentials, one
 * local-session bootstrap, timeout on every request, CSRF header on writes.
 */

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "") as string;
const API_PREFIX = (import.meta.env.VITE_API_PREFIX ?? "/api/v1") as string;

export const INTELLIGENCE_SCHEMA_VERSION = "industrial-intelligence.v1";

let localSessionPromise: Promise<void> | null = null;

async function ensureLocalSession(): Promise<void> {
  if (!localSessionPromise) {
    localSessionPromise = fetch(`${API_BASE}${API_PREFIX}/auth/local-session`, {
      method: "POST",
      credentials: "include",
    }).then(
      () => undefined,
      () => undefined,
    );
  }
  return localSessionPromise;
}

function publicCsrfToken(): string {
  const prefix = "__Host-poy_dty_csrf=";
  for (const item of document.cookie.split(";")) {
    const candidate = item.trim();
    if (candidate.startsWith(prefix)) return candidate.slice(prefix.length);
  }
  return "";
}

const DEFAULT_TIMEOUT_MS = 15_000;

function isTimeoutError(error: unknown): boolean {
  return error instanceof Error && error.name === "TimeoutError";
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason instanceof Error ? signal.reason : new Error("请求已取消"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(signal?.reason instanceof Error ? signal.reason : new Error("请求已取消"));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function requestOnce<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
  await ensureLocalSession();
  const controller = new AbortController();
  const callerSignal = init.signal;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  const timer = window.setTimeout(() => {
    // Abort with a named reason: without it fetch rejects with the opaque
    // "signal is aborted without reason" DOMException that used to surface
    // verbatim in the radar error card.
    const timeoutError = new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒），服务端仍在处理；请稍后重试。`);
    timeoutError.name = "TimeoutError";
    controller.abort(timeoutError);
  }, timeoutMs);
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = publicCsrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  try {
    const response = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      credentials: "include",
      signal: controller.signal,
    });
    if (!response.ok) {
      let code = `HTTP_${response.status}`;
      let message = `请求失败（${response.status}）`;
      try {
        const envelope = (await response.json()) as { error?: { code?: string; message?: string } };
        code = envelope.error?.code ?? code;
        message = envelope.error?.message ?? message;
      } catch {
        // non-JSON error body; keep status-based defaults
      }
      if (response.status === 401 || response.status === 403) message = `接口拒绝访问（HTTP ${response.status}），请刷新页面重试。`;
      else if (response.status >= 500) message = `服务暂时不可用（HTTP ${response.status}），请稍后重试。`;
      const error = new Error(message) as Error & { code?: string; status?: number };
      error.code = code;
      error.status = response.status;
      throw error;
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof TypeError) throw new Error("网络连接失败，请检查网络后重试。");
    throw error;
  } finally {
    window.clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

// Idempotent reads get one automatic retry so a slow snapshot (radar lists
// regularly need 20-30s in production) or a dropped connection recovers
// without turning the whole panel into an error card.
const GET_RETRY_DELAYS_MS = [1_500];

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const mayRetry = method === "GET" || method === "HEAD";
  const callerSignal = init.signal ?? undefined;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await requestOnce<T>(path, init, timeoutMs);
    } catch (error) {
      const recoverable = error instanceof TypeError || isTimeoutError(error);
      if (!mayRetry || callerSignal?.aborted || !recoverable || attempt >= GET_RETRY_DELAYS_MS.length) throw error;
      await delay(GET_RETRY_DELAYS_MS[attempt], callerSignal);
    }
  }
}

export type SnapshotPage<T> = {
  schema_version: string;
  snapshot_at: string;
  snapshot_id: string;
  items: T[];
  has_more: boolean;
  next_cursor: string | null;
  applied_filters: Record<string, string>;
};

export type RightsSummary = {
  rights_policy_version: string;
  storage_mode: "metadata_only" | "link_excerpt" | "full_content" | "operator_supplied";
  display_scope: "link_only" | "metadata" | "excerpt" | "full";
  cache_mode: "none" | "ephemeral" | "bounded" | "long_term";
  commercial_use_status: "allowed" | "restricted" | "unknown";
  redistribution_status: "allowed" | "restricted" | "unknown";
  attribution_required: boolean;
  retention_class: string | null;
  retention_days: number | null;
};

export type SourceCatalogEntry = {
  source_id: string;
  display_name: string;
  source_type: string;
  tier: "A" | "B" | "C" | "D";
  categories: string[];
  capabilities: string[];
  cadence: string;
  cost_status: "free" | "optional_paid" | "paid" | "unknown";
  credential_status: "not_required" | "configured" | "missing" | "invalid" | "unknown";
  operational_status: string;
  rights_summary: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  quality_status: string;
  metadata_drift: boolean;
  drift_fields: string[];
};

export type ItemRevision = {
  item_id: string;
  item_revision_id: string;
  revision_no: number;
  revision_kind: "upsert" | "invalidate" | "tombstone";
  canonical_url: string;
  origin_url: string | null;
  title: string | null;
  excerpt: string | null;
  category: string;
  product_ids: string[];
  region_codes: string[];
  geometry: { type: string; coordinates: number[] } | null;
  location_precision: string | null;
  occurred_at: string | null;
  published_at: string | null;
  published_date?: string | null;
  first_seen_at: string;
  retrieved_at: string;
  visible_at: string;
  collector_source_id: string;
  aggregator_source_id: string | null;
  origin_source_id: string | null;
  origin_group_id: string;
  source_tier: "A" | "B" | "C" | "D";
  rights: RightsSummary;
  content_status: "absent" | "available" | "expired" | "rights_withdrawn";
  prediction_eligible: false;
  instruction_eligible: false;
  payload_sha256: string;
  presentation_status: "full" | "redacted";
  redactions: { scope: string; target_id: string; reason_code: string }[];
};

export type Claim = { claim_id: string; text: string; evidence_link_ids: string[]; published_at?: string | null; published_date?: string | null };

export type Inference = {
  inference_id: string;
  text: string;
  basis_claim_ids: string[];
  assumptions: string[];
  confidence: number;
  counterevidence_claim_ids: string[];
};

export type HorizonImpact = {
  product_id: string;
  horizon: "D1" | "D7" | "D30";
  direction: "upward_pressure" | "downward_pressure" | "mixed" | "unclear";
  confidence: number;
  basis_claim_ids: string[];
  gaps: string[];
};

export type WatchItem = {
  watch_id: string;
  observable_condition: string;
  product_ids: string[];
  horizon: "D1" | "D7" | "D30";
};

export type Gap = { code: string; scope: string; message_safe: string };

export type EventSummary = {
  event_id: string;
  event_revision_id: string;
  revision_no: number;
  status: "open" | "monitoring" | "resolved" | "retracted";
  title: string;
  overview_text?: string | null;
  category: string;
  region_codes: string[];
  product_ids: string[];
  last_seen_at: string;
  as_of_time: string;
  relevance_score: number;
  severity_score: number;
  urgency_score: number;
  confidence: number;
  location_precision: string | null;
  evidence_count: number;
  gap_count: number;
  payload_sha256: string;
};

export type EventDetail = EventSummary & {
  semantic_reviews?: EvidenceSemanticReview[];
  semantic_review_as_of?: string | null;
  facts: Claim[];
  inferences: Inference[];
  counterevidence: Claim[];
  supply_chain_paths: { path_id: string; node_ids: string[]; basis_claim_ids: string[]; explanation: string }[];
  horizon_impact: HorizonImpact[];
  watch_items: WatchItem[];
  gaps: Gap[];
  revision_count: number;
  revisions_url: string;
  evidence_url: string;
  presentation_status: "full" | "redacted";
  redactions: { scope: string; target_id: string; reason_code: string }[];
};

export type EvidenceLink = {
  evidence_link_id: string;
  event_revision_id: string;
  item_revision_id: string;
  claim_id: string;
  evidence_role: "fact" | "corroboration" | "counterevidence" | "discovery" | "location";
  origin_group_id: string;
  independent_corroboration: boolean;
  citation_label: string;
  source_tier: "A" | "B" | "C" | "D";
  canonical_url: string;
  payload_sha256: string;
};

export type CoverageDomain = { covered: boolean; source_ids: string[]; gap_codes: string[] };

export type RunSummary = {
  run_id: string;
  run_type: "provider" | "projection" | "clustering" | "analysis" | "brief";
  provider_id: string | null;
  business_date: string | null;
  started_at: string;
  finished_at: string | null;
  status: "succeeded" | "degraded" | "failed" | "cancelled";
  duration_ms: number;
  counts: { input: number; inserted: number; existing: number; revised: number; rejected: number };
  degraded_reasons: string[];
  error_code: string | null;
  error_detail_safe: string | null;
  input_sha256: string | null;
  output_sha256: string | null;
};

export type MapFeatureCollection = {
  schema_version: string;
  snapshot_at: string;
  snapshot_id: string;
  type: "FeatureCollection";
  features: {
    type: "Feature";
    id: string;
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: {
      event_id: string;
      title: string;
      category: string;
      product_ids: string[];
      relevance_score: number;
      status: string;
      location_precision: string;
      location_confidence: number;
      as_of_time: string;
      detail_url: string;
      cluster_count: number;
    };
  }[];
  applied_filters: Record<string, string>;
};

export function listSources(limit = 200, signal?: AbortSignal): Promise<SnapshotPage<SourceCatalogEntry>> {
  return request(`/intelligence/sources?limit=${limit}`, { signal });
}

export function listEvents(params: {
  limit?: number;
  cursor?: string | null;
  product?: string | null;
  category?: string | null;
  min_relevance?: number | null;
  sort?: "relevance" | "recency";
}, signal?: AbortSignal): Promise<SnapshotPage<EventSummary>> {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.product) query.set("product", params.product);
  if (params.category) query.set("category", params.category);
  if (params.min_relevance != null) query.set("min_relevance", String(params.min_relevance));
  if (params.sort) query.set("sort", params.sort);
  // Radar snapshot pages are the slowest read in the module (regularly 20-30s
  // in production); 12s used to abort them mid-flight on every cold load.
  return request(`/intelligence/events?${query.toString()}`, { signal }, 30_000);
}

export function getEvent(eventId: string, signal?: AbortSignal): Promise<EventDetail> {
  return request(`/intelligence/events/${encodeURIComponent(eventId)}`, { signal }, 20_000);
}

// 审计 A5：事件修订历史端点此前无前端消费——情报事件只能看快照，看不到演变。
export function listEventRevisions(
  eventId: string,
  limit = 5,
  signal?: AbortSignal
): Promise<SnapshotPage<EventDetail>> {
  return request(
    `/intelligence/events/${encodeURIComponent(eventId)}/revisions?limit=${limit}`,
    { signal },
    20_000
  );
}

export type SearchHit = {
  ref_type: "item" | "event";
  ref_id: string;
  title: string;
  category: string;
  detail_url: string;
  payload_sha256: string;
};

export function searchEvents(
  query: string,
  options: { limit?: number; cursor?: string | null } = {},
  signal?: AbortSignal,
): Promise<SnapshotPage<SearchHit>> {
  const params = new URLSearchParams({ q: query, limit: String(options.limit ?? 30), types: "event" });
  if (options.cursor) params.set("cursor", options.cursor);
  return request(`/intelligence/search?${params.toString()}`, { signal }, 30_000);
}

export function listEvidence(
  eventId: string,
  limit = 200,
  signal?: AbortSignal,
): Promise<SnapshotPage<EvidenceLink>> {
  return request(`/intelligence/events/${encodeURIComponent(eventId)}/evidence?limit=${limit}`, { signal }, 20_000);
}

export function listRuns(limit = 30, signal?: AbortSignal): Promise<SnapshotPage<RunSummary>> {
  return request(`/intelligence/runs?limit=${limit}`, { signal });
}

export function listItems(params: { limit?: number; cursor?: string | null }): Promise<SnapshotPage<ItemRevision>> {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);
  return request(`/intelligence/items?${query.toString()}`);
}

export function getMap(bbox: string, zoom: number, signal?: AbortSignal): Promise<MapFeatureCollection> {
  const query = new URLSearchParams({ bbox, zoom: String(Math.round(zoom)) });
  return request(`/intelligence/map?${query.toString()}`, { signal }, 15_000);
}

export async function postFeedback(payload: {
  client_request_id: string;
  target_type: "item" | "event" | "source" | "topic";
  target_id: string;
  action: "relevant" | "irrelevant" | "duplicate" | "watch" | "unwatch" | "mute" | "unmute";
  reason?: string | null;
}): Promise<{ feedback_id: string; replayed: boolean }> {
  return request("/intelligence/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": payload.client_request_id },
    body: JSON.stringify(payload),
  });
}

export function postProjectionNews(): Promise<{ run_id: string; status: string }> {
  return request("/intelligence/projection/news", { method: "POST" }, 60_000);
}
