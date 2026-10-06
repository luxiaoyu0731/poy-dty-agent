const sourceNames: Record<string, string> = {
  eia_petroleum_api: "EIA 原油数据",
  opec_press: "OPEC 公告",
  ine_sc_intraday: "上期能源原油",
  czce_pta_px: "郑商所 PTA/PX",
  dce_meg: "大商所 MEG",
  cfets_cny_parity: "人民币中间价",
  fred_macro_api: "FRED 宏观数据",
  cftc_cot_petroleum: "CFTC 持仓",
  ofac_sanctions: "OFAC 制裁",
  ofac_recent_actions: "OFAC 制裁公告",
  uk_government_news: "英国政府公告",
  google_news_oil_rss: "Google 原油新闻",
  google_news_chemical_rss: "Google 化工新闻",
  gacc_trade_statistics: "海关贸易",
  un_comtrade_api: "UN Comtrade",
  internal_market_notes: "内部行业笔记"
};

const categoryLabels: Record<string, string> = {
  sanctions_geopolitics: "制裁与地缘政治",
  geopolitics: "地缘政治",
  oil_policy: "原油政策",
  energy_policy: "能源政策",
  shipping_security: "航运安全",
  shipping: "航运扰动",
  crude_oil: "原油市场",
  crude: "原油市场",
  naphtha: "石脑油链条",
  macro: "宏观金融",
  inventory: "库存变化",
  demand: "需求变化",
  supply: "供应变化",
  trade_flow: "贸易流",
  refinery_supply: "炼厂与供应",
  market_report: "市场报告",
  price_signal: "价格线索",
  company: "企业公告",
  safety: "安全事件",
  policy: "政策公告",
  opec: "OPEC",
  eia: "EIA"
};

const tokenLabels: Record<string, string> = {
  eia: "EIA",
  fred: "FRED",
  opec: "OPEC",
  iea: "IEA",
  ofac: "OFAC",
  treasury: "美国财政部",
  state: "美国国务院",
  gov: "政府",
  uk: "英国",
  eu: "欧盟",
  un: "联合国",
  gacc: "海关",
  comtrade: "Comtrade",
  sunsirs: "生意社",
  texnet: "纺织网",
  textile: "纺织",
  polyester: "聚酯",
  filament: "长丝",
  google: "Google",
  rss: "RSS",
  news: "新闻",
  api: "接口",
  macro: "宏观",
  petroleum: "石油",
  sanctions: "制裁",
  geopolitics: "地缘政治",
  oil: "原油",
  crude: "原油",
  policy: "政策",
  shipping: "航运",
  security: "安全",
  energy: "能源",
  inventory: "库存",
  demand: "需求",
  supply: "供应",
  trade: "贸易",
  flow: "流向",
  refinery: "炼厂",
  market: "市场",
  report: "报告",
  price: "价格",
  signal: "线索",
  company: "企业",
  safety: "安全"
};

const productLabels: Record<string, string> = {
  crude_oil: "原油",
  crude: "原油",
  brent: "Brent",
  wti: "WTI",
  naphtha: "石脑油",
  px: "PX",
  pta: "PTA",
  meg: "MEG",
  poy: "POY",
  dty: "DTY",
  lpg: "LPG"
};

const factorSymbolLabels: Record<string, string> = {
  crude: "原油",
  crude_oil: "原油",
  brent: "Brent",
  wti: "WTI",
  "usd/rates": "美元/利率",
  usdcny: "美元/人民币",
  dxy: "美元指数",
  px_pta: "PX/PTA",
  "px/pta": "PX/PTA",
  poy_dty: "POY/DTY",
  "poy/dty": "POY/DTY",
  events: "事件风险",
  sanctions: "制裁事件",
  shipping: "航运扰动",
  inventory: "库存",
  demand: "需求",
  supply: "供应"
};

const statusLabels: Record<string, string> = {
  ok: "成功",
  ready: "可用",
  error: "失败",
  failed: "失败",
  fallback: "降级结果",
  entered_backtest: "进入回测",
  not_scored: "未纳入评分",
  scored: "已评分",
  insufficient: "证据不足",
  partial_observed: "部分后验",
  pending_future_prices: "待未来价格",
  full_14d: "完整后验",
  pending: "待评分",
  candidate: "候选",
  featured: "重点",
  event: "事件",
  article: "文章",
  cluster: "事件聚类",
  news_article: "新闻文章",
  news_event_cluster: "事件聚类"
};

export function sourceNameLabel(sourceId?: string | null) {
  if (!sourceId) return "未知来源";
  if (sourceNames[sourceId]) return sourceNames[sourceId];
  const label = sourceId
    .toLowerCase()
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => tokenLabels[part] ?? part.toUpperCase())
    .join(" ");
  return label.replace(/\bAPI\b/g, "接口").replace(/\bRSS\b/g, "资讯源") || "外部来源";
}

export function categoryLabel(category?: string | null) {
  const normalized = category?.trim().toLowerCase();
  if (!normalized) return "未分类事件";
  if (categoryLabels[normalized]) return categoryLabels[normalized];
  const translated = normalized
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => tokenLabels[part] ?? part.toUpperCase())
    .join("/");
  return translated || "未分类事件";
}

export function productLabel(product?: string | null) {
  if (!product?.trim()) return "";
  const normalized = product.trim().toLowerCase();
  return productLabels[normalized] ?? product.toUpperCase();
}

export function productListLabel(products?: string[] | null, limit = 5) {
  const labels = products?.map(productLabel).filter(Boolean) ?? [];
  return labels.length ? labels.slice(0, limit).join(" / ") : "影响品种待确认";
}

export function factorSymbolLabel(symbol?: string | null) {
  const normalized = symbol?.trim().toLowerCase();
  if (!normalized) return "因子";
  const product = productLabel(normalized);
  return factorSymbolLabels[normalized] ?? (product || symbol);
}

export function evidenceLevelLabel(level?: string | null) {
  return level ? `证据等级 ${level}` : "证据待补";
}

export function statusLabel(status?: string | null) {
  if (!status?.trim()) return "状态待确认";
  return statusLabels[status] ?? status.replace(/_/g, " ");
}

export function backtestEvidenceLabel(status: "full_14d" | "partial_observed" | "pending") {
  if (status === "full_14d") return "完整后验";
  if (status === "partial_observed") return "部分后验";
  return "待价格";
}
