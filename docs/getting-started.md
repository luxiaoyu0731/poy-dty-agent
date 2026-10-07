# 本地运行与隔离验证

本仓库不包含生产数据。请使用自己的开发目录、数据库与模型密钥；不要复制线上数据库来启动演示。

## 安装

Node.js 22.12+、npm、Python 3.11+、uv。

```bash
npm ci
uv sync --project server
cp .env.example .env
```

`.env` 默认仅用于本地开发。模型密钥保持空时不会产生真实模型回答；数据不足会显示缺口。首次 FastEmbed 下载需要网络和磁盘空间。

## 启动

后端在一个终端启动，禁用自动定时采集：

```bash
mkdir -p "$HOME/.cache/poy-dty-local"
chmod 700 "$HOME/.cache/poy-dty-local"
SQLITE_PATH="$HOME/.cache/poy-dty-local/agent.db" \
INTRADAY_PRICE_SCHEDULER_ENABLED=false \
AGENT_GOVERNANCE_SCHEDULER_ENABLED=false \
uv run --project server uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000
```

另一个终端启动前端：

```bash
VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1
```

打开终端打印的本地 URL。空数据库出现空态是正常现象；运行测试会在独立目录创建测试资料，不会伪装成真实行情。

## 验证

```bash
npm run check
npm run test:e2e
```

后端的 DG-01 测试要求数据库与临时文件放在同一个真实目录下，目录权限为 0700：

```bash
mkdir -p "$HOME/.cache/poy-dty-tests/tmp"
chmod 700 "$HOME/.cache/poy-dty-tests" "$HOME/.cache/poy-dty-tests/tmp"
DG01_TEST_DB_ROOT="$HOME/.cache/poy-dty-tests" \
TMPDIR="$HOME/.cache/poy-dty-tests/tmp" \
SQLITE_PATH="$HOME/.cache/poy-dty-tests/agent.db" \
PYTHONPATH=server:. \
uv run --project server python -m pytest server/tests -q
```

不要对生产库运行测试。实际抓取、付费模型调用和生产调度都需要单独配置；简单启动页面不会代替这些操作。

## 先体验合成样例

```sh
npm run demo
```

只在新建临时目录写样例数据库；不加载 `.env`，不启动定时器、不使用模型密钥。可指定 `-- --api-port 18001 --web-port 15175`。关闭终端任务会停止服务并移除临时目录；这不是线上数据或预测质量演示。

## One-command synthetic trial

After cloning, run `npm run demo:setup`. The command installs the repository lockfiles, then starts the isolated synthetic sample. Use `npm run demo` afterwards. Initial setup needs internet for packages; it does not call a paid model. Stop with Ctrl+C. On macOS/Linux, child processes and the sample database are cleaned up. Windows process lifecycle is not validated.
