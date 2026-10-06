import { formatDisplayTimestamp } from "../utils/displayFormatting";
import { useEffect, useState } from "react";
import { api, type IntradayPriceObservation } from "../services/api";

/** A separate reference series: never splice a futures proxy into spot history. */
export function PublicQuoteHistory({ instrument, refreshKey }: { instrument: string; refreshKey?: string }) {
  const [state, setState] = useState<{ instrument: string; rows: IntradayPriceObservation[]; error?: string }>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    api.intradayPrices(instrument, 200).then((rows) => {
      if (active) setState({ instrument, rows });
    }).catch(() => {
      if (active) setState({ instrument, rows: [], error: "近期报价未能加载" });
    });
    return () => { active = false; };
  }, [instrument, attempt, refreshKey]);
  const current = state?.instrument === instrument ? state : undefined;
  const latest = current?.rows[0];
  const basis = (row: IntradayPriceObservation) => [row.instrument, row.symbol, row.source_id, row.unit, row.price_type].join("|");
  const seen = new Set<string>();
  const rows = (current?.rows ?? []).filter((row) => {
    if (typeof row.last !== "number" || !Number.isFinite(row.last) || !latest || basis(row) !== basis(latest) || seen.has(row.observed_at)) return false;
    seen.add(row.observed_at);
    return true;
  }).slice(0, 20);
  return <details className="public-quote-history" open>
    <summary>近期同口径参考报价 · {instrument}</summary>
    {!current ? <p role="status">正在读取近期报价…</p> : current.error ?
      <p role="alert">{current.error} <button type="button" onClick={() => setAttempt((value) => value + 1)}>重试</button></p> :
      rows.length ? <>
        <p>来自 {latest?.source_id}，单位 {latest?.unit}。按原始观察时间列出，与下方历史现货曲线分开。</p>
        <div style={{ overflowX: "auto", maxHeight: 240 }}>
          <table><thead><tr><th scope="col">观察时间（原始时区）</th><th scope="col">参考价</th><th scope="col">来源</th></tr></thead>
            <tbody>{rows.map((row) => <tr key={row.observed_at}>
              <td>{formatDisplayTimestamp(row.observed_at) ?? "暂无日期"}</td><td>{row.last?.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} {row.unit}</td>
              <td><a href={row.source_url} target="_blank" rel="noopener noreferrer">原始来源</a></td>
            </tr>)}</tbody></table>
        </div>
      </> : <p>当前尚无可读取的同口径报价记录。</p>}
  </details>;
}
