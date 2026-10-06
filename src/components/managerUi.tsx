import type { ReactNode } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, Clock3, Info, LineChart, ShieldCheck } from "lucide-react";
import { AuditDrawer } from "./researchUi";
import { Button, StatusPill, cx, type Tone } from "./ui";

type Action = {
  label: string;
  onClick?: () => void;
  href?: string;
  disabled?: boolean;
  tone?: Tone;
  icon?: ReactNode;
};

function toneIcon(tone: Tone) {
  if (tone === "good") return <CheckCircle2 size={16} />;
  if (tone === "warn" || tone === "bad") return <AlertTriangle size={16} />;
  if (tone === "accent") return <LineChart size={16} />;
  return <Info size={16} />;
}

function ActionButton({ action }: { action: Action }) {
  if (action.href && !action.disabled) {
    return (
      <a className={cx("manager-action-link", `tone-${action.tone ?? "neutral"}`)} href={action.href}>
        {action.icon}
        <span>{action.label}</span>
      </a>
    );
  }
  return (
    <Button
      disabled={action.disabled}
      icon={action.icon}
      onClick={action.onClick}
      tone={action.tone ?? "neutral"}
    >
      {action.label}
    </Button>
  );
}

export function PageDecisionHeader({
  title,
  subtitle,
  question,
  decision,
  meta,
  actions,
  actionSlot
}: {
  title: string;
  subtitle: string;
  question?: string;
  decision?: ReactNode;
  meta?: ReactNode;
  actions?: Action[];
  actionSlot?: ReactNode;
}) {
  return (
    <header className="manager-page-header span-2">
      <div className="manager-page-copy">
        <span>{question ?? "今日先看什么"}</span>
        <h1>{title}</h1>
        <p>{subtitle}</p>
        {decision ? <div className="manager-decision-line">{decision}</div> : null}
        {meta ? <div className="manager-header-meta">{meta}</div> : null}
      </div>
      {actionSlot ? (
        <div className="manager-page-actions">{actionSlot}</div>
      ) : actions?.length ? (
        <div className="manager-page-actions">
          {actions.map((action) => <ActionButton action={action} key={action.label} />)}
        </div>
      ) : null}
    </header>
  );
}

export function MetricTile({
  label,
  value,
  caption,
  trend,
  tone = "neutral"
}: {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  trend?: ReactNode;
  tone?: Tone;
}) {
  return (
    <article className={cx("manager-metric-tile", `tone-${tone}`)}>
      <div className="metric-tile-top">
        <span>{label}</span>
        {toneIcon(tone)}
      </div>
      <strong>{value}</strong>
      {trend ? <b>{trend}</b> : null}
      {caption ? <small>{caption}</small> : null}
    </article>
  );
}

export function SignalCard({
  label,
  title,
  summary,
  tone = "neutral",
  meta,
  action
}: {
  label: string;
  title: string;
  summary: ReactNode;
  tone?: Tone;
  meta?: ReactNode;
  action?: Action;
}) {
  return (
    <article className={cx("manager-signal-card", `tone-${tone}`)}>
      <header>
        <span>{label}</span>
        <StatusPill tone={tone}>{tone === "good" ? "可用" : tone === "warn" ? "需关注" : tone === "bad" ? "风险" : "观察"}</StatusPill>
      </header>
      <strong>{title}</strong>
      <p>{summary}</p>
      <footer>
        {meta ? <small>{meta}</small> : <small>已纳入当前研判</small>}
        {action ? <ActionButton action={action} /> : null}
      </footer>
    </article>
  );
}

export function PriceTickerCard({
  instrument,
  subtitle,
  price,
  date,
  change,
  unit,
  source,
  quoteType,
  tone = "neutral"
}: {
  instrument: string;
  subtitle?: string;
  price: ReactNode;
  date: ReactNode;
  change?: ReactNode;
  unit?: string;
  source?: ReactNode;
  quoteType?: ReactNode;
  tone?: Tone | "up" | "down";
}) {
  return (
    <article className={cx("price-ticker-card", `tone-${tone}`)}>
      <span>{subtitle}</span>
      <strong>{instrument}</strong>
      <b>{price}</b>
      <small>{date}</small>
      <em>{change ?? "变化待补"}</em>
      <footer>
        <span>{unit ? `单位 ${unit}` : "单位待确认"}</span>
        <i>{quoteType ?? source ?? "来源待确认"}</i>
      </footer>
    </article>
  );
}

export function EventInsightCard({
  title,
  summary,
  source,
  time,
  direction,
  confidence,
  evidence,
  href,
  onOpen
}: {
  title: string;
  summary: string;
  source?: ReactNode;
  time?: ReactNode;
  direction?: ReactNode;
  confidence?: ReactNode;
  evidence?: ReactNode;
  href?: string;
  onOpen?: () => void;
}) {
  return (
    <article className="manager-event-card">
      <header>
        <strong>{title}</strong>
        <div>
          {direction ? <StatusPill tone="accent">{direction}</StatusPill> : <StatusPill tone="warn">待判断</StatusPill>}
          {evidence ? <StatusPill>{evidence}</StatusPill> : null}
        </div>
      </header>
      <p>{summary}</p>
      <footer>
        <span>{source ?? "来源待确认"}</span>
        <span>{time ?? "时间待确认"}</span>
        {confidence ? <span>置信度 {confidence}</span> : null}
        {onOpen ? <button onClick={onOpen} type="button">查看影响依据</button> : null}
        {href ? <a href={href} rel="noreferrer" target="_blank">打开原文</a> : null}
      </footer>
    </article>
  );
}

export function SourceReadinessCard({
  title,
  value,
  summary,
  tone = "neutral",
  action
}: {
  title: string;
  value: ReactNode;
  summary: ReactNode;
  tone?: Tone;
  action?: Action;
}) {
  return (
    <article className={cx("source-readiness-card", `tone-${tone}`)}>
      <header>
        <ShieldCheck size={17} />
        <span>{title}</span>
      </header>
      <strong>{value}</strong>
      <p>{summary}</p>
      {action ? <ActionButton action={action} /> : null}
    </article>
  );
}

export function EvidenceDrawer({
  title = "高级详情",
  summary,
  children,
  defaultOpen = false
}: {
  title?: string;
  summary?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <AuditDrawer defaultOpen={defaultOpen} summary={summary} title={title}>
      {children}
    </AuditDrawer>
  );
}

export function ActionChecklist({
  items
}: {
  items: Array<{ title: string; description: ReactNode; done?: boolean; href?: string; onClick?: () => void }>;
}) {
  return (
    <div className="manager-action-checklist">
      {items.map((item) => (
        <article key={item.title}>
          {item.done ? <CheckCircle2 size={18} /> : <Clock3 size={18} />}
          <div>
            <strong>{item.title}</strong>
            <p>{item.description}</p>
          </div>
          {item.href ? <a aria-label={`打开：${item.title}`} href={item.href}><ArrowRight aria-hidden="true" size={15} /></a> : null}
          {item.onClick ? <button aria-label={`执行：${item.title}`} onClick={item.onClick} type="button"><ArrowRight aria-hidden="true" size={15} /></button> : null}
        </article>
      ))}
    </div>
  );
}

export function EmptyInsightState({
  title,
  children,
  action
}: {
  title: string;
  children: ReactNode;
  action?: Action;
}) {
  return (
    <div className="empty-insight-state">
      <Info size={19} />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
      {action ? <ActionButton action={action} /> : null}
    </div>
  );
}

export function CompactTable({
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
    <div className="compact-table-wrap">
      <table className="compact-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TimeWindowFilter({
  label = "时间窗口",
  value,
  options,
  onChange
}: {
  label?: string;
  value: string;
  options: Array<{ label: string; value: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="time-window-filter">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </label>
  );
}

export function PriceTrendMiniChart({ tone = "neutral" }: { tone?: Tone | "up" | "down" }) {
  return (
    <div className={cx("price-trend-mini-chart", `tone-${tone}`)} aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
      <i />
    </div>
  );
}
