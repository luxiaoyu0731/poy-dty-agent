import { useCallback, useEffect, useRef, useState } from "react";
import * as intelligenceApi from "./api";
import type { EventSummary, RunSummary, SourceCatalogEntry } from "./api";

export type DataStatus<T> =
  | { ok: true; data: T; fetchedAt: string }
  | { ok: false; reason: string; pending?: boolean };

function nowIso(): string {
  return new Date().toISOString();
}

/** Module-scoped fetcher: stale responses are ignored after view switches. */
export function useStaleWhileRevalidate<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): { status: DataStatus<T>; reload: () => void; loading: boolean } {
  const [status, setStatus] = useState<DataStatus<T>>({ ok: false, reason: "尚未加载", pending: true });
  const [attempt, setAttempt] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setStatus({ ok: false, reason: "正在加载", pending: true });
    fetcherRef
      .current(controller.signal)
      .then((data) => {
        if (!cancelled) setStatus({ ok: true, data, fetchedAt: nowIso() });
      })
      .catch((error: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        const reason = error instanceof Error ? error.message : "加载失败";
        setStatus({ ok: false, reason });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  return { status, reload, loading: "pending" in status && status.pending === true };
}

export function useRadar(
  filters: { product: string | null; category: string | null; sort: "relevance" | "recency" },
): {
  status: DataStatus<intelligenceApi.SnapshotPage<EventSummary>>;
  reload: () => void;
  loading: boolean;
  loadingMore: boolean;
  moreError: string | null;
  loadMore: () => void;
} {
  const [status, setStatus] = useState<DataStatus<intelligenceApi.SnapshotPage<EventSummary>>>({ ok: false, reason: "正在加载", pending: true });
  const [attempt, setAttempt] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const loadingMoreRef = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    setMoreError(null);
    setStatus({ ok: false, reason: "正在加载", pending: true });
    intelligenceApi.listEvents({ limit: 30, ...filters }, controller.signal).then((data) => {
      if (!controller.signal.aborted) setStatus({ ok: true, data, fetchedAt: nowIso() });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setStatus({ ok: false, reason: error instanceof Error ? error.message : "加载失败" });
    });
    return () => controller.abort();
  }, [filters.product, filters.category, filters.sort, attempt]);

  const loadMore = () => {
    const controller = controllerRef.current;
    if (!status.ok || !status.data.has_more || !status.data.next_cursor || !controller || controller.signal.aborted || loadingMoreRef.current) return;
    const previous = status.data;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setMoreError(null);
    intelligenceApi.listEvents({ limit: 30, ...filters, cursor: previous.next_cursor }, controller.signal).then((page) => {
      if (controller.signal.aborted) return;
      const items = [...previous.items, ...page.items];
      setStatus({ ok: true, data: { ...page, items: items.filter((item, index) => items.findIndex((other) => other.event_revision_id === item.event_revision_id) === index) }, fetchedAt: nowIso() });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setMoreError(error instanceof Error ? error.message : "下一页加载失败");
    }).finally(() => {
      if (!controller.signal.aborted) { loadingMoreRef.current = false; setLoadingMore(false); }
    });
  };
  return { status, reload: () => setAttempt((value) => value + 1), loading: !status.ok && status.pending === true, loadingMore, moreError, loadMore };
}

export function useSources(): {
  status: DataStatus<intelligenceApi.SnapshotPage<SourceCatalogEntry>>;
  reload: () => void;
  loading: boolean;
} {
  return useStaleWhileRevalidate((signal) => intelligenceApi.listSources(200, signal), []);
}

export function useRuns(): {
  status: DataStatus<intelligenceApi.SnapshotPage<RunSummary>>;
  reload: () => void;
  loading: boolean;
} {
  return useStaleWhileRevalidate((signal) => intelligenceApi.listRuns(30, signal), []);
}

export function newFeedbackRequestId(): string {
  const cryptoObject = globalThis.crypto as Crypto | undefined;
  if (cryptoObject?.randomUUID) return cryptoObject.randomUUID();
  return `fb-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
