import type { PageId } from "./pages";

export const visualReferenceByTitle: Record<PageId, string> = {
  总览看板: "/visual-reference/01-overview.png",
  原料链路: "/visual-reference/03-chain-map.png",
  事件新闻: "/visual-reference/04-major-events.png",
  情报工作流: "/visual-reference/05-political-reasoning.png",
  行情观测: "/visual-reference/06-cost-pressure.png",
  预测复盘: "/visual-reference/08-review.png",
  证据知识: "/visual-reference/10-knowledge-graph.png",
  数据来源: "/visual-reference/12-sources.png",
  AI助手: "/visual-reference/11-ai-assistant.png"
};

export function shouldShowVisualReference() {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  return params.get("reference") === "1" || params.get("visual-reference") === "1";
}
