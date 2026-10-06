# 前瞻影子预测运行说明

此入口用于在真实结果出现之前保存候选预测，随后独立评分。当前实现和模板已完成；是否已在云端启用，以安装记录和实际账本为准，不以本文件为准。

研究协议见[ADR](adr/20260926-prospective-prediction-shadow.md)。正式模型、七品种1/7/30账本、预算和历史记录保持原样。

## 命令

```sh
# 只在新的独立目录登记一次；不打开数据库。
server/.venv/bin/python server/scripts/run_prediction_shadow.py init --ledger-dir /external/prediction-shadow-v1

# 需要对所指生产数据库具备明确只读授权。没有 --as-of 或导入预测参数。
server/.venv/bin/python server/scripts/run_prediction_shadow.py run \
  --ledger-dir /external/prediction-shadow-v1 --database /explicit/path/agent.db

# 检查真实尝试终态、数据/候选可用性和26小时新鲜度；不连接数据库。
server/.venv/bin/python server/scripts/run_prediction_shadow.py status --ledger-dir /external/prediction-shadow-v1

# 已授权取得的历史JSON可用于离线预览；不会写前瞻账本。
server/.venv/bin/python server/scripts/run_prediction_shadow.py preview --input /external/vintages.json
```

生产源仅以SQLite URI `mode=ro` 打开，另设`query_only`，45秒查询期限和每源20,000行上限。超过上限拒绝，不静默截断。不导出原文或密钥，不调用模型API，不迁移表。冻结快照保留在独立目录。

每上海业务日最多一份预测，时间取实际运行和耐久写入后的时钟，不补前一天。原始特征只用本次开始前已获得的数据；发报时间晚于保存时间，结算以发报时间为准。120秒超时、跨日、时间倒退和缺发报回执不得当成成功预测。权限/hash不能对抗能够修改主机时钟和全部文件的恶意管理员。

## 目录与检查

- `manifest.json`：登记时间、代码/依赖锁/运行时及协议指纹。任何变化必须新实验目录。
- `inputs/<hash>.json`：每轮只读输入快照；不是原始数据库副本。
- `days/YYYY-MM-DD/forecast.json`：不可覆盖的原始预测，包含训练/输入哈希、模型和比较基线。
- 同目录`issue.json`：预测已耐久写入后生成的发报回执；没有它只算未发报。
- 同目录`outcome-N.json`：到期后的首次结算；不覆盖历史答案。
- `attempts/*/start.json`与`finish.json`：先记尝试再执行，失败不借用旧成功状态。
- `runs/<hash>.json`：逐品种、逐合同的正确率、同样本基线、覆盖率、缺跑天数、类别召回、不重叠片段和条件概率指标。

`ok`仅表示当前周期输入与候选产出正常；`degraded`代表输入/候选/发报存在缺口；`unknown`代表未完成或检查已过期；`failed`代表执行错误。CLI分别返回0或非0，并发占用返回75。没有成熟结果时正确率为null。`effect_validated`和`automatic_promotion`始终false；数量过门槛也不自动切模型。

候选总计14次方向判断/日：六种下游品种×两个合同的上游信号，加DTY两个合同的直接分类。原油只作驱动。日历D1与下一次报价分开报告，不相加制造样本量。数据完整但候选全部弃权也会降级。

## 云端部署方案（执行前审核）

1. 在`/opt/agent`确认当前实际backend容器的image ID与生产数据named volume；不读取应用.env密钥，不复制数据库。保留实际base image ID和现有服务状态。
2. 将本次精确文件包放入独立构建目录，用当前生产镜像作基础离线构建：

   ```sh
   docker build --pull=false --network=none \
     --build-arg BASE_IMAGE=<已核实并固定的本地镜像标签> \
     -f scripts/cloud/Dockerfile.prediction-shadow -t poydty-prediction-shadow:20260926 .
   ```

   只覆盖Dockerfile列出的预测源码，不安装依赖。原应用镜像、容器、前后端发布标识都不改变。记录最终镜像ID，不以可变标签运行。
3. 先以临时影子目录和隔离SQLite夹具验证该镜像的init/run/status、不可覆盖重试、只读挂载及退出码；不在此阶段连接生产库。核对实际镜像内代码与交付清单。测试失败不启用定时器。
4. 新建`/opt/agent/state/prediction-shadow/v1`，仅赋给镜像中的运行UID/GID（当前Dockerfile为10001）；写入root私有配置`/opt/agent/state/prediction-shadow.env`：

   ```sh
   SHADOW_IMAGE_ID=sha256:<核实的64位镜像ID>
   SHADOW_DATA_VOLUME=<核实的生产named-volume名称>
   SHADOW_DB_READER_CONTAINER=<挂载同一数据卷的现有backend容器名>
   SHADOW_LEDGER_DIR=/opt/agent/state/prediction-shadow/v1
   ```

5. 安装`prediction-shadow.sh`至`/opt/agent/bin/`，安装对应`.service/.timer`。执行init，再做一次当前时点run，检查`issue.json`、输入与候选数量，以及源数据库没有写入路径。空闲 SQLite 会收起 WAL 辅助文件；wrapper 先核对现有 backend 挂载的是同一卷，再在其中保持一个 mode=ro/query_only 连接（最长165秒，仅查询 schema，结束即关闭），让 SQLite 正常维持锁文件。影子容器源卷仍为只读，不 checkpoint、不复制整库、不更改业务记录；reader 启动失败则不运行预测。
6. 首次真实记录通过检查后启用timer，每天08:00Asia/Shanghai执行。进程上限180秒，内部发报最大120秒；重启不补跑旧时间。确认`systemctl list-timers`和下一次计划，保存image ID、manifest、首次回执与run哈希。
7. 纳入现有运维巡检的只读`status`调用；systemd失败和26小时未完成必须可见。当前任务未替用户新增外部通知渠道或更改已有告警配置。

隔离限制由wrapper强制：固定image ID、`--network none`、容器文件系统只读、生产卷只读、仅独立影子目录可写、0.5CPU/768MiB/64进程。不重启backend、scheduler或旧Mac任务。

**回退：**停止并禁用`poydty-prediction-shadow.timer`，确认该oneshot任务结束；保留所有影子记录、固定镜像和配置，不删除数据。正式应用无需回滚。若代码/依赖/来源合同变化，旧实验用旧镜像继续结算，新的实验另建目录；不能把两个协议的数据混评分。

## 验证结果与边界

本机实现证据位于`/path/to/project-context/prediction-shadow-20260926/`；本轮08:00部署证据位于`/path/to/project-context/prediction-shadow-activation-20260926/`。云端目标镜像已构建并通过隔离验证。当前安装/发报/定时器状态以最新 DELIVERY 和实际账本为准。基础镜像系统包漏洞的发布决策见该目录 SECURITY_DECISION.md；未获解决或明确例外前不启用自动任务。历史JSON preview不算前瞻证据。
