/** A display projection only. The original diagnostic is returned unchanged. */
export interface PipelineSummaryInput {
  id: string;
  status_detail: string;
  status: string;
}

export interface PipelineNodeSummary {
  summary: string;
  detail?: string;
}

export function pipelineNodeSummary(node: PipelineSummaryInput): PipelineNodeSummary {
  const raw = node.status_detail ?? "";
  const text = raw.trim();
  const result = (summary: string): PipelineNodeSummary => ({ summary, ...(raw ? { detail: raw } : {}) });
  if (!text) return result("运行详情见抽屉");

  if (node.id === "index" && text === "索引过期：grounded_summary_changed") {
    return result("索引过期：摘要内容已变更，需更新索引");
  }
  // These three explicit evidence-state labels are part of the honest fallback
  // contract, not arbitrary machine enums. Preserve their stated human cause.
  if (/^(?:missing|corrupt|stale)(?:\s*[:：]|$)/.test(text)) return result(text);

  // Missing, corrupt and stale evidence must stay visible even when a later
  // sentence happens to contain parsable counts. Never turn these into success.
  if (/文件损坏|无法解析|链报告非当日|无链报告|无信号报告|索引缺失|心跳缺失/.test(text)) {
    return result(text.replace(/\bworker\b/g, "摘要任务"));
  }

  let match: RegExpMatchArray | null;
  switch (node.id) {
    case "collect": {
      match = text.match(/error\s*\+\s*timeout\s+(\d+)\/(\d+)/);
      if (match) {
        const notes = [`采集异常 ${match[1]}/${match[2]}`];
        if (/automation=missing/.test(text)) notes.push("自动采集状态缺失");
        else if (/automation=blocked/.test(text)) notes.push("自动采集受阻");
        else if (/automation=degraded/.test(text)) notes.push("自动采集降级");
        if (/critical:/.test(text)) notes.push("另有关键异常，见详情");
        return result(notes.join(" · "));
      }
      match = text.match(/可达源 (\d+) 次抓取 ok 率 (\d+%)/);
      if (match) {
        const restricted = text.match(/(\d+) 源外部受限/);
        return result(`抓取 ${match[1]} 次 · 成功率 ${match[2]}${restricted ? ` · ${restricted[1]} 源外部受限` : ""}`);
      }
      if (/当日无任何采集运行/.test(text)) return result("当日无采集运行 · 调度未到或停摆");
      if (/automation=missing/.test(text)) return result("自动采集状态缺失 · 见详情");
      if (/automation=blocked/.test(text)) return result("自动采集受阻 · 见详情");
      if (/automation=degraded|^automation degraded$/.test(text)) return result("自动采集降级 · 见详情");
      if (/critical:/.test(text)) return result("采集存在关键异常 · 见详情");
      break;
    }
    case "clean":
      if (text.includes("needs_human_review")) return result("观察级放行 · 仍需人工核验");
      if (/质量门 exit=(?:0|None)，overall=(?:ok|success)$/.test(text) && node.status === "ok") return result("质量门检查通过");
      if (text === "质量门未跑（链未到）") return result(text);
      break;
    case "index":
      match = text.match(/^索引 ready，(\d+) 文档（semantic_embedding）$/);
      if (match) return result(`语义索引 · ${match[1]} 份文档`);
      if (/^索引 ready 但 embedding_mode=/.test(text)) return result("索引采用降级检索 · 见详情");
      if (text === "索引 building 中") return result("索引正在构建");
      break;
    case "event_summary":
      match = text.match(/^pending=(\d+)，failed=(\d+)(.*)$/);
      if (match) return result(`待处理 ${match[1]} 篇 · 失败 ${match[2]} 篇${match[3].includes("积压超 2h") ? " · 积压超过 2 小时" : ""}`);
      match = text.match(/^心跳 (\d+)s，队列清空$/);
      if (match) return result(`队列清空 · 心跳距今 ${match[1]} 秒`);
      break;
    case "event_overview":
      match = text.match(/^今日付费失败 (\d+) 次（failures\/）$/);
      if (match) return result(`今日调用失败 ${match[1]} 次`);
      match = text.match(/^预算 (\d+)\/(\d+) micro-USD，已结算 (\d+) 条$/);
      if (match) return result(`已结算 ${match[3]} 条 · 费用详情见抽屉`);
      if (text === "预算停用（limit=0，fail-closed）") return result("预算停用 · 暂停运行");
      if (text === "预算封盘（used>=limit）") return result("预算已耗尽 · 暂停运行");
      break;
    case "factor_score":
      match = text.match(/^(\d+) 项因子（(\d+) 项 ready）(?:，(\d+) 项输入缺失)?$/);
      if (match) return result(`${match[1]} 项因子 · ${match[2]} 项就绪${match[3] ? ` · ${match[3]} 项输入缺失` : ""}`);
      break;
    case "political_analysis":
      match = text.match(/^调用 \d+ · 模板回退 \d+ · 存活 (\d+)$/);
      if (match) return result(`存活事件 ${match[1]} 个`);
      break;
    case "event_signal":
      if (text === "当日 0 候选：Agent 链跳过，预测走纯价格") return result("当日 0 候选 · 推理链跳过，按价格基准发牌");
      break;
    case "historical_analog":
      match = text.match(/^调用 \d+ · 无先例 (\d+) · 回退 \d+$/);
      if (match) return result(`无匹配先例 ${match[1]} 个`);
      break;
    case "product_synthesis":
      match = text.match(/^(\d+)\/(\d+) 品种因子 · 有方向 (\d+) · 回退 \d+$/);
      if (match) return result(`品种因子 ${match[1]}/${match[2]} · 有方向 ${match[3]} 个`);
      break;
    case "skeptic_review":
      match = text.match(/^维持 (\d+) · 降级 (\d+) · 推翻 (\d+) · 回退 \d+$/);
      if (match) return result(`维持 ${match[1]} · 降级 ${match[2]} · 推翻 ${match[3]}`);
      break;
    case "seven_product": {
      match = text.match(/^formal=(\d+) reference=(\d+) unavailable=(\d+)/);
      if (match) {
        const notes = [`正式 ${match[1]} · 观察 ${match[2]} · 不可用 ${match[3]}`];
        if (text.includes("方向/误差未达标")) notes.push("方向或误差未达标");
        else if (/OOS (?:门禁未过|evaluation blocked)/.test(text)) notes.push("到期评估门禁未过");
        else if (Number(match[1]) === 0) notes.push("正式晋级未开");
        return result(notes.join(" · "));
      }
      break;
    }
    case "counter_scan":
    case "daily_interpretation":
      if (text === "扫描完成（observation_only）") return result("扫描完成 · 仅用于观察");
      if (text === "解读完成（observation_only）") return result("解读完成 · 仅用于观察");
      if (text === "无 artifact：当日链未成功（skipped）") return result("无研判产出 · 当日链未成功，已跳过");
      if (text === "无 artifact：当日链已成功，跳过/等待重跑") return result("无研判产出 · 当日链已成功，等待重跑");
      break;
    case "report_assembly":
      match = text.match(/^overall=(ready|ready_with_warnings)，morning-brief 当日文件存在（warnings=(\d+)）$/);
      if (match && node.status === "ok") return result(`当日晨报已就绪 · ${match[2]} 项提示`);
      match = text.match(/^overall=(ready|ready_with_warnings|blocked|failed|unknown)；morning-brief 当日文件(缺失|存在)$/);
      if (match) return result(`当日晨报文件${match[2]} · ${match[1] === "blocked" ? "组装受阻" : match[1] === "failed" ? "组装失败" : "组装详情见抽屉"}`);
      if (text === "overall=blocked") return result("研报组装受阻");
      if (text === "overall=failed") return result("研报组装失败");
      if (text === "链运行中（latest-status 未生成）") return result("链运行中 · 运行状态尚未生成");
      break;
    case "assistant":
      if (text === "空闲（无最近 run，正常）") return result("暂无最近问答记录");
      match = text.match(/^最近 run completed(?:，质量旗标 (\d+) 项)?$/);
      if (match) return result(`最近回答已交付${match[1] ? ` · ${match[1]} 项质量提示` : ""}`);
      if (text === "最近 run failed（无回答交付）") return result("最近回答生成失败 · 无回答交付");
      if (text === "最近 run 进行中") return result("最近问答正在运行");
      break;
  }

  // Already-human Chinese diagnostics remain useful. Unknown machine payloads
  // are not guessed or scrubbed into misleading pseudo-business information.
  // Recognised business abbreviations do not make readable prose a machine
  // payload. Only whole tokens are allowed; unknown snake_case still falls back.
  const withoutBusinessTerms = text.replace(/\b(?:R[1-5]|OOS|POY|DTY|PTA|MEG|PX|ADR(?:-\d+)?|Agent|SHA(?:-?256)?|lessons|D(?:1|7|30))\b/g, "");
  if (!/[a-zA-Z_]/.test(withoutBusinessTerms)) return result(text);
  if (node.id === "index" && text.startsWith("索引过期：")) return result("索引过期：具体原因见抽屉");
  return result("运行详情见抽屉");
}
