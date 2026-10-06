# ADR 0008：CCF 范围重开尝试——CCFGroup 指标捕获

状态：尝试完成，未认证捕获判定不可行；凭据化捕获保持关闭，待操作人另行决定。

## 问题

聚酯库存天数、开工率、加工差无免费结构化当前数据源（2026-09-11 审计及 G7/G8 结论）。
唯一近免费来源为 CCFGroup（化纤信息网英文站），落在 ADR-0004 的 CCF 软移除冻结范围。
操作人于 2026-09-12 明示"重开试试看"，本 ADR 记录该决定与尝试结果。

## 决定与边界

- 仅重开"评估 CCFGroup 未认证抓取"这一范围，不恢复 CCF 价格标签
  （`ccf_dom_daily`/`ccf_manual_export` 仍冻结），不恢复授权 CSV 导入通道。
- 遵守不绕过登录/付费墙/验证码的硬边界。

## 尝试证据（2026-09-12，全部为当日实测）

- 新闻中心列表、Weekly 列表（`newslist.php?Class_ID=2G0000`）、PFY 频道页可匿名读取。
- 存在每周系列 "Polyester filament yarn market weekly"（8/31、8/24、8/17、8/10 均有）。
- 该周报免费正文仅剩 3 条要点（价格方向/加工差方向/销售水平）；库存天数、开工率、
  加工流数字全部在会员墙后（Member ID/7-day trial/Create Account）。
- Database 页（Inventory/Operating rate/Cash flow）匿名访问仅返回导航与规格标签，
  数值为会员门；PFY market morning express 正文同为登录表单噪音。
- 2024 年 Insight 例文（inventory of POY was at 22.x days）属旧免费口径，现已不存在。
- 日评短文偶发 0 字节响应（"One major PFY unit to cut production"两次抓取一次为空）。

## 结论

1. 未认证捕获 CCFGroup 指标数字：不可行。不新增 CCFGroup 新闻源或抓取任务
   （免费残余为登录表单噪音与每周 3 条无数字要点，噪声/价值比不成立）。
2. 库存/开工/加工差结构化序列的可行路径只有两条，均需操作人进一步决定：
   - 注册 CCFGroup 账号并按历史 CCF 授权会话捕获模式（Safari 会话基建仍在），
     该路径属凭据化访问，须另行批准采集范围与频次；
   - 或维持关闭，以 100ppi/纺织网新闻中的叙述性数字作为定性佐证（已上线）。
3. 价格标签侧（CCF 石脑油/PX/PTA/MEG 价格）维持 ADR-0004 软移除不变。

## 影响

无代码、调度、数据库或白名单变更；本 ADR 仅固化决定与证据。
后续若走凭据化捕获，须新立 ADR 明确账号授权、捕获清单、频次与存储边界。

## 验证

本文所有站点行为均为 2026-09-12 实测；证据脚本输出见会话记录
（article 28002B/2697C、database 90466B 三页同内容、morning express 正文登录噪音）。
