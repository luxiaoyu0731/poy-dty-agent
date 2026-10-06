import { useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import type { FetchResult, ImportResult, SourceReadiness } from "../services/api";
import { api } from "../services/api";
import { evidenceLevelLabel, sourceNameLabel, statusLabel } from "./displayLabels";
import { cleanText, readinessLabel } from "./researchUi";
import { Button, EmptyState, StatusPill } from "./ui";

const importKindLabels: Record<string, string> = {
  "public-observations": "公开行情观测",
  "industry-observations": "行业价格观测",
  events: "事件输入",
  "news-observations": "新闻文章"
};

export function CsvImportCard({
  kind,
  title,
  placeholder,
  onImported
}: {
  kind: "public-observations" | "industry-observations" | "events" | "news-observations";
  title: string;
  placeholder: string;
  onImported: () => void;
}) {
  const [csvText, setCsvText] = useState("");
  const [result, setResult] = useState<ImportResult | { error: string } | null>(null);
  const [importing, setImporting] = useState(false);

  const submit = async () => {
    if (!csvText.trim()) return;
    setImporting(true);
    try {
      const next = await api.importCsv(kind, csvText);
      setResult(next);
      setCsvText("");
      onImported();
    } catch (error) {
      setResult({ error: error instanceof Error ? error.message : "导入失败" });
    } finally {
      setImporting(false);
    }
  };

  return (
    <article className="csv-card">
      <div>
        <strong>{title}</strong>
        <StatusPill tone="neutral">{importKindLabels[kind]}</StatusPill>
      </div>
      <textarea onChange={(event) => setCsvText(event.target.value)} placeholder={placeholder} value={csvText} />
      <Button disabled={!csvText.trim() || importing} icon={<Database size={15} />} onClick={() => void submit()} tone="accent">
        {importing ? "导入中" : "导入 CSV"}
      </Button>
      {result ? (
        "error" in result ? (
          <small className="error-copy">{cleanText(result.error, "导入失败，请检查 CSV 内容。")}</small>
        ) : (
          <small>接收 {result.accepted} · 拒绝 {result.rejected} · 证据版本 {result.data_snapshot_id ? "已生成" : "待生成"}</small>
        )
      ) : null}
    </article>
  );
}

export function CsvImportGrid({ onImported }: { onImported: () => void }) {
  return (
    <div className="csv-grid">
      <CsvImportCard
        kind="public-observations"
        onImported={onImported}
        placeholder="粘贴公开行情 CSV；字段说明见下方模板入口"
        title="公开观测"
      />
      <CsvImportCard
        kind="industry-observations"
        onImported={onImported}
        placeholder="粘贴 POY/DTY/PX/PTA/MEG 行业价格 CSV"
        title="行业观测"
      />
      <CsvImportCard
        kind="events"
        onImported={onImported}
        placeholder="粘贴手工事件 CSV；保留来源、时间、摘要和影响品种"
        title="事件输入"
      />
      <CsvImportCard
        kind="news-observations"
        onImported={onImported}
        placeholder="粘贴新闻补录 CSV；需要标题、来源链接、发布时间和摘要"
        title="新闻文章"
      />
    </div>
  );
}

export function SingleSourceFetchList({
  fetching,
  readiness,
  sourceResult,
  onFetchOne
}: {
  fetching: boolean;
  readiness: SourceReadiness[];
  sourceResult: Record<string, FetchResult | { error: string }>;
  onFetchOne: (sourceId: string) => void;
}) {
  if (!readiness.length) {
    return <EmptyState title="暂无来源状态">来源登记完成后会显示更新状态。</EmptyState>;
  }
  return (
    <div className="single-source-list">
      {readiness.slice(0, 12).map((source) => {
        const result = sourceResult[source.source_id];
        return (
          <article key={source.source_id}>
            <div>
              <strong>{sourceNameLabel(source.source_id)}</strong>
              <StatusPill tone={source.status === "ready" ? "good" : "warn"}>{readinessLabel(source.status)}</StatusPill>
            </div>
            <p>{source.status === "ready" ? "可用于更新研究数据。" : actionCopy(source.status)}</p>
            <small>{evidenceLevelLabel(source.tier)}</small>
            <Button disabled={fetching || source.status !== "ready"} icon={<RefreshCw size={15} />} onClick={() => onFetchOne(source.source_id)}>
              拉取
            </Button>
            {result ? (
              "error" in result ? <small className="error-copy">{cleanText(result.error, "拉取失败，请稍后重试。")}</small> : <small>{statusLabel(result.status)} · 入库 {result.stored_observations}</small>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

function actionCopy(status: SourceReadiness["status"]) {
  if (status === "requires_api_key" || status === "requires_license") return "需要补充授权信息后才能自动更新。";
  if (status === "internal_only") return "需要人工录入或本地资料补充。";
  if (status === "manual_review") return "需要复核来源口径。";
  return "来源状态待确认。";
}
