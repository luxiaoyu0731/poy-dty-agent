export const pageItems = [
  "总览看板",
  "原料链路",
  "事件新闻",
  "情报工作流",
  "行情观测",
  "预测复盘",
  "证据知识",
  "数据来源",
  "AI助手"
] as const;

export type PageId = (typeof pageItems)[number];

export function pageFromHash(): PageId {
  if (typeof window === "undefined") return pageItems[0];
  const hash = decodeURIComponent(window.location.hash.replace(/^#\/?/, ""));
  return pageItems.includes(hash as PageId) ? (hash as PageId) : pageItems[0];
}

export function hashForPage(page: PageId) {
  return `#/${encodeURIComponent(page)}`;
}
