import { Skeleton } from "antd";

export function ContentLoading({ title = "正在读取工作台", detail = "各区块会在真实数据返回后显示。", compact = false }: { title?: string; detail?: string; compact?: boolean }) {
  return <section className={`content-loading${compact ? " is-compact" : ""}`} role="status" aria-live="polite" aria-busy="true">
    <header><span className="content-loading-dot" /><div><strong>{title}</strong><p>{detail}</p></div></header>
    <div className="content-loading-grid" aria-hidden="true">
      {Array.from({ length: compact ? 1 : 3 }, (_, index) => <div key={index}><Skeleton active title={{ width: "40%" }} paragraph={{ rows: compact ? 3 : 4 }} /></div>)}
    </div>
  </section>;
}
