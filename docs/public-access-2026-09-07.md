# 2026-09-07 全功能公开访问决定

用户明确选择取消密码，并开放全部既有功能，包括 AI 提问、生成与抓取。公网访客因此能够读取业务数据，并通过页面执行原有写入操作。该决定替代此前“个人工作台必须密码登录”的发布要求；没有新增多用户、账号管理或注册功能。

实现采用显式 `PUBLIC_AUTH_MODE=public`。公开模式不读取密码校验器、不创建登录会话，`/login` 跳转首页，`/auth/session` 返回 `{mode: "public", authenticated: false}`，页面不显示退出按钮。既有 `single_user_password` 模式保留，缺失和未知配置仍拒绝启动。

所有业务 API 沿用同一个公网代理。POST/PUT/PATCH/DELETE 需要精确同源 Origin 和 `Sec-Fetch-Site: same-origin`；这仅防止其他网页借浏览器触发操作，不是用户身份验证，也不限制直接访问本站的访客。内部 token 只由服务器添加，浏览器传入的 Cookie、Authorization 和 X-Internal-Token 不透传。内部服务仍仅监听 loopback。工业反馈的 Idempotency-Key 必须原样透传。

复用评估：GitHub [expressjs/cors](https://github.com/expressjs/cors) 提供浏览器响应读取策略，不能替代请求端的同源校验。本次复用现有代理和原生 HTTP，不增加依赖、外部服务或新的身份体系。

回滚同时恢复 `PUBLIC_AUTH_MODE=single_user_password` 和上一前后端发布对。通过独立非敏感 `shared/public-access-mode` 文件切换；前端和探针 wrapper 读取该覆盖值。现有 `.env.production` 与校验器留在服务器，不读取、不输出、不改写、不复制到交付证据；没有数据库迁移。

验收矩阵：匿名页面/深链接/API 200；旧登录地址跳转；同源匿名 AI/生成/抓取请求在隔离后端可达；跨站和缺少同源头的写入 403 且未到达后端；服务 token 不返回浏览器；反馈幂等头不丢失；密码模式原有保护回归通过；健康探针和烟测按显式公开模式验收。公网仍以 Chrome Computer Use 实际页面为主证据，不用隔离写入测试冒充生产生成结果。

匿名版本信息仅返回 release_id、release_hash、created_at、git_sha 和 modules；不返回本机回滚路径或分支诊断。常规来源运行状态不返回 database.path，保留运行和就绪业务字段。生产服务器上的完整发布清单继续用于内部回滚。

冷启动补充：模型信号客户端原有5秒独立等待预算会比接口成功返回更早结束，导致首次页面保留无方向状态。预算改为20秒，仍受底层请求上限保护；隔离测试注入6秒真实异步延迟，要求首次页面自动更新当前观察，无需用户刷新。
