# 视觉回归机制

本项目没有原始 Figma 文件，因此把 `public/visual-reference/` 中的 12 张图片作为黄金参考图。所有页面的视觉验收都通过固定视口截图、像素差异图和报告完成。

## 命令

```bash
npm run visual:diff
```

运行后会输出：

- `.visual-regression/latest/actual/`：当前前端截图
- `.visual-regression/latest/diff/`：当前截图与参考图的差异图
- `.visual-regression/latest/report.md`：Markdown 报告
- `.visual-regression/latest/report.html`：浏览器可读报告
- `.visual-regression/latest/report.json`：机器可读结果

CI 或严格验收使用：

```bash
npm run visual:check
```

`visual:check` 会按 `design/visual-baselines.json` 中的阈值失败退出。当前交付工作台采用 3.5% 感知差异审阅线，避免动态日期与浏览器字体渲染造成无意义的 0% 假失败；超过阈值仍会阻断 CI。

当前只保留真实 React 组件版。12 张视觉参考图保存在 `public/visual-reference/`，仅作为回归报告中的对照资源；正常开发和生产默认不覆盖真实 UI。人工叠看参考图时，可在地址后追加 `?reference=1`。

感知差异的计算方式是：

- 先把参考图和页面截图做 4x 降采样，降低 AI 参考图纹理、浏览器字体抗锯齿、微小压缩噪声的影响。
- 再用固定色差阈值计算结构性视觉差异。
- 报告中仍保留“原始像素差”，用于诊断，而不作为验收口径。

## 单页检查

```bash
npm run visual:diff -- --page=overview
npm run visual:diff -- --page=总览看板
```

## 更换服务地址

默认检查 `http://127.0.0.1:5173`。如果本地服务跑在其他地址：

```bash
npm run visual:diff -- --url=http://127.0.0.1:5174
```

## 参考图登记

页面、参考图路径、视口尺寸和阈值登记在：

```text
design/visual-baselines.json
```

修改页面结构或新增页面时，必须同步更新这个清单。

## 资产引用检查

```bash
npm run assets:check
```

该命令扫描 `src/`、`design/` 和 `docs/` 中引用的 `public/visual-reference/` 与 `public/extracted-assets/` 资源，防止页面图标、插画或黄金参考图改名后静默丢失。

## 当前策略

- 页面截图使用参考图的原始尺寸。
- 截图时禁用动画、隐藏输入光标，并使用 `prefers-reduced-motion`。
- 输出感知差异、原始像素差、差异像素数、实际截图、差异图和参考图路径。
- 如果本地 Vite 服务没有启动，脚本会自动启动临时服务；如果已经启动，会复用现有服务。

## 上线门禁策略

当前 12 张参考图来自无 Figma 源文件的视觉重建，因此 release gate 分两层：

- CI 必跑 `npm run visual:diff` 并上传报告。
- UI 改动必须人工审阅 `.visual-regression/latest/report.html`。
- 只有在参考图由正式 Figma 或设计系统重新导出后，才把对应页面切到 0% 硬阈值。
- 没有 UI 改动的后端/API/CI PR，只需要确认 diff 报告与基线相比没有新增异常。

当前人工审阅阈值建议：

| 页面 | 感知差异审阅线 |
| --- | --- |
| 总览看板、每日晨报、预警中心、数据来源 | <= 3.5% |
| 原料链路图、重大事件、政治事件推演、成本压力预测、预测复盘、预测账本、知识图谱、AI研究助手 | <= 2.5% |

超过审阅线的页面必须附截图说明或修复后再发布。

## 2026-10-06 手机契约迁移

公开版保留旧纵向手机参考图，新手机参考改为操作员已确认的 1440px 桌面等比例复刻。采集使用 `isMobile + hasTouch`，并检查实际画布宽度；不再用窄桌面窗口冒充手机。桌面参考与 3.5% 阈值不变。旧标题/状态条已被实际总览决策区和风险区的等待条件替换。

仅在操作员审核新方案后，可在本地生成手机参考：

```bash
npm run visual:diff -- --page=delivery-mobile --update-mobile-baseline
```

正常 CI 只比较，不更新参考；`--ci` 与更新参数同时使用会报错。更换参考不证明业务数据或预测效果正确。
