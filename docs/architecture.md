# 统一网站架构

## 产品边界

浏览器看到一个 Literature Workspace 网站。Library、Papers、Documents 与 Chat 是同一
产品的功能模块，不是独立站点。

## 运行边界

后端采用一个模块化应用、多个运行进程：

```text
Browser
  -> frontend/nginx
       -> /api/v2/*       -> application API
       -> /api/chat/v1/*  -> application API
       -> /*              -> frontend SPA

application API ---------> PostgreSQL / MinIO
metadata-worker ---------> PostgreSQL / external metadata providers
chat-worker -------------> PostgreSQL / model provider / internal domain services
document-worker ---------> PostgreSQL / document pipeline
```

API 和 worker 使用同一份 `services/app` 代码与同一条 Alembic 迁移链，但使用不同数据库
角色。模型或工具执行不会占用 API 请求进程；Chat worker 仍可横向扩容，并通过数据库领取
Turn 和租约协调并发。

## 统一身份

OIDC 登录、Principal、WebSession Cookie 与 CSRF 全部由统一应用拥有。Chat 路由与
Literature 路由使用同一个认证依赖，不接受开发 Principal header、共享服务令牌或
“代用户”身份头。

用户级并发限制由 Chat 的 Turn 调度层执行，默认同一 Principal 最多运行 3 个 Turn；超过
限制的会话不创建新的活动 Turn，前端可以呈现忙线等待状态。

## 文献权限边界

- CanonicalPaper 与 PipelineDocument 是全局规范数据，不从属于某个 Library。
- Document Database 是全局检索语料配置；Chat 的 `document_retrieval` 直接查询它。
- `document_get_by_doi` 直接查询 CanonicalPaper 与 PipelineDocument。
- Library 是全局论文/文档的用户投影，另含用户自定义文件、覆盖和集合。
- Library membership 只约束 Library 投影的可见与可修改行为，不能被误用来限制全局文献
  查询。

## Chat 持久化边界

- `MessageUnit`：用户可见聊天树节点。
- `TurnRun`：从一次用户输入到把输入权交回用户的完整执行。
- `ModelStep` / `ModelOutputItem`：每次模型 API 调用及其原始输出项。
- `ToolExecution`：函数工具的一次执行、输入、结果和状态。

消息树引用执行结果，但模型输出项仍独立持久化；编辑、分支和重新生成追加新节点，不原地
篡改历史执行记录。SSE 断线续传只保证应用 API 到浏览器这一段；上游模型流中断按失败或
中断状态落库。

## 前端迁移约束

`frontend` 是唯一目标前端。`prototypes/chat-frontend` 仅作为消息树、SSE、工具活动栏和
严肃蓝主题的参考。迁移时复用现有应用壳、认证状态和 API client，不带入旧的 Document
Database/Pipeline 管理界面，也不建立第二个 nginx 或登录入口。
