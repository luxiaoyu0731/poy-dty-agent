import type { NewsArticle, NewsEventCluster } from "../services/api";
import { categoryLabel, productLabel } from "./displayLabels";
import { cleanText, displaySourceName, shortText } from "./researchUi";

function productSummary(products?: string[]) {
  const labels = products?.map(productLabel).filter(Boolean) ?? [];
  if (!labels.length) return "上游原料链";
  return labels.slice(0, 5).join("、");
}

function usefulChineseText(value?: string | null, limit = 74) {
  const cleaned = cleanText(value, "").trim();
  if (!cleaned || !/[\u4e00-\u9fff]/.test(cleaned)) return "";
  return shortText(cleaned, limit);
}

export function articleChineseSummary(article: NewsArticle) {
  const topic = categoryLabel(article.category);
  const source = displaySourceName(article.source_id);
  const extra = usefulChineseText(article.summary, 68);
  const first = `这条新闻来自${source}，主题归为${topic}。`;
  const second = extra ? `要点：${extra}` : "系统会把它作为事件判断的公开证据，需结合原文核对。";
  return `${first}${second}`;
}

export function clusterChineseSummary(cluster: NewsEventCluster) {
  const topic = categoryLabel(cluster.category);
  const products = productSummary(cluster.affected_products);
  const direction = cluster.direction || "待判断";
  const extra = usefulChineseText(cluster.summary, 62);
  const first = `这是${topic}相关事件，模型判断方向为${direction}，主要影响${products}。`;
  const second = extra ? `要点：${extra}` : `当前证据等级为 ${cluster.evidence_level}，建议打开原文复核关键事实。`;
  return `${first}${second}`;
}

export function clusterSourceUrl(cluster: NewsEventCluster, articles?: NewsArticle[]) {
  if (cluster.primary_url) return cluster.primary_url;
  if (!articles?.length) return "";
  const ids = new Set(cluster.article_ids);
  const byId = articles.find((article) => ids.has(article.article_id) && article.url);
  if (byId?.url) return byId.url;

  const normalizedTitle = normalizeTitle(cluster.title);
  const byTitle = articles.find((article) => article.url && normalizeTitle(article.title) === normalizedTitle);
  if (byTitle?.url) return byTitle.url;

  const byPartialTitle = articles.find((article) => {
    if (!article.url) return false;
    const articleTitle = normalizeTitle(article.title);
    return articleTitle.includes(normalizedTitle) || normalizedTitle.includes(articleTitle);
  });
  return byPartialTitle?.url ?? "";
}

function normalizeTitle(title?: string | null) {
  return (title ?? "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}
