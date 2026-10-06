#!/usr/bin/env bash
# 数据源可达性探测：在目标服务器上运行，验证 18 个中国大陆域名 + 关键国际源。
# 用法: bash scripts/probe_source_reachability.sh [超时秒数，默认 12]
#
# 判读：
#   HTTP 200/3xx      可达
#   403/412/429       被 WAF/反爬拦截（大陆源上出现 = 高风险信号）
#   000 (curl 错误)   连接失败/超时/DNS
set -uo pipefail
T="${1:-12}"
UA_TEXTILE="POY-DTY-Agent/1.0 personal-research"
UA_BROWSER="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

probe() { # name url ua
  local name="$1" url="$2" ua="$3"
  local out
  out=$(curl -s -o /dev/null -w "%{http_code} %{time_total}s %{size_download}B" \
    -m "$T" -A "$ua" ${4:+-H "Referer: $4"} "$url" 2>/dev/null) || out="000 -- --"
  printf "%-28s %-58s -> %s\n" "$name" "${url:0:58}" "$out"
}

echo "== 中国大陆数据源（UA=纺织网调度器）=="
probe "郑商所 PX/PTA"        "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260918/FutureDataDaily.txt" "$UA_TEXTILE"
probe "纺织网价格频道(POY/DTY)" "https://info.texnet.com.cn/list--20-.html" "$UA_TEXTILE" "https://info.texnet.com.cn/list--20-.html"
probe "TNC 均价页"           "https://www.tnc.com.cn/market/average-price-d92.html" "$UA_TEXTILE" "https://www.tnc.com.cn/market/average-price.html"
probe "生意社 MEG"           "https://www.sunsirs.com/uk/prodetail-222.html" "$UA_BROWSER"
probe "生意社 PX"            "https://www.sunsirs.com/uk/prodetail-968.html" "$UA_BROWSER"
probe "100ppi dl"            "https://dlpoy.100ppi.com/" "$UA_BROWSER"
probe "新浪行情"             "https://hq.sinajs.cn/list=hf_OIL" "$UA_BROWSER" "https://finance.sina.com.cn"
probe "东财行情"             "https://push2.eastmoney.com/api/qt/stock/get?secid=0.000001&fields=f43" "$UA_BROWSER"
probe "发改委"               "https://www.ndrc.gov.cn/xwdt/xwfb/" "$UA_BROWSER"
probe "能源局"               "https://www.nea.gov.cn/" "$UA_BROWSER"
probe "海关总署"             "https://english.customs.gov.cn/" "$UA_BROWSER"
probe "中石油新闻"           "https://news.cnpc.com.cn/cnpcnews/" "$UA_TEXTILE" "https://news.cnpc.com.cn/cnpcnews/"
probe "中石化"               "https://www.sinopec.com/listco/en/news/index.shtml" "$UA_BROWSER"
probe "上交所"               "https://www.sse.com.cn/" "$UA_BROWSER"
probe "深交所"               "https://www.szse.cn/" "$UA_BROWSER"
probe "巨潮资讯"             "https://www.cninfo.com.cn/" "$UA_BROWSER"
probe "棉花协会"             "https://www.ccfa.com.cn/" "$UA_BROWSER"
probe "煤炭协会"             "https://www.coalchina.org.cn/" "$UA_BROWSER"

echo
echo "== 关键国际源 =="
probe "EIA API"             "https://api.eia.gov/" "$UA_BROWSER"
probe "EIA 官网"            "https://www.eia.gov/" "$UA_BROWSER"
probe "OFAC"                "https://ofac.treasury.gov/" "$UA_BROWSER"
probe "oilprice(新源)"      "https://oilprice.com/Latest-Energy-News/World-News/" "$UA_BROWSER"
probe "TradingEconomics 石脑油" "https://zh.tradingeconomics.com/commodity/naphtha" "$UA_BROWSER"
probe "GDELT"               "https://api.gdeltproject.org/" "$UA_BROWSER"
probe "Yahoo 期货"          "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=1d&interval=1m" "$UA_BROWSER"
probe "USGS 地震"           "https://earthquake.usgs.gov/fdsnws/event/1/query?limit=1&format=geojson" "$UA_BROWSER"

echo
echo "== 判读：403/412/429 或 000 出现在大陆源上，说明该机房对中文管道有实质风险 =="
