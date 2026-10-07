# 公开源码包验证 · 2026-10-06

范围：新的 GitHub 源码仓库；原私有历史、生产部署、生产数据库与预测效果回放不在本次发布范围。

## 观察结果

| 检查 | 本次结果 |
| --- | --- |
| `npm run check` | exit 0；TypeScript、素材检查与生产构建通过；仍有大 chunk 提示 |
| `npm run test:e2e -- --reporter=line` | 228 passed，1 skipped；本次关闭失败重试 |
| `uv run --project server python -m pytest server/tests -q` | 3498 passed |
| 后端完整覆盖率测试 | 3498 passed，4 warnings；84.93%，达到现有 70% 门槛 |
| `npm run visual:check` | 桌面 2.68%、手机 0.00%，均低于 3.5%；独立于参考生成再次执行 |
| 公共入口 Node 测试 | 27 passed，0 failed |
| Ruff | All checks passed |
| pre-commit 全文件检查 | 全部通过，含 Semgrep ERROR 与 staged Gitleaks |
| 当前文件 Gitleaks | 未发现密钥；公开包不带旧提交对象 |
| npm / Python dependency audit | 未发现已知漏洞 |
| Trivy HIGH / CRITICAL | 未发现依赖漏洞、配置问题或密钥 |
| 特定隐私模式检查 | 原服务器地址、旧 SSH 密钥名与个人本地目录标识均为 0 |
| 视觉更新负向检查 | `--ci --update-mobile-baseline` 拒绝执行，exit 1 |

## 发布审查

**公开源码：GO（2026-10-07 完整 CI 通过，见文末）；先前 NO-GO 及修复经过保留如下。生产部署：本次未执行。**

操作员批准新公开仓库、Apache-2.0、既有前端素材再分发与已确认手机方案的参考迁移。客户资料、Apple 字体、数据库和旧 Git 历史已排除；原私有仓库保留。GitHub CI 会继续运行包括 CodeQL 在内的远程检查，远程检查结果需单独观察，不能从本地结果推定。

已知限制：工作台、样式与存储模块仍较大；构建大 chunk 警告保留。未进行加载速度对照实验或新增预测效果验收，不宣称性能提升百分比。Python 审计使用同一虚拟环境的真实路径，工具提示了 symlink 路径差异；不是一个独立新安装环境的验证。

## 本地原始证据摘要

日志保存在操作员本地任务目录，不公开其中的工作区路径。以下摘要便于核对；命令输出未伪造，也未用历史报告替代。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `check-complete.log` | 2817 | `0d373553d044b47df8c535568db17ac9613fa45cdef308cf39c6d939bda2924f` |
| `e2e.log` | 982299 | `6dd36efaec96d30b7416eafe7f1d287f2bbacef4fea59ad01a9da0a33904b855` |
| `backend.log` | 3953 | `5341de5a4fed8bcf26fb9048998f798d46ae79d6515bfe6f1d68f10966346704` |
| `coverage.log` | 23291 | `dc365748c231da2bbf2243dfe079250d4100ab802799a636b7c60dd85b2e4446` |
| `visual-final.log` | 346 | `3406c30b798fbccec88621bee6d9a36a62095957544dbc2f5c219f14356cf68e` |
| `precommit-complete.log` | 800 | `99ff44e9d83b5bffc7ef81820c21534ca4681d3315ab09786de504f27dc75596` |
| `node-tests.log` | 2194 | `cdc68a6fad65a6bbd03a70c1e412ea9af6fd5bc6758d579eea5eaaccf8b9d062` |
| `ruff.log` | 19 | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `npm-audit.json` | 363 | `d9186a1ac1061183563ab6d60d2f7ef565982758b3969184c876b96dec0025e9` |
| `pip-audit.json` | 5184 | `02b09becd22ea5c3a351be84ccc0b4aa113bebd92b8f4867c55c571481a21d2d` |

## 远端 CI 复核（2026-10-06）

[首次 main CI](https://github.com/luxiaoyu0731/poy-dty-agent/actions/runs/37459774017) 已完成，结论为 failure。这是新增的跨环境证据，不能继续仅引用本地全绿。

- Security review：success。
- 后端：3497 passed、1 failed；测试运行生产环境加载脚本时 Linux runner 没有 `/bin/zsh`。
- 前端：225 passed、2 failed、1 flaky、1 skipped。石脑油单元断言未隔离真实价格历史，手机原生 pinch 模拟在 Linux 环境未达到预期比例；会话取消用例发生重试通过。
- CodeQL：报告 22 项未豁免发现，需逐项审阅，不能把它们直接认定为误报或修改门禁放行。

本次同步审查文档，保留原始 CI 证据；未更改生产业务或掩盖失败。后续需隔离测试数据、修复跨平台 shell 与手势验收，并处理 CodeQL 发现后重跑。

## 首次体验修复验证（2026-10-07）

新增隔离合成样例，不读取生产库、不调用付费模型。Node CI 对齐 Vite 的运行要求；Linux 环境脚本改用 bash；逐项修复 CodeQL 报告涉及的目标路径、错误信息、代理目标和文本匹配。未加入扫描豁免。

本批新鲜本地结果：后端 3499 passed；E2E 229 passed、1 skipped；移动端专项 2 passed；新增锁边界及主机匹配相关测试 50 passed；构建检查通过；视觉桌面 2.80%、手机 0.00%，均在原 3.5% 门槛内。移动端测试验证浏览器缩放与布局，真实手机双指手势仍需设备验证。

本批远端验收已完成，结果见下一节。

## 远端最终验收（2026-10-07）

[完整 CI](https://github.com/luxiaoyu0731/poy-dty-agent/actions/runs/37588279803) 对源码提交 `b935fe92734a88ec94b915e0f32c4a5955880fad` 全部通过：后端 3500 passed（覆盖率 85%），前端 229 passed、1 skipped，构建与视觉回归通过；Python 依赖审计未发现已知漏洞；21 项 AI 软件评测通过；Security review 与 CodeQL 门禁通过，未增加扫描豁免。AI 软件评测不代表预测效果回测验收。

本节只同步完成的验证记录，不改变源码、门槛或生产状态。
