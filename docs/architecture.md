# 统一网站架构

## 产品边界

浏览器看到的是一个 Literature Workspace 网站。Chat 是网站中的功能模块，而不是第二个
产品。前端路由可以表现为 `/library/*`、`/documents/*` 与 `/chat/*`，但它们由同一个前端
应用提供。

## 服务边界

保留两个后端服务仍然有价值：

- Literature Service 管理用户资料、系统角色、Library、CanonicalPaper、Document、
  Pipeline 与检索。
- Chat Service 管理 Session、Branch、MessageUnit、TurnRun、ModelStep、OutputItem、
  ToolExecution、并发调度与 SSE。

服务拆分不应产生第二套登录、第二个页面入口或第二份用户真相来源。

## 目标请求路径

```text
Browser
  -> one site / one reverse proxy
       -> /api/v2/*       -> Literature Service
       -> /api/chat/v1/*  -> Chat Service
       -> /*              -> one frontend

Chat Worker
  -> internal Literature API for retrieval and DOI document lookup
```

所有容器最终进入同一个应用级 Compose 网络。浏览器不直接感知容器名或内部端口。

## 统一身份

目前 Literature Service 已拥有 OIDC/Keycloak 登录流程，Chat 的开发身份头只是临时测试
机制，不能成为网站架构。

实施时应先确定以下契约：

1. 浏览器只与统一站点建立登录会话。
2. 网关或两个 API 使用同一 OIDC issuer 验证用户身份。
3. 两个服务使用同一个稳定的 subject/principal ID；Chat 数据库只保存该外部 ID，不复制
   用户资料和角色。
4. Chat 调用 Literature 时使用服务凭据，并显式传递已验证的用户 subject，用于需要用户
   语境的操作。
5. CanonicalPaper 与 Document 的全局读取不错误套用 Library 权限；Library 权限只约束
   Library 投影与用户自定义内容。

优先建议让 Chat Service 自己验证同一 OIDC access token/session，而不是每次请求都向
Literature Service 查询“这个用户是谁”。这样 Literature 故障不会让全部 Chat 请求在身份
查询处串行阻塞，两个服务也不会形成不必要的同步耦合。

## 前端迁移

`frontend` 是唯一目标前端。`prototypes/chat-frontend` 中可复用的部分包括：

- 消息树与分支导航
- 编辑和重新生成
- 中断并保留局部回复
- fetch SSE reader 与 `Last-Event-ID` 游标
- Turn/ToolExecution 活动栏
- 严肃蓝 Chat 主题

迁移时应将这些实现拆成 `frontend/src/features/chat/`，复用现有应用壳、路由、认证状态和
API client。不得迁移开发 Principal UUID 登录页，也不得迁移第二个 nginx/站点入口。

## 推荐实施顺序

1. 明确统一身份 token/session 如何同时到达两个 API。
2. 在 Chat API 替换开发 Principal header，增加统一认证适配器。
3. 在唯一前端加入 `/chat` 路由和 Chat feature，并复用现有用户状态。
4. 建立根级 nginx 与 Compose，将两个 API 和唯一前端放入同一网络。
5. 将 Chat -> Literature 地址改为容器 DNS，例如 `http://literature-api:8020/api/v2`。
6. 完成跨服务、SSE、三并发和权限边界测试后，删除 Chat 前端原型。
