# Zcode 审计补齐与修复记录（2026-09-13）

结论：当前生产仍有已实测的问题，不能沿用“只剩两项”的结案描述。本轮修复已获授权并发布；在线备份、隔离恢复、生产索引重建和三条新闻日期纠正已完成。全应用全部通过仍不能据此宣称。

## 当前事实与修复

| ID / 级别 / 分类 | 页面或组件 | 复现、实际与影响 | 期望与本地修复 | 验证状态 |
|---|---|---|---|---|
| C01 / P1 / A | 日更备份 | `verify_backup` 在运行中的 SQLite 上使用 `copy2` 主文件；完整性通过仍可能遗漏已提交 WAL 数据 | 改为 SQLite online backup，恢复副本核验保留；测试保持 writer 打开且 WAL 不 checkpoint 后仍恢复到新增行 | 隔离测试通过；生产 online backup、integrity_check 与隔离 restore_integrity_check 均通过 |
| C02 / P1 / A | `/?module=assistant`；`/api/v1/rag-index/status` | 点击“今天上游成本压力怎么看”；9月13日回答引用7月16—20日原油数据；索引7月23日构建但返回 ready | 24小时以上索引标记 snapshot_expired；跳过旧向量检索，读取当前证据库，保留时点、审查状态和文档类型过滤 | 公网问题实测1次，索引状态可重复读取；本地回归通过；已追加当前问题实时检索、原始发布日期过滤；最终问答证据见执行结果 |
| C03 / P2 / A | `/?module=events`；GDELT 接入 | 三条新闻 published_at 晚于 created_at；Brent 原站 publish-date 为2026-09-10，应用记录12 Sep 2026 14:30:00 +0000 | 聚合时间保留到 raw.discovery_timestamp，不再直接充当发布时间；详情补全以原站日期为准，支持 publish-date 元数据 | 原始接口及原文取证；新逻辑测试通过；三条生产日期均已据原文纠正，原值及证据哈希保留 |
| C04 / P2 / A | `/?module=market` | POY 库存、开工已软移除，下方卡片仍显示“已更新”，误导有效覆盖 | 显示“已停用”，不改变已接受软移除边界 | Chrome实测；本地浏览器回归及 Chrome 生产复验通过 |
| C05 / P2 / A | `/?module=events` 中文搜索 | 查询仅覆盖英文标题、原始摘要、来源和正文，遗漏已生成的中文事实摘要 | 查询及计数都增加中文事实摘要检索 | 隔离 SQLite 行为测试通过；Chrome 搜索“布伦特”返回5条，命中英文标题下的中文摘要 |
| C06 / P2 / A | `/?module=assistant` 引用文本 | 本轮真实回答出现“业务数据:业务数据”；通用文案替换破坏证据 ID | 结构化回答保留精确 doc_id/evidence_id，字段名显示为“证据” | 格式化回归通过；生产引用点击和完整映射仍待新版本复验 |

六项均为工程修复，不能靠等待自然日解决；没有通过隐藏旧数据日期或编造新观测解决新鲜度。

## 纠正原审计结论

- “备份8天未更新”是目录核查不完整：实际目录为 `shared/local-production/db-backups`，9月13日12:38、12:48、12:57已有备份。真正问题是备份方式未保证 WAL 一致性。
- 日更报告实际运行04:53:37—04:58:02 UTC，状态 ready_with_warnings。报告仍列出来源隔离失败、21格正式晋级未通过、角色记录仅固化、质量需复核等警告。
- 防休眠进程安装不能证明连续运行、断网恢复或备份可用性；长期观察仍未完成。
- TNC 当下未复现异常不能证明历史异常必然来自外部，本轮不修改其原始账本。
- 聚酯库存/开工软移除、DCE/CCF软移除继续保留；不为追分重新启用。
- 本轮不是一次新的全应用全量验收，不给未经覆盖的新总分；Zcode的78.5分不作为本轮独立评分。

## 验证和证据

证据根目录：`/path/to/project/agent-context/zcode-closeout-20260913/`。

- `ai-answer.png`：Computer Use 控制用户 Chrome，推荐问题实际回答截图。
- `rag-index-status.json`：公网语义索引原始响应，7月23日索引仍 ready。
- `gdelt-articles.json`：三条 GDELT 原始日期响应。
- `gdelt-original-brent.html`：公开原文 HTML，包含 `<meta name="publish-date" content="2026-09-10">`。
- `backend-tests.log`：备份、语义检索与图检索回归49项。
- `news-tests.log`：新闻与事件搜索回归（最终结果见日志）。
- `e2e.log`：停用卡片及引用ID回归2项。
- `build.log`：npm run check，通过；保留既有大分块构建提示。
- `candidate-manifest.json`：候选代码和构建产物的逐文件SHA-256及字节数；工作区有既有未提交改动，不能以Git HEAD冒充候选版本。
- `evidence-manifest.json`：本轮证据的绝对路径、SHA-256和字节数。

复用：沿用项目现有 SQLite/检索/测试能力，无新增依赖或外部服务。在线备份采用标准库接口，与现有管理脚本使用的模式一致。

## 已授权生产执行

当前生产：`20260913T063613Z-350e085f330cf0de`。前后端版本一致，代码包不含生产数据。原始回滚锚点 `20260913T044640Z-cc3ee0864660b8bf` 保留；回滚代码不会撤销经核实的日期纠正。日期修改前记录均在证据目录，不恢复整库。

- SQLite online backup 与隔离恢复完成；WAL 回归测试覆盖尚未 checkpoint 的已提交数据。生产恢复副本三张核心表计数：forecast_price_points 12653、industry_observations 1503、market_observations 111502。
- 生产索引 `semantic_index_fd8c3dd9607e4a16aeaaca40ace80c79` ready，9694份文档、9772个向量。修正管理脚本未加载生产环境配置的问题；重建采用影子索引，完成后才切换。
- 修正重建在向量计算期间长期占用 SQLite 写锁：分批提交未激活的影子数据，模型计算期间其他连接可写。并发写入回归通过，生产构建完成。
- 原油新闻三条日期：OilPrice `2026-09-09T04:30:00-05:00`；Bankingnews `2026-09-10`；Vanguard `2026-09-11`。最后一条由 Chrome 原站页面直接核验；日期精度为天的来源不编造时区。原聚合时间保存在 raw 审计信息。
- 新闻再采集不能覆盖已经原文核实的日期；新闻聚类更新时间与事实发布时间分离，防止旧闻重新聚类后冒充近期材料。
- “今天/当前/最新”问题直接读取实时证据，不等待向量模型冷启动。事实时间门槛为价格类7日、新闻类14日；明确历史/指定时点查询保留历史检索。无原始日期的新闻不作为当前事实。
- 中间版本 AI 在索引并行重建时超时，后端记录119530毫秒；已保存失败截图，未隐去失败。最终版本追加实时检索修复，回归17项通过。
- 公网只读 smoke 25/25通过，包括 health live/ready/deep、版本一致、索引和8个模块入口。入口HTML通过不代表模块全部交互通过。预测评估仍 blocked，正式晋级0/21，未通过修改门槛粉饰。

## 新增工程回归

`index-lock-tests.log` 15项；`current-live-tests.log` 17项；`manager-tests.log` 44项；`event-time-tests.log` 23项；`time-policy-tests.log` 30项；`final-followup-tests.log` 50项。测试集有重叠，不累计成独立总数。`build-final.log` 前端构建通过，既有大分块提示仍在。

## 验收边界

本轮 Chrome 直接检查了行情停用卡片、事件中文搜索和日期、外部原文、AI真实问答及重启恢复。地图拖动/缩放、所有产品深度交互、键盘与200%缩放、浏览器控制台全量网络审计等，本轮没有补全。
长期自动化跨日连续性、断电断网恢复、来源持续发布和预测自然到期均不能由本次运行证明。不新增总分，不沿用78.5分冒充独立评分。

## 最终 AI 复验与未结问题

Chrome 点击“今天上游成本压力怎么看”后成功返回，服务端 HTTP 200 耗时10403毫秒（前次失败119530毫秒）；浏览器于54秒检查时已完成，未测得准确首字时间。正文证据ID保持原样，引用卡片可打开详情及原始来源链接。此次回答引用9月日期，不再复现最初7月行情冒充当前数据的问题。

但不能宣称AI事实质量通过：

- C07 / P1 / G：`/?module=assistant`，点击同一推荐问题，正文多条论断被质量层标为“上下文中未找到足够直接支持”，侧栏参考材料与正文引用对应不完整。期望每条事实能映射到直接证据；实际只能作待核验推断，影响判断可信度。证据 `ai-final.png/.txt`、`ai-citation-detail.png/.txt`。最终版本实测1次，稳定复现尚未确认；原因需进一步区分中英文支持度判断、模型生成和材料映射，不能通过降低门槛结案。
- C08 / P2 / G：生成前的参考材料出现中文替换字符乱码；生成后的材料另有RSS HTML片段、重复条目和日期截断。证据为Chrome读取及 `ai-final.txt` 中的RSS片段/重复/截断；乱码未单独截图，不将根因判定为已证实。影响可读性；需要进一步检查来源解码与展示清理。

上述新发现没有列为已修复。发布的工程修复已验收，但“全部修复”总目标仍未完成。不得把25项只读smoke通过解释为AI内容和全应用验收全部通过。

## 证据索引补充

- `production-backup.json`：在线备份与隔离恢复结果。
- `deploy-live-current.json`：最终不可变发布ID。
- `index-rebuild-closeout.json`：影子索引成功切换结果。
- `post-rebuild-smoke-result.json`：25/25公网只读验收。
- `ai-http-timings.json`：真实AI请求耗时，包括失败和恢复。
- `search-fixed.png`、`market-fixed.png`：中文搜索和停用状态生产截图。
- `vanguard-original.txt/.png`：Chrome原站9月11日日期证据。
- `date-correction-result.json`、`vanguard-date-result.json`：三条核实后的日期修改证据。
- `nigeria-capture-time.png`：未获得原文前诚实显示采集时间的中间状态，不代表最终发布日期。
- `evidence-manifest.json`：证据逐文件绝对路径、SHA-256及字节数。


最终健康复核：curl live 200/1.439秒、ready 200/1.931秒；默认 urllib 客户端另遇403，原因未判断，未将其计为应用业务故障。生产smoke与Chrome可访问，客户端差异保留为未决网络观察。
