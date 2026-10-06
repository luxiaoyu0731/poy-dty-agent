import { ContentLoading } from "./ContentLoading";
import { EvidenceDossierButton } from "../features/evidence-system/EvidenceDossierButton";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, App, Button, Card, Empty, Select, Space, Typography } from "antd";
import { api, type InformationReport } from "../services/api";
import { formatDisplayTimestamp, formatTimestampsInText } from "../utils/displayFormatting";

// Render our frozen Markdown subset as text nodes; source HTML is never executed.
function reportText(value: string) {
  return formatTimestampsInText(value.replace(/\\([\\`*_{}\[\]<>])/g, "$1"));
}
function readingSection(section: string): string {
  if (!section.startsWith("## 七品种传导判断")) return section;
  let legacyTable = false;
  return section.split("\n").map(line => {
    if (line.trim() === "## 七品种传导判断") return "## 七品种观察与事件判断\n\n阅读视图将价格观察与事件判断分栏；保存原文与下载内容保持不变。";
    if (/^\|\s*品种\s*\|\s*当前判断（系统推断）\s*\|\s*直接依据\s*\|$/.test(line.trim())) {
      legacyTable = true;
      return "| 品种 | 事件判断（系统推断） | 已发生的价格观察 | 事件引用 |";
    }
    if (!legacyTable || !line.trim().startsWith("|")) return line;
    if (/^\|[-:| ]+\|$/.test(line.trim())) return "|---|---|---|---|";
    const cells = line.split("|").slice(1, -1).map(cell => cell.trim());
    if (cells.length !== 3) return line;
    const combined = cells[1].match(/^价格事实：(.*?)。事件判断：(.*)$/);
    const price = combined?.[1] ?? "原文未提供价格观察";
    const judgment = combined?.[2] ?? cells[1];
    const refs = cells[2].replace(/\[价格观察·[^\]]+\][；;]?\s*/g, "") || "未建立直接事件依据";
    return `| ${cells[0]} | ${judgment} | ${price} | ${refs} |`;
  }).join("\n");
}
function reportBlocks(content: string): ReactNode[] {
  const lines = content.split("\n");
  const blocks: ReactNode[] = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i].trim();
    if (!line) { i++; continue; }
    const key = i;
    if (/^批次[：:]\s*seven-/.test(line)) {
      blocks.push(<details key={key}><summary>预测追溯信息</summary><p>{reportText(line)}</p></details>);
      i++; continue;
    }
    const heading = line.match(/^(#{1,3}) (.*)$/);
    if (heading) {
      const text = reportText(heading[2]);
      blocks.push(heading[1].length === 1 ? <h2 key={key}>{text}</h2> : heading[1].length === 2 ? <h3 key={key}>{text}</h3> : <h4 key={key}>{text}</h4>);
      i++; continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const row = lines[i++].trim();
        if (!/^\|[-:| ]+\|$/.test(row)) rows.push(row.split("|").slice(1, -1).map(cell => reportText(cell.trim())));
      }
      blocks.push(<div className="report-table-scroll" key={key}><table><thead><tr>{rows[0]?.map((cell,j) => <th key={j} scope="col">{cell}</th>)}</tr></thead><tbody>{rows.slice(1).map((row,j) => <tr key={j}>{row.map((cell,k) => <td key={k}>{cell}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    if (line.startsWith("- ")) {
      const items: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("- ")) items.push(reportText(lines[i++].trim().slice(2)));
      blocks.push(<ul key={key}>{items.map((item,j) => <li key={j}>{item}</li>)}</ul>);
      continue;
    }
    const paragraph: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3} |\||- |批次[：:]\s*seven-)/.test(lines[i].trim())) paragraph.push(reportText(lines[i++]));
    blocks.push(<p key={key}>{paragraph.join("\n")}</p>);
  }
  return blocks;
}
function ReportBody({ content }: { content: string }) {
  // When the introduction repeats the core judgement verbatim, omit only that
  // repeated paragraph in the reader. Frozen files and download remain intact.
  const core = content.match(/^## 核心研判\s*\n\s*([^\n]+)/m)?.[1]?.trim();
  if (core) {
    const boundary = content.search(/^## /m);
    if (boundary >= 0) {
      const intro = content.slice(0, boundary).split("\n").filter(line => line.trim() !== core).join("\n");
      content = intro + "\n" + content.slice(boundary);
    }
  }
  return <article className="business-report-body" data-testid="information-report-content">
    {content.split(/(?=^## )/m).map((section, index) => {
      // Old reports remain frozen on disk; omit the retired sections in the reader too.
      const title = section.split("\n", 1)[0].trim();
      if (title === "## 判断如何变化" || title === "## 关键风险、反证与下一步观察") return null;
      if (section.startsWith("## 附录：")) {
        const split = section.indexOf("\n");
        return <details className="business-report-appendix" key={index}><summary>{section.slice(3, split)}</summary>{reportBlocks(section.slice(split + 1))}</details>;
      }
      return <section key={index}>{reportBlocks(readingSection(section))}</section>;
    })}
  </article>;
}

export function InformationReports({ kind: reportType, headerActions }: { kind: string; headerActions?: ReactNode }) {
  const kind = reportType.replace("报告", "");
  const { message } = App.useApp();
  const [items, setItems] = useState<InformationReport[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState<InformationReport & { content: string }>();
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [listReading, setListReading] = useState(true);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const [generation, setGeneration] = useState(0);
  const refresh = async () => {
    setListReading(true);
    try {
      const result = await api.informationReports();
      setItems(result.items);
      setError("");
    } catch {
      setError("报告列表未读取成功，请重新读取。");
    } finally { setListReading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  const visible = items.filter(item => item.kind === kind).sort((a, b) => b.generated_at.localeCompare(a.generated_at));
  const active = visible.find(item => item.id === selected) ?? visible[0];
  useEffect(() => {
    let cancelled = false;
    setContent(previous => previous?.id === active?.id ? previous : undefined);
    if (!active) { setReading(false); return; }
    setReading(true);
    api.informationReportContent(active.id).then(result => {
      if (!cancelled) setContent(result);
    }).catch(() => {
      if (!cancelled) setError("报告正文未读取成功，请重新读取。");
    }).finally(() => { if (!cancelled) setReading(false); });
    return () => { cancelled = true; };
  }, [active?.id, generation]);
  const generate = async () => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const report = await api.generateInformationReport(kind);
      setItems(previous => [report, ...previous.filter(item => item.id !== report.id)]);
      setSelected(report.id);
      message.success("信息报告已生成并保存");
    } catch {
      await refresh();
      setError("尚未确认生成结果，请先核对报告列表和生成时间，再决定是否重试。");
    } finally { lock.current = false; setBusy(false); }
  };
  const generatedDay = content && Number.isFinite(new Date(content.generated_at).getTime()) ? new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(content.generated_at)) : "";
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const historical = Boolean(generatedDay && generatedDay !== today);
  const sourceUrls = Array.from(new Set((content?.content ?? "").split("\n")
    .map(line => line.trim().match(/^原文：(https?:\/\/\S+)$/)?.[1]).filter((url): url is string => Boolean(url))));
  return <Card title={`市场与产业研判${kind}`} extra={headerActions} data-testid="information-reports" style={{ marginBottom: 16 }}>
    <Space wrap style={{ marginBottom: 12 }}>
      {active ? <EvidenceDossierButton key={active.id} label="查看本报告证明" context={{report_id: active.id}} /> : null}
      <Button aria-label={`生成信息${kind}`} type="primary" loading={busy} disabled={busy} onClick={generate}>生成信息{kind}</Button>
      <Button onClick={() => { setGeneration(value => value + 1); void refresh(); }}>重新读取报告</Button>
      <Select aria-label="选择信息报告" value={active?.id} style={{ minWidth: 240, maxWidth: "100%" }}
        placeholder="选择已生成报告" onChange={setSelected} options={visible.map(item => ({
          value: item.id, label: `${item.title} · ${formatDisplayTimestamp(item.generated_at) ?? "时间待确认"}`
        }))} />
      <Button disabled={!content} href={content ? api.informationReportDownloadUrl(content.id) : undefined}>下载信息报告</Button>
      <Button disabled={!content} onClick={async () => {
        if (!content) return;
        try { await navigator.clipboard.writeText(`${content.title}\n${formatTimestampsInText(content.summary)}`); message.success("信息报告摘要已复制"); }
        catch { message.warning("复制未完成，请从正文手动复制。"); }
      }}>复制信息摘要</Button>
    </Space>
    {historical ? <Alert type="info" showIcon message={`正在阅读历史${kind} · ${generatedDay}`} description="以下内容保留报告生成时的资料，不代表今天的最新判断。" /> : null}
    {error ? <Alert type="warning" showIcon message={error} /> : null}
    {reading && content?.id === active?.id ? <Typography.Text role="status" type="secondary">正在核对报告更新，已显示的原文保留。</Typography.Text> : null}
    {(reading && content?.id !== active?.id) || (listReading && !active) ? <ContentLoading title="正在读取信息报告" detail="读取已保存的报告；加载期间不生成新的判断。" compact /> : content ? <>
      <Typography.Text type="secondary">信息报告 · 生成时间 {formatDisplayTimestamp(content.generated_at) ?? "时间待确认"}（上海）</Typography.Text>
      <div className="review-report-scroll"><ReportBody content={content.content} />
      <details><summary>来源链接（{sourceUrls.length}）</summary><ul>{sourceUrls.map(url => <li key={url} style={{ overflowWrap: "anywhere" }}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a></li>)}</ul></details>
      <details><summary>报告校验值</summary><code style={{ overflowWrap: "anywhere" }}>{content.sha256}</code></details>
    </div></> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={error ? "读取未确认，无法判断报告是否为空。" : "当前类型尚无信息报告，点击生成即可整理现有资料。"} />}
  </Card>;
}
