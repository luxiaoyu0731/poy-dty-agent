import type { ReactNode } from "react";
import { AlertTriangle, Box, CheckCircle2, Info, Loader2 } from "lucide-react";

export type Tone = "neutral" | "good" | "warn" | "bad" | "accent";

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export function Button({
  children,
  icon,
  tone = "neutral",
  disabled,
  onClick,
  type = "button",
  className = ""
}: {
  children: ReactNode;
  icon?: ReactNode;
  tone?: Tone;
  disabled?: boolean;
  onClick?: () => void;
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button className={cx("ui-button", `tone-${tone}`, className)} disabled={disabled} onClick={onClick} type={type}>
      {icon}
      <span>{children}</span>
    </button>
  );
}

export function Panel({
  id,
  title,
  eyebrow,
  actions,
  children,
  className = ""
}: {
  id?: string;
  title: string;
  eyebrow?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("ui-panel", className)} id={id}>
      <div className="ui-panel-head">
        <div>
          {eyebrow ? <span>{eyebrow}</span> : null}
          <h2>{title}</h2>
        </div>
        {actions ? <div className="ui-panel-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function MetricCard({
  label,
  value,
  meta,
  badge,
  caption,
  tone = "neutral",
  icon
}: {
  label: string;
  value: ReactNode;
  meta?: ReactNode;
  badge?: ReactNode;
  caption?: ReactNode;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <article className={cx("metric-card", `tone-${tone}`)}>
      <div>
        <span>{label}</span>
        {icon ? <i>{icon}</i> : null}
      </div>
      <strong>{value}</strong>
      {badge ? <span className={cx("metric-badge", `tone-${tone}`)}>{badge}</span> : null}
      {meta ? <small>{meta}</small> : null}
      {caption ? <small className="metric-caption">{caption}</small> : null}
    </article>
  );
}

export function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={cx("status-pill", `tone-${tone}`)}>{children}</span>;
}

export function EmptyState({
  title,
  children,
  tone = "neutral"
}: {
  title: string;
  children: ReactNode;
  tone?: Tone;
}) {
  const Icon = tone === "good" ? CheckCircle2 : tone === "warn" || tone === "bad" ? AlertTriangle : Info;
  return (
    <div className={cx("empty-state", `tone-${tone}`)}>
      <Icon size={18} />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  );
}

export function LoadingState({ label = "加载中" }: { label?: string }) {
  return (
    <div className="loading-state">
      <Loader2 size={16} />
      <span>{label}</span>
    </div>
  );
}

export function KeyValueList({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="key-value-list">
      {items.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function DataTable({
  columns,
  rows,
  empty
}: {
  columns: string[];
  rows: ReactNode[][];
  empty: ReactNode;
}) {
  if (!rows.length) return <>{empty}</>;
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function IconBox({ children, tone = "neutral" }: { children?: ReactNode; tone?: Tone }) {
  return <i className={cx("icon-box", `tone-${tone}`)}>{children ?? <Box size={16} />}</i>;
}
