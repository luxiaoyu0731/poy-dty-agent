import { Button, Drawer } from "antd";

export const pipelineLenses = [
  { title: "RAG 检索增强", nodes: ["index", "historical_analog", "unified_memory"], sections: [
    ["检索存在 ≠ 已获得方向计票权", "合格材料分块进入唯一语义索引。资源数量、索引健康与当日召回结果以接口实时状态为准。"],
    ["已运行 · 结构化案例先验", "历史经验保留结构化案例库先验，新增索引召回由独立开关控制。候选按品种重叠 → 案例层级 → 时新排序；无合适先例返回 no_prior。"],
    ["ADR-10 · 召回参与定案", "时点安全向量召回 → 段落配价格结果 → 升级为召回案例 → 同方向 ≥3 条独立互证才有计票权。召回准入与品种、期限绑定；启用状态和本次合格召回数量分别展示。"],
    ["两种支持数不可混用", "R2 的历史先验支持案例 ≥2 是当前定案条件；ADR-10 的同向 ≥3 是新增召回案例准入条件，且只能作用于证据对应的品种与期限。原始相似段落不能直接投票。"]
  ] },
  { title: "多 Agent 接力", nodes: ["event_summary", "event_overview", "political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion"], sections: [
    ["四阶段主链", "政局解读 16 → 历史经验 16 → 品种研判 7 → 交叉质证 7。四阶段请求配额合计 46；各阶段仍有独立上限，成功数与模板回退分别记录。"],
    ["条件支路 · 冲突裁决", "R5 发现跨品种矛盾传导断裂时，进入冲突裁决 Agent（≤2 次/日），再重跑确定性定案。它不是每次必经的第五阶段，也不新增后端节点。"],
    ["失败路径", "全链默认 HTTP 硬顶 60 次/日：五阶段请求配额最多 48，另留 12 次尝试余量。连接失败和 schema 修复重试同样计入总账；预算耗尽走显式模板回退。实际硬顶以当日账本为准，不能阻塞 21 格发行。"]
  ] },
  { title: "Agent 通信", nodes: ["political_analysis", "historical_analog", "product_synthesis", "skeptic_review", "event_fusion"], sections: [
    ["agent_artifact.v1 · 标准信封", "按统一工件协议交接推理产物和引用。事件 ID、案例 ID、教训 ID 必须可溯源；不能用自然语言叙述替代可验证引用。"],
    ["引用防伪", "摘要引句与原文逐字对齐；supporting_event_ids 必须落在冻结输入集内；case_id 不得超出候选案例。拒收与模板回退需显式保留。"],
    ["预算账本边界", "graph 的 metrics.calls 只覆盖四阶段，画布仅显示四阶段小计。全链总账读取链报告预算快照，包含失败尝试、R5 与重试；接口缺值时保持未知。事件摘要等常驻任务另有预算。"],
    ["接口可见性", "节点详情展示真实输入、输出和 evidence_entries；当前详情未提供完整标准信封时，不补造信封、引用校验结果或运行记录。"]
  ] },
  { title: "上下文管理", nodes: ["collect", "index", "event_signal", "political_analysis", "product_synthesis", "seven_product", "shadow_eval"], sections: [
    ["发行时点 · ex-ante", "近 7 天热度前 16 个事件形成冻结输入集 SHA。预测只使用截至 08:00 北京时间可得的信息，不能把 08:00 后新材料回填进已发行判断。"],
    ["价格基准与快照", "point-in-time 价格基准同时进入品种研判与预测定案。21 格按 append-only 入账，附输入快照哈希；回放水印隔离，回放只写隔离库。"],
    ["时序", "07:50 事件管线 → 08:00 结算与发牌 → 09:31 晨报；每周一 09:05 蒸馏。教训只能来自已结算样本，不能越过周屏障。"],
    ["引用约束", "推理必须引用冻结输入集内的事件与候选案例；无法核对来源的引用不进入定案依据。"]
  ] },
  { title: "记忆模块", nodes: ["index", "historical_analog", "shadow_eval", "unified_memory"], sections: [
    ["已运行 · 案例库", "7,209 条（审查快照）：45 T1 人工精选 + 1,396 T2 极端日 + 5,602 T2Q 全日锚 + 166 条含真实后验旧案。案例的适用品种与期限决定其先验范围。"],
    ["接入 · 索引召回与互证", "统一记忆舱的 b 路通过独立开关控制：时点安全检索、价格结果配对、同向 ≥3 互证准入。卡片能力位不能推定成功；没有独立运行证据时仍显示未知，已有案例库和教训单独记录。"],
    ["运行中 · lessons 回灌", "每周一 09:05 从有冻结分期限分析口径的已结算改写格蒸馏，活跃教训 ≤30 条，以背景假设注入政局解读与历史经验；缺少合格样本时跳过。"],
    ["教训来源与更新", "教训保留来源、有效时刻和样本引用；模拟导入与真实结算蒸馏分别标记。新教训从已到期的方向改写格提炼，采用发行时冻结的分期限分析带，以背景假设进入下一轮判断。"]
  ] },
  { title: "融合与门禁", nodes: ["product_synthesis", "skeptic_review", "event_fusion", "seven_product", "shadow_eval", "counter_scan"], sections: [
    ["R1–R4 · 确定性定案", "R1：置信 ≥0.6 且同向，沿用基准。R2：置信 ≥0.6 且分歧，历史先验同向且支持案例 ≥2，改写为事件方向。R3：分歧而先验不支持，维持基准。R4：置信 <0.6 或因子中性，维持基准。"],
    ["R5 · 条件裁决后重跑", "跨品种矛盾传导断裂 → 冲突裁决 Agent ≤2 次/日 → 规则重跑。规则融合自身零 LLM，条件裁决会消耗 LLM 预算。"],
    ["O1 · D1 恒用基准", "D1 不接受事件改写。链失败亦使用价格基准。融合方向是唯一发行方向，OOS 门禁独立评判这一方向，不能改用旁路方向冒充成绩。"],
    ["三种状态分别判断", "执行成功：任务完成；发行资格：正式或观察；验证覆盖：哪些品种与期限得到验证。0 正式 / 21 观察是 2026-10-03 审查快照中的治理状态，不是执行故障，也不是实时接口值。"],
    ["反证读取", "反证扫描提供可追溯的相反线索；没有找到反证时保留空态，不自动提升判断置信度。"]
  ] }
];
const validationSections = [
  ["来源与时刻", "依据保留原文、来源与可见时刻。发行之后出现的材料不能进入此前预测，历史召回还需证明后验结果在当时已经可得。"],
  ["计票范围", "召回案例仅作用于其证据对应的品种与期限。同向 ≥3 条独立互证满足召回准入；R2 的历史先验支持案例 ≥2 是定案条件。"],
  ["运行核对", "启用状态、资源数量和实际执行分别读取。节点详情提供输入、输出、引用与运行记录；分析证据档案保留本次发行的依据链。"],
  ["发行与结算", "二十一格按唯一主线冻结入账。正式或观察资格由独立 OOS 门禁评判；到期结算和复盘教训保留样本与版本出处。"]
];
const lensQuestions: Record<string, string> = {
  "RAG 检索增强": "系统从哪里找材料，找到了什么，什么情况下才有计票权？",
  "多 Agent 接力": "各阶段如何分工，上一阶段的输出怎样约束下一阶段？",
  "Agent 通信": "推理结果如何交接，怎样拒收无来源或格式错误的结果？",
  "上下文管理": "每次判断能看到哪些信息，如何避免使用发行之后才出现的材料？",
  "记忆模块": "历史案例和复盘教训如何进入判断，怎样约束计票范围？",
  "融合与门禁": "模型意见如何变成唯一方向，哪些规则限制改写与正式晋级？"
};
const nodeNames: Record<string, string> = {"collect": "采集", "clean": "清洗去重门禁", "index": "证据索引", "event_summary": "事件摘要", "event_overview": "事件总览", "event_signal": "当日事件精选", "political_analysis": "政局解读", "historical_analog": "历史经验", "product_synthesis": "品种研判", "skeptic_review": "交叉质证", "event_fusion": "预测定案", "seven_product": "七产品预测", "shadow_eval": "复盘校准", "counter_scan": "反证扫描", "unified_memory": "统一记忆舱"};
export function PipelineTechnicalDrawer({ view, onClose, onNode }: { view?: string; onClose: () => void; onNode: (id: string) => void }) {
  const lens = pipelineLenses.find(item => item.title === view);
  return <Drawer title={view === "验证证据" ? "验证证据与适用边界" : `${view ?? ""} · 技术剖面`} open={Boolean(view)} onClose={onClose} width={470} mask={false} className="pipeline-node-drawer" destroyOnClose footer={<div className="pipeline-drawer-footer"><Button size="small" aria-label="关闭" onClick={onClose}>关闭</Button><span>只读观测 · 不可编辑</span></div>}>
    <div className="pipeline-technical-content" data-testid="pipeline-technical-view">
      <div className="pipeline-lens-intro"><span>工作原理</span><h2>{lensQuestions[view ?? ""] ?? "判断依据从哪里来，怎样核对本次发行？"}</h2><p>下面是设计与审查口径；当前配置、输入输出和运行记录请打开对应节点核对。</p></div>
      {(lens?.sections ?? validationSections).map(([title, text]) => <section key={title}><h3>{title}</h3><p>{text.replace(/no_prior/g, "无可用先验").replace(/supporting_event_ids/g, "支持事件编号").replace(/affected_products=\["crude"\]/g, "适用品种仅原油")}</p></section>)}
      {lens && <p className="pipeline-lens-next">打开节点，核对真实组件与本次运行。四个链阶段提供当前系统提示词原文；未提供的配置不会补造。</p>}
      {lens && <nav aria-label="相关节点">{lens.nodes.map(id => <Button key={id} size="small" onClick={() => onNode(id)}>{nodeNames[id] ?? "查看节点"}</Button>)}</nav>}
    </div>
  </Drawer>;
}
