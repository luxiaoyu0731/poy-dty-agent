# Data Import Templates

这些模板用于 0 成本真实数据 MVP。文件只定义字段，不包含伪造行情值；公开源下载、免费 API、OCR 或人工补录时按列名填充。

## Recommended Source Choice

- 当前免费官方源：EIA、OPEC、OFAC、FRED、CFTC、CFETS/SAFE/PBOC、INE、ZCE、GACC、UN Comtrade。DCE 已软移除，仅保留历史审计。
- 内部手工源：CSV、截图 OCR、个人笔记、预测复盘标签。
- 0 成本默认不包含 CCF、隆众、卓创、ICE/CME 实时行情、Reuters/Bloomberg、Kpler/Vortexa 等付费/授权源；已获用户授权的 CCF 数据必须走授权页面/导出/上传文件和标准入库流程。

## Files

- `public_observations.csv`：EIA、FRED、CFTC、INE/ZCE、CFETS/SAFE 等当前公开日/周/月数据；不接受已软移除的 DCE 新数据。
- `industry_chain_observations.csv`：内部手工录入的 PX/PTA/MEG 现货、加工费、价差、库存、开工率、装置状态。
- `polyester_chain_observations.csv`：POY/DTY/PX/PTA/MEG 手工价格和行业观察 CLI 导入模板，配套说明见 `polyester_chain_import.md`。
- `poy_dty_spot_slow_queue.csv`：POY/DTY 公开网页历史现货价 Computer Use 慢队列。
- `poy_dty_public_seed_queue.csv`：已从公开页面核验的 POY/DTY 种子观测，转换后写入 `polyester_chain_observations.csv`。
- `industry_daily_manual_checklist.md`：每天手工/OCR 补录 POY/DTY 全国口径和上游化纤链数据时的填报说明。
- `events_and_alerts.csv`：OPEC、EIA、OFAC、政策、航运、装置检修等事件输入。
- `news_observations.csv`：新闻抓取回填或 Computer Use/人工整理后的新闻文章输入。
- `computer_use_slow_queue.csv`：对脚本不稳定/需要人工判断来源的 Computer Use 慢队列任务表。
- `event_direction_review_queue.csv`：从 as-of 回测生成的事件方向人工复核队列，只用于确认方向标签；填写后先用 `apply_event_direction_reviews.py` dry-run，再显式 `--apply` 写回本地库。
- `prediction_review_labels.csv`：预测账本到期复盘和人工标签。
- `hs_code_mapping.yaml`：贸易流相关 HS 编码候选，入库前需要人工确认。

## Import Rules

- `observed_at` 推荐使用 `YYYY-MM-DD` 或 ISO-8601；日度/月度官方数据可只填日期。
- `source_id` 必须来自 `server/source_registry.json`。
- `tier` 必须是 `A`、`B`、`C`、`D`。
- 所有数据必须保留 `source_url`、`source_id` 和采集/录入人；不得绕过登录、验证码、付费墙或许可限制。
- A/B 级数据可以进入预测；C 级事件需要交叉验证；D 级弱信号只进入人工复核。
- 行业手工数据允许先填状态和 notes、不填 `value`；这类占位记录会进入证据队列，但不会抬高系统置信度。

## CCF Industry Indicator Rules

授权 CCF 行业指标进入 `industry_observations` 时，主库只接受已定义的 canonical 指标。网页表格/OCR/导出文件可以使用中文列名和常见别名，导入脚本会归一到下表：

生产门禁按“工作日每日授权页检查/采集”判断新鲜度：`observed_at` 可以随 CCF 指标自身频率保持周频或日频，但 `notes` 或 `raw` 中必须保留 `captured_at`，用于证明当天已经检查过授权页面；周末不因未采集而阻塞。

| 页面含义 | product | metric | unit | frequency |
| --- | --- | --- | --- | --- |
| 聚酯开工 / 聚酯负荷 | `POLYESTER` | `polyester_operating_rate` | `%` | `weekly` |
| POY 库存 / POY 库存天数 | `POY` | `poy_inventory` | `天` | `weekly` |
| DTY 库存 / DTY 库存天数 | `DTY` | `dty_inventory` | `天` | `weekly` |
| 聚酯利润 / 聚酯现金流 / 聚酯加工差 | `POLYESTER` | `polyester_profit` | `元/吨` | `daily` |
| POY 利润 / POY 现金流 / POY 加工差 | `POY` | `poy_profit` | `元/吨` | `daily` |
| DTY 利润 / DTY 现金流 / DTY 加工差 | `DTY` | `dty_profit` | `元/吨` | `daily` |

允许的列名别名包括：`日期/observed_at`、`产品/product`、`指标/指标名称/metric`、`数值/value`、`单位/unit`、`频率/frequency`、`来源链接/source_url`、`备注/notes`。

允许的单位别名包括：`day/days/天`、`％/%`、`元／吨/元/吨/yuan/ton/RMB/ton`。不在表内的指标会被 dry-run 拒绝，避免生成主库没有质量门禁规则的孤儿指标。

## API Import Endpoints

- `POST /api/v1/imports/public-observations`
- `POST /api/v1/imports/industry-observations`
- `POST /api/v1/imports/events`
- `POST /api/v1/imports/news-observations`

请求体直接发送 CSV 文本，`Content-Type` 使用 `text/csv`。成功导入后接口会返回 `data_snapshot_id`，预测账本可以用这个快照 ID 绑定当时的证据。

授权 CCF 数据继续通过行业观测模板进入系统。PX、PTA、MEG、库存、开工率和利润可以参与成本压力判断；POY/DTY 后验评估价只用于传导验证和复盘，不是预测目标。所有授权数据必须保留来源、采集时间、许可范围和可见时间。
