# Figma 重建说明

没有 Figma 源文件时，不建议直接凭感觉继续改 UI。当前项目采用“参考图 -> 设计 token -> 视觉回归”的方式补齐设计源。

## 当前设计源

- 组件蓝图：`docs/frontend-component-blueprint.md`
- 参考图 prompt：`docs/frontend-imagegen-prompts.md`
- 黄金参考图：`public/frontend-reference/`
- 参考图 manifest：`design/frontend-reference/manifest.md`
- 页面清单：`design/visual-baselines.json`
- 设计 token：`design/design-tokens.json`
- 视觉回归脚本：`scripts/visual-regression.mjs`
- HTML 备份：`docs/frontend-reference-system.html`

## 重建 Figma 的操作顺序

1. 在 Figma 中创建 8 个 Frame。
2. 每个 Frame 使用 `design/visual-baselines.json` 中的 viewport 尺寸。
3. 把对应参考图导入 Frame 并锁定为底图。
4. 按 `design/design-tokens.json` 和 `docs/frontend-component-blueprint.md` 建立颜色、字体、间距、圆角和组件变量。
5. 从全局组件开始描摹：顶部状态栏、侧边栏、面板、表格、空状态、状态标签、按钮。
6. 每完成一页，在前端运行 `npm run visual:diff -- --page=<slug>` 对比，直到差异收敛。
7. Figma 完成后，后续以 Figma 变量和 `design/design-tokens.json` 双向同步为准。

## Frame 清单

| Frame | 页面 | 尺寸 | 参考图 |
| --- | --- | --- | --- |
| 01 | 总览看板 | 1672x941 | `public/frontend-reference/01-dashboard.png` |
| 02 | 原料链路 | 1672x941 | `public/frontend-reference/02-chain.png` |
| 03 | 事件新闻 | 1672x941 | `public/frontend-reference/03-news.png` |
| 04 | 行情观测 | 1672x941 | `public/frontend-reference/04-market.png` |
| 05 | 预测复盘 | 1672x941 | `public/frontend-reference/05-prediction.png` |
| 06 | 证据知识 | 1672x941 | `public/frontend-reference/06-evidence.png` |
| 07 | 数据来源 | 1672x941 | `public/frontend-reference/07-data-sources.png` |
| 08 | AI助手 | 1672x941 | `public/frontend-reference/08-assistant.png` |

## 组件重建优先级

1. App Shell：全宽顶部状态栏、左侧导航、背景层。
2. Dashboard Panels：通用面板、标题、工具按钮、状态角标。
3. Data Displays：表格、KPI、链路节点、证据列表、空状态。
4. Interaction States：导航 active、状态 tone、输入框焦点、禁用按钮。
5. Page Layouts：8 个页面的栅格比例和首屏密度。

## 验收口径

Figma 重建不是为了替代前端，而是给后续设计协作提供源文件。当前代码验收以 `npm run visual:diff`、构建结果和人工视觉评审共同决定。
