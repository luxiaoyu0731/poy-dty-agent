import type { DailyRagEvalResult, EvidenceQueueResponse } from "../services/api";
import { EvidenceList } from "./domain";
import { StatusPill } from "./ui";

export function EvidenceQueuePanel({
  queue,
  status,
  onReview,
  showSummary = true
}: {
  queue: EvidenceQueueResponse | null;
  status: string;
  onReview: (docId: string, nextStatus: "reviewed" | "rejected") => void;
  showSummary?: boolean;
}) {
  return (
    <>
      {showSummary ? (
        <div className="queue-summary">
          <StatusPill tone="accent">{status}</StatusPill>
          <span>待审 {queue?.counts.unreviewed ?? 0}</span>
          <span>已确认 {queue?.counts.reviewed ?? 0}</span>
          <span>已拒绝 {queue?.counts.rejected ?? 0}</span>
        </div>
      ) : null}
      <EvidenceList items={queue?.items ?? []} onReview={onReview} />
    </>
  );
}

export function RagQualityGate({ evalResult }: { evalResult: DailyRagEvalResult | null }) {
  if (!evalResult) {
    return (
      <p className="muted-copy">
        运行后会检查成本链证据、来源边界、地缘风险、预测复盘、引用覆盖率和已拒绝证据排除。
      </p>
    );
  }

  return (
    <div className="eval-list">
      <strong>{evalResult.suite}: {evalResult.passed}/{evalResult.total}</strong>
      {evalResult.results.map((item) => (
        <article key={String(item.name)}>
          <span>{String(item.name)}</span>
          <StatusPill tone={item.passed ? "good" : "bad"}>{item.passed ? "通过" : "需处理"}</StatusPill>
        </article>
      ))}
    </div>
  );
}
