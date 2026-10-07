# Contributing

Small, focused changes are easier to verify. Start from an issue describing the behavior, affected inputs and expected result.

1. Use npm for the frontend and uv with Python 3.11+ for the backend.
2. Preserve source dates, citations and issued forecast records. Do not invent evidence to fill a page.
3. Document API changes in `docs/openapi.yaml` and add positive and negative behavior tests.
4. Run `npm run check`, affected E2E tests and isolated backend tests. See [local setup](../docs/getting-started.md).
5. Keep credentials, databases, provider caches, private documents and generated test outputs out of commits.
6. Explain what changed, why, how it was checked and any remaining limitations in the pull request.

Do not bypass logins, CAPTCHAs or paywalls when adding a source. Source discovery is not permission to redistribute its content.

Contributions are provided under Apache-2.0 unless explicitly agreed otherwise. See [LICENSE](../LICENSE).

## 从一个小任务开始 / Starter tasks

先在 Issue 中说明选择的任务和复现方法，再提交最小修改。测试只使用合成资料；不把外部模型收费作为贡献前提。

- **无密钥样例的端口冲突提示** — `scripts/start-sample.py`。验收：占用端口时清楚报错，退出不留下子进程；不使用生产库。
- **英文样例标识的可访问性检查** — `src/main.tsx`。验收：样例标识被读屏识别，窄屏不覆盖导航；只改样例模式。

[报告问题 / Report a bug](https://github.com/luxiaoyu0731/poy-dty-agent/issues/new?template=bug_report.yml) · [首次使用反馈 / First-use feedback](https://github.com/luxiaoyu0731/poy-dty-agent/issues/new?template=first_use.yml)
