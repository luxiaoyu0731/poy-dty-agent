import { expect, test } from "@playwright/test";
import { formatAssistantCitationText } from "../src/utils/assistantCitationText";

test("business formatting preserves exact source identifiers in citations", () => {
  const input = "原油上涨（doc_id=event:news_oil_123, market:abc_def）。quality_gate";
  const result = formatAssistantCitationText(input, text => text.replace(/[A-Za-z]+(?:_[A-Za-z0-9]+)+/g, "业务数据"));
  expect(result).toBe("原油上涨（证据：event:news_oil_123, market:abc_def）。业务数据");
});

test("only current-answer evidence becomes an original-source link", async () => {
  const { splitAssistantCitations } = await import("../src/utils/assistantCitationText");
  const known = { id: "news_article:art_one", title: "POY报价", url: "https://example.com/original" };
  const parts = splitAssistantCitations("POY为9242.50 [news_article:art_one；news_article:fake]", [known]);
  expect(parts).toEqual([{ text: "POY为9242.50 " }, { reference: known, number: 1 }, { unmatched: true }]);
});


test("colon form preserves the same resolvable document reference", async () => {
  const { splitAssistantCitations } = await import("../src/utils/assistantCitationText");
  const known = { id: "news_article:art_one", title: "POY报价", url: "https://example.com/original" };
  expect(splitAssistantCitations("报价（doc_id: news_article:art_one）", [known]))
    .toEqual([{ text: "报价（" }, { reference: known, number: 1 }, { text: "）" }]);
});

test("current quote publisher is independent of the historical-series publisher", async () => {
  const { priceSourceLabel } = await import("../src/utils/priceSourceLabel");
  expect(priceSourceLabel("public_spot_page_refresh", "https://info.texnet.com.cn/detail-1083841.html"))
    .toBe("纺织网（生意社参考价）");
  expect(priceSourceLabel("tnc_polyester_history")).toBe("全球纺织网");
  expect(priceSourceLabel("public_spot_page_refresh", "https://zh.tradingeconomics.com/commodity/naphtha"))
    .toBe("Trading Economics");
});


test("lookalike hosts cannot impersonate price publishers", async () => {
  const { priceSourceLabel } = await import("../src/utils/priceSourceLabel");
  for (const url of ["https://eviltradingeconomics.com/a", "https://not100ppi.com/a", "not a URL"]) {
    expect(priceSourceLabel("public_spot_page_refresh", url)).toBe("公开页面评估");
  }
  expect(priceSourceLabel("public_spot_page_refresh", "https://www.100ppi.com/a")).toBe("生意社");
});
