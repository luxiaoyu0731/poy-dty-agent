/** Name the actual quote publisher independently of the historical series. */
export function priceSourceLabel(sourceId?: string, sourceUrl?: string): string {
  if (sourceId === "public_spot_page_refresh" && sourceUrl) {
    try {
      const host = new URL(sourceUrl).hostname;
      if (host === "texnet.com.cn" || host.endsWith(".texnet.com.cn")) return "纺织网（生意社参考价）";
      if (host.endsWith("tradingeconomics.com")) return "Trading Economics";
      if (host.endsWith("100ppi.com")) return "生意社";
    } catch { /* Invalid URLs must not determine a publisher. */ }
  }
  return ({
    tnc_polyester_history: "全球纺织网", czce_pta_px: "郑商所", sunsirs_public_commodity_assessment: "生意社",
    public_spot_page_refresh: "公开页面评估", eia_petroleum_api: "EIA", sina_futures_realtime: "新浪期货代理",
    yahoo_finance_proxy: "Yahoo 期货代理", eastmoney_futures_realtime: "东方财富期货代理",
  } as Record<string, string>)[sourceId ?? ""] ?? "公开报价来源";
}
