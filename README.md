# Literature Workspace

Literature Workspace 是一个网站项目：一个浏览器入口、一个模块化应用后端，以及按职责
独立运行的后台 worker。

```text
frontend/                    唯一的可部署前端（当前以 Literature v2 为基础）
services/app/                统一后端：身份、文献领域、Chat API 与 worker
prototypes/chat-frontend/    Chat 交互原型，仅供后续合并前端时参考
docs/                        架构与迁移说明
compose.yaml                 本地统一运行栈
```

## 当前后端边界

- Literature 与 Chat 共用 Principal、WebSession Cookie、CSRF 和同一数据库迁移链。
- 浏览器只访问一个 API；不存在 Chat 代用户调用 Literature 的身份转借协议。
- Chat HTTP 路由位于 `/api/chat/v1`，Literature 路由继续位于 `/api/v2`。
- `chat-worker` 是独立进程，但属于同一应用。它异步领取 Turn，避免模型与工具执行阻塞
  API 请求；每个用户默认最多同时执行 3 个 Turn。
- 全局 CanonicalPaper/Document 与用户 Library 投影保持不同权限边界。Chat 的文献检索
  直接访问 Document Database，DOI 精确查询直接访问全局 CanonicalPaper/Document，均不
  借道 Library。

## 本地启动

```powershell
docker compose up --build
```

网站入口为 `http://127.0.0.1:5174`，API 为 `http://127.0.0.1:8020`。文档处理测试服务使用
可选的 `document` profile。

更详细的 Literature 领域说明见 `services/app/README.md`，架构决策见
`docs/architecture.md`。
