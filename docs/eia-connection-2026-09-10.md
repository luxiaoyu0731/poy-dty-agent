# EIA 接入实测记录（2026-09-10）

用户提供的准确文本 key 已通过 EIA 三组 API 验证，无需因申请满三个月而更换。首次截图转录存在大小写字符错误，准确文本验证成功。

## 本次变更与验证

仅追加生产运行环境 EIA_API_KEY 并重启后端；未修改代码、提交或发布新版本，未直接操作数据库。沿用已有 EIA 采集器与自动采集配置。

- EIA 现货、库存、供需三组官方 API 均 HTTP 200。
- 生产 readiness HTTP 200。
- 应用单源采集 HTTP 200，耗时 10.707 秒，应用报告存储 1,480 条观察、10 个序列；这不是新增唯一记录数量的证明。
- 公网 market-observations 按 EIA 来源回读 HTTP 200，耗时 2.190 秒，取得 100 条记录；部分记录创建时间早于本轮，说明本次含既有数据。
- 来源状态为 configured / active / ok。
- Chrome 实际查看行情页面并保存当前页面截图、可访问性文本。

## 实际新鲜度与限制

本次官方接口最新返回：WTI 91.48 美元/桶、Brent 96.02 美元/桶，观察日均为 2026-09-01；库存及炼厂等周度序列截至 2026-08-28。采集成功不能表示数据更新至今天。

行情 API 原油历史曲线仍截至 2026-06-19；最新展示现货来源选择仍为 FRED（数值与 EIA 一致），价格比较中另有 Yahoo 期货口径。尚不能宣称 EIA 历史已经完整整合到主图，也不能混合期货、现货计算涨跌。此次 EIA 接入不补齐 POY、DTY 等行业现货、库存和开工率缺口。

现有自动采集列表包含 EIA，运行包装脚本读取该环境配置；本轮仅验证一次采集成功，连续自然日稳定性仍未验证。

首次应用 POST 因缺少同源请求头返回 403，按已有同源规则补齐合法 Origin / Sec-Fetch-Site 后成功；未更改访问控制。

## 回滚与证据

配置变更为追加后缀，configuration.json 记录变更前长度、追加长度及哈希；如需回滚，应核对文件未发生后续变化后只撤销本次追加并重启后端，不覆盖其他配置。本轮未执行回滚。

证据目录：/path/to/project/agent-context/eia-key-20260910/。
probe.json 为官方响应，fetch-result.json 为正常采集响应，observations-after.json 为公网回读，verification-summary.json 为摘录，source-after.json 为来源状态，market-after.json 为行情响应，market-ui.png / market-ui.txt 为 Chrome 页面证据。evidence-index.json 记录文件大小与 SHA-256。证据中不包含明文 key；临时 key 文件在验证后删除。
