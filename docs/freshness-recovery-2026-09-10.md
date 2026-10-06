# 新鲜度排查与恢复 — 2026-09-10

## 已恢复

Chrome初始及重新读取均显示9/9 02:05旧快照；公网ready返回530、正文1033，本机8000ready返回200。隧道进程仍运行，但日志9/9 16:11:55—59 UTC四条边缘连接全部断开。
重启已有com.poydty.agent.cloudflare-tunnel，退出0；没有代码部署、数据库或配置修改。
恢复后公网ready200/3.756秒、prices200/2.036秒、snapshot200/3.412秒。Chrome快照更新到9/10 00:17，连接降级提示消失。
进入行情一度仍显示7月旧回退值，强制刷新后POY9080、DTY10122.14，观察日期9/8。不能声称所有软恢复/缓存分支已修复。

## 仍存在的日期缺口

| 数据 | 真实观察日期 | 判断 |
|---|---|---|
| Brent/WTI | 9/10 00:06左右上海时间 | 当次读取约10分钟，近实时 |
| PX/PTA/MEG | 9/9 23:00左右 | API按超过1小时标延迟；休市时间口径尚需核对，不能当作停止采集 |
| POY/DTY | 9/8 | 纺织网现有单品条目相同；9/9榜单可能提供更近期数据，尚未接入核对 |
| 石脑油 | 9/4 | 当前中文来源明确日期摘要为9/4；不可将无日期页面数字与旧日期拼接 |
| 历史曲线/库存/开工/加工差 | 部分7月 | 未补齐，不能因快照刷新而标新鲜 |

公开来源旁证：https://info.texnet.com.cn/list--20-.html 和 https://zh.tradingeconomics.com/commodity/naphtha 。
Cloudflare 1033说明：https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1033/ 。

## 后续范围

此次完成连接恢复，未完成全数据新鲜度修复。优先核对9/9纺织榜单与现有解析覆盖、交易时段口径、断线恢复后行情缓存刷新、历史序列补齐。不能改抓取时间来冒充观察日期。
AI单次耗时已由用户接受，不再以原30秒目标作为当前优化阻塞；不代表预测质量或所有AI指标通过。
未改代码故未运行代码回归；以公网HTTP、Chrome重读/强制刷新验证本次服务恢复。
证据目录：/path/to/project/agent-context/freshness-20260910；evidence-index.json记录绝对路径/大小/SHA256。
