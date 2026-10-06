import type { ReactNode } from "react";
import {
  Activity,
  Bell,
  Bot,
  Database,
  Droplet,
  FileText,
  GitBranch,
  Home,
  LineChart,
  Newspaper,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  UserCircle
} from "lucide-react";
import type { PageId } from "../app/pages";
import { cx } from "./ui";

const primaryPages: PageId[] = ["总览看板", "原料链路", "事件新闻", "情报工作流", "行情观测", "预测复盘"];
const advancedPages: PageId[] = ["证据知识", "数据来源", "AI助手"];

const pageIcons = {
  总览看板: Home,
  原料链路: GitBranch,
  事件新闻: Newspaper,
  情报工作流: SlidersHorizontal,
  行情观测: LineChart,
  预测复盘: Activity,
  证据知识: ShieldCheck,
  数据来源: Database,
  AI助手: Bot
} satisfies Record<PageId, typeof Home>;

function BrandBlock() {
  return (
    <div className="brand-block">
      <i><Droplet size={22} /></i>
      <div>
        <strong>上游原料研究工作台</strong>
        <span>POY/DTY 成本压力 · 证据链 · 复盘</span>
      </div>
    </div>
  );
}

function SideNavigation({
  activePage,
  onSelect
}: {
  activePage: PageId;
  onSelect: (page: PageId) => void;
}) {
  return (
    <aside className="app-nav">
      <div className="nav-kicker">工作区</div>
      <nav aria-label="主导航">
        {primaryPages.map((page) => {
          const Icon = pageIcons[page];
          return (
            <button
              aria-label={page}
              className={cx(activePage === page && "active")}
              key={page}
              onClick={() => onSelect(page)}
              type="button"
            >
              <Icon size={18} />
              <span>{page}</span>
            </button>
          );
        })}
        <div className="nav-section-label">证据与工具</div>
        {advancedPages.map((page) => {
          const Icon = pageIcons[page];
          return (
            <button
              aria-label={page}
              className={cx("is-secondary", activePage === page && "active")}
              key={page}
              onClick={() => onSelect(page)}
              type="button"
            >
              <Icon size={18} />
              <span>{page}</span>
            </button>
          );
        })}
      </nav>
      <div className="nav-footer">
        <button onClick={() => onSelect("证据知识")} type="button">证据</button>
        <button onClick={() => onSelect("情报工作流")} type="button">流程</button>
        <button onClick={() => onSelect("预测复盘")} type="button">复盘</button>
      </div>
    </aside>
  );
}

function TopStatusBar({ apiLabel, modelLabel }: { apiLabel: string; modelLabel: string }) {
  const today = new Intl.DateTimeFormat("zh-CN", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric"
  }).format(new Date());

  return (
    <header className="app-topbar">
      <BrandBlock />
      <div className="topbar-status">
        <div className="status-cell">
          <span>当前日期</span>
          <strong className="info">{today}</strong>
        </div>
        <div className="status-cell">
          <span>模型判断</span>
          <strong className={modelLabel.includes("可用") ? "ok" : "info"}>{modelLabel.includes("可用") ? "可用" : "回退"}</strong>
        </div>
        <div className="status-cell">
          <span>本地数据</span>
          <strong className={apiLabel.includes("正常") ? "ok" : apiLabel === "正在连接" ? "info" : "warn"}>{apiLabel.includes("正常") ? "正常" : apiLabel}</strong>
        </div>
        <div className="status-cell">
          <span>关注重点</span>
          <strong>成本 / 事件 / 缺口</strong>
        </div>
        <Bell size={17} />
        <UserCircle size={18} />
      </div>
    </header>
  );
}

function InsightRail({ activePage, apiLabel, modelLabel, onSelect }: {
  activePage: PageId;
  apiLabel: string;
  modelLabel: string;
  onSelect: (page: PageId) => void;
}) {
  const pageQuestion: Record<PageId, string> = {
    总览看板: "今天该先看什么？",
    原料链路: "成本从哪一段传导？",
    事件新闻: "哪些事件需要相信？",
    情报工作流: "判断链路是否跑通？",
    行情观测: "价格是否支持结论？",
    预测复盘: "模型准不准，错在哪？",
    证据知识: "证据是否足够可靠？",
    数据来源: "数据是否够用？",
    AI助手: "如何解释当前判断？"
  };

  return (
    <aside className="insight-rail" aria-label="右侧研究辅助面板">
      <section className="rail-card rail-focus">
        <span>当前问题</span>
        <strong>{pageQuestion[activePage]}</strong>
        <p>主界面只保留结论、证据、风险、缺口和下一步动作；技术字段进入高级详情。</p>
      </section>
      <section className="rail-card">
        <span>证据链状态</span>
        <div className="rail-status-row"><i className="ok" />本地数据：{apiLabel.includes("正常") ? "可用" : apiLabel}</div>
        <div className="rail-status-row"><i className={modelLabel.includes("可用") ? "ok" : "warn"} />模型：{modelLabel}</div>
        <div className="rail-status-row"><i className="info" />口径：POY/DTY 为公开现货评估价</div>
      </section>
      <section className="rail-card">
        <span>单工作台证据形态</span>
        <ol>
          <li>证据库先于回答</li>
          <li>引用链随回答展示</li>
          <li>工作流节点可追溯</li>
        </ol>
      </section>
      <section className="rail-card rail-actions">
        <button onClick={() => onSelect("证据知识")} type="button">查看证据队列</button>
        <button onClick={() => onSelect("情报工作流")} type="button">查看情报工作流</button>
        <button onClick={() => onSelect("AI助手")} type="button"><Sparkles size={15} />询问助手</button>
      </section>
    </aside>
  );
}

export function PageToolbar({
  activePage,
  onRefresh
}: {
  activePage: PageId;
  onRefresh: () => void;
}) {
  return (
    <div className="page-toolbar">
      <div>
        <FileText size={16} />
        <span>{activePage}</span>
      </div>
      <button onClick={onRefresh} type="button">
        <RefreshCw size={15} />
        <span>刷新</span>
      </button>
    </div>
  );
}

export function AppShell({
  activePage,
  apiLabel,
  modelLabel,
  onSelect,
  children
}: {
  activePage: PageId;
  apiLabel: string;
  modelLabel: string;
  onSelect: (page: PageId) => void;
  children: ReactNode;
}) {
  return (
    <div className="app-shell">
      <TopStatusBar apiLabel={apiLabel} modelLabel={modelLabel} />
      <div className="app-body">
        <SideNavigation activePage={activePage} onSelect={onSelect} />
        <main className="app-main">{children}</main>
        <InsightRail activePage={activePage} apiLabel={apiLabel} modelLabel={modelLabel} onSelect={onSelect} />
      </div>
    </div>
  );
}
