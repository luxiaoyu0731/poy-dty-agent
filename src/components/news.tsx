import { FileText } from "lucide-react";
import type { NewsArticle, NewsFetchRun } from "../services/api";
import { categoryLabel, evidenceLevelLabel, statusLabel } from "./displayLabels";
import { toneForTier } from "./domain";
import { articleChineseSummary } from "./newsSummary";
import { cleanText, displaySourceName } from "./researchUi";
import { EmptyState, StatusPill } from "./ui";

export function NewsRunList({ runs }: { runs: NewsFetchRun[] }) {
  if (!runs.length) {
    return <EmptyState title="暂无更新记录">点击更新新闻后会保留时间、来源和结果。</EmptyState>;
  }

  return (
    <div className="run-list">
      {runs.slice(0, 10).map((run) => (
        <article key={run.run_id}>
          <div>
            <strong>{displaySourceName(run.source_id)}</strong>
            <StatusPill tone={run.status === "ok" ? "good" : run.status.includes("error") ? "bad" : "warn"}>
              {statusLabel(run.status)}
            </StatusPill>
          </div>
          <small>文章 {run.articles_found} · 聚类 {run.clusters_upserted} · 事件 {run.events_created}</small>
          {run.error ? <p>{cleanText("本次更新未完成，请查看本地日志。")}</p> : null}
        </article>
      ))}
    </div>
  );
}

export function ArticleClueList({
  articles,
  onSelect
}: {
  articles: NewsArticle[];
  onSelect?: (article: NewsArticle) => void;
}) {
  if (!articles.length) {
    return <EmptyState title="暂无文章线索">新闻更新或 CSV 导入后会展示标题、摘要和来源。</EmptyState>;
  }

  return (
    <div className="article-list">
      {articles.slice(0, 12).map((article) => (
        <article key={article.article_id}>
          <FileText size={15} />
          <div>
            <div>
              <strong>{article.title}</strong>
              <StatusPill tone={toneForTier(article.tier)}>{evidenceLevelLabel(article.tier)}</StatusPill>
            </div>
            <p className="news-cn-summary">{articleChineseSummary(article)}</p>
            <small>{displaySourceName(article.source_id)} · {categoryLabel(article.category)} · 相关度 {Math.round(article.score)}</small>
            {onSelect || article.url ? (
              <div className="inline-actions">
                {onSelect ? <button onClick={() => onSelect(article)} type="button">查看证据</button> : null}
                {article.url ? <a href={article.url} rel="noreferrer" target="_blank">查看原文</a> : null}
              </div>
            ) : null}
          </div>
        </article>
      ))}
    </div>
  );
}
