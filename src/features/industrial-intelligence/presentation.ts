/** The frozen v1 calendar is Monday–Friday, not the PRC holiday calendar. */
export function publicationState(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now);
  const get = (type: string) => parts.find((part) => part.type === type)!.value;
  const date = `${get("year")}-${get("month")}-${get("day")}`;
  const weekday = new Date(`${date}T00:00:00Z`).getUTCDay();
  const minutes = Number(get("hour")) * 60 + Number(get("minute"));
  const message = weekday === 0 || weekday === 6
    ? "今天不在发布日历内。当前日历为周一至周五，不含法定节假日调休规则。"
    : minutes < 500
      ? "今日尚在信息收集窗口，08:20（上海时间）截止后形成摘要。"
      : minutes < 570
        ? "今日信息窗口已截止，计划于09:30（上海时间）前发布；当前仍在等待摘要。"
        : "今日已超过09:30（上海时间）计划发布时间，尚无当日摘要。请在运行与来源核对任务及缺口。";
  return { date, message, overdue: weekday > 0 && weekday < 6 && minutes >= 570 };
}

export function shanghaiDateTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short", hour12: false }).format(date)}（上海）`;
}

const PLACEHOLDER_TITLE_MARKERS = ["原站未在自动采集范围内", "未生成事实摘要", "暂无可核验"];
// A URL slug leaked in as a title: 3+ hyphen-joined ASCII segments, no spaces.
const SLUG_TITLE = /^[a-z0-9]+(?:-[a-z0-9]+){2,}$/i;

function cleanTitleText(value: string | null | undefined): string {
  return (value ?? "").replace(/\uFFFD+/g, "").replace(/\s+/g, " ").trim();
}

// Aggregator titles often arrive as raw English headlines. They are valid
// titles, but for a Chinese operator they read as noise when a Chinese
// overview exists; treat "no CJK at all, more than a short ticker" as weak.
function isForeignLanguageTitle(value: string): boolean {
  if (!value) return false;
  const cjkCount = (value.match(/[\u4e00-\u9FFF]/g) ?? []).length;
  if (cjkCount > 0) return false;
  const asciiLetterCount = (value.match(/[A-Za-z]/g) ?? []).length;
  return asciiLetterCount > 6;
}

/** Prefer a readable title; fall back to the overview for slug/placeholder/
 *  foreign-language titles and drop replacement-character noise from upstream
 *  encodings. */
export function displayEventTitle(event: { title?: string | null; overview_text?: string | null }): string {
  const title = cleanTitleText(event.title);
  const overview = cleanTitleText(event.overview_text);
  const weak = !title
    || PLACEHOLDER_TITLE_MARKERS.some((marker) => title.startsWith(marker))
    || SLUG_TITLE.test(title)
    || isForeignLanguageTitle(title);
  if (!weak) return title;
  if (overview) return overview.length > 60 ? `${overview.slice(0, 60)}…` : overview;
  return title || "未命名事件";
}
