# Industry Daily Manual Checklist

这个清单用于 0 成本个人研究场景：你可以把公开页、截图 OCR、券商终端肉眼观察、微信群/笔记里的可用线索整理成 `industry_chain_observations.csv`，再导入 `/api/v1/imports/industry-observations`。它是可选的上下文补充，不是正式七品种价格标签的人工值班表。

公开可见数据在本个人、非商用工作台中不需要逐源许可审批；仍不得绕过登录、付费墙、验证码、访问控制或技术限流。来源不稳定或无法公开引用时，`tier` 填 `D`，并在 `notes` 里说明。

## 每日必填顺序

| 优先级 | product | quote_type / metric | 建议口径 | value 可否为空 | 用途 |
| --- | --- | --- | --- | --- | --- |
| P0 | POY | 全国报价 | 全国主流报价或你实际关注规格 | 可以 | 直接复核成品端传导 |
| P0 | DTY | 全国报价 | 全国主流报价或你实际关注规格 | 可以 | 复核下游弹性和价差 |
| P0 | PTA | 期货/现货/加工费 | 交易所日度或公开现货线索 | 可以 | 原油/PX 向聚酯端传导 |
| P0 | PX | CFR 中国/亚洲 PXN | 公开摘要或人工观察 | 可以 | 原油/石脑油向 PTA 传导 |
| P1 | MEG | 现货/港口库存/开工率 | 全国或华东口径，注明来源 | 可以 | 判断 MEG 是否抵消成本上行 |
| P1 | NAPHTHA | 现货/文章摘录/期货参考 | 先用公开文章或慢队列确认 CFR Japan 等口径 | 可以 | 原油向 PX 的裂解成本中间层 |
| P1 | PTA-MEG-POY | spread_name / spread_value | 价差、加工费、利润 | 可以 | 判断成本压力是否被利润吸收 |
| P1 | 装置 | plant_status | 检修、重启、降负、意外停车 | 可以 | 解释供应侧扰动 |

## 字段填写规则

| 字段 | 填法 |
| --- | --- |
| `observed_at` | 数据对应日期，推荐 `YYYY-MM-DD` |
| `source_id` | 手工/OCR 默认用 `internal_market_notes` 或 `manual_industry_notes` |
| `tier` | 官方/交易所可填 A/B；人工观察、截图笔记、不可公开引用线索填 D |
| `product` | `NAPHTHA`、`PX`、`PTA`、`MEG`、`POY`、`DTY` |
| `market` | 建议统一写 `全国`，区域观察写 `华东`、`华南` 等 |
| `value` | 真实数值；如果只是“偏强/偏弱/缺报价”，留空并写 notes |
| `unit` | `元/吨`、`美元/吨`、`%`、`万吨` 等 |
| `source_url` | 可公开访问来源；没有则留空，但 notes 必须说明来源形态 |
| `quality_flag` | `reviewed`、`ocr_pending`、`needs_confirmation`、`placeholder` |
| `notes` | 写清楚规格、口径、是否含税、是否只是观察线索 |

## 空值对系统的影响

- `value` 为空的行业记录会进入 RAG 证据和人工确认队列。
- `value` 为空的行业记录不会提高 overview confidence。
- 缺少 POY/DTY/NAPHTHA/PX/PTA/MEG 真实数值时，系统可以解释事件和成本压力，但不能给出高置信价格判断。

## 自动与慢队列分工

- 当前正式/候选自动链路分别覆盖 EIA Brent、Trading Economics 石脑油公开估值、CZCE PX/PTA 和 TNC POY/DTY；各自的历史首采只作抓取后训练数据，不能回算为历史 OOS。
- 仓库中不存在 `server/scripts/auto_chain_history_fetch.py`。现有分钟/公开页采集器已能把 SunSirs 中国乙二醇现货评估保存为专属候选序列和 append-only 首次可见 revision，但它不是正式标签；DCE 已软移除，MEG 当前仍保持显式正式来源缺口，只有另行冻结标签口径并完成验收后才可晋级。
- 本表导入的 MEG 库存、开工率、装置和价格线索只作上下文证据，不会静默填充 MEG 正式 D1/D7/D30 标签。

## 推荐每日导入节奏

1. 上午：补 POY/DTY 全国报价、PTA 期货/现货、PX 线索。
2. 午后：补 MEG 港口库存/开工率、装置状态、价差。
3. 收盘后：创建 data snapshot，再写预测账本。
4. 次日或到期后：用实际 Brent/WTI 和行业观察做 prediction review。
