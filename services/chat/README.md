# Chat Workspace v2

Chat Workspace v2 是独立于 `literature_workspace_v2` 的多用户 Chat 控制面。当前实现以
OpenAI Responses API 为真实 Provider，同时保留 Fake Provider 用于确定性测试。

第一版 Web 前端位于 `frontend/`，采用独立的严肃蓝视觉主题。它只覆盖 Chat 边界：
Session、消息树分支、编辑/重新生成、中断保留局部回复、SSE 流和工具执行轨迹；不包含
Document Database、Document Pipeline、Library 管理或 Codex 界面。

## 当前边界

- PostgreSQL 持久化 Session、Branch、ConversationUnit、TurnRun、ModelStep、
  ModelOutputItem 与 TurnEvent。
- 上下文由聊天树路径和完整 Provider Output Items 重建，不依赖
  `previous_response_id`。
- Chat API 到浏览器使用 SSE；每个事件以本地 `sequence_no` 为 ID，支持
  `Last-Event-ID` 和 `after` 断线重放。
- Worker 到 Provider 的流不承诺续传。上游断流会保留已持久化事件并将 Turn 标记为
  `FAILED/upstream_stream_failed`。
- 从任意已结算节点发送会创建分支；用户消息编辑采用追加新节点；regenerate 会创建新
  Branch 和 Turn，历史节点不原地修改。
- 用户中断会保留已显示的局部模型回复，结算为 `interrupted=true` 的
  `MODEL_RESPONSE`。
- 一个 Session 最多一个非终态 Turn；一个 Principal 最多同时运行三个 Turn。超过容量
  的 Turn 保持 `WAITING`，不会阻塞其他用户。
- 已实现应用侧 Function Calling 循环、ToolExecution 审计和每 Turn 工具预算。
- 当前工具为 `plan_board`、`document_retrieval`、
  `document_get_by_doi`。MCP 只保留工具来源抽象，不实现连接或执行。
- Codex 不在当前边界。

## API

所有路径前缀为 `/api/chat/v1`。开发环境使用
`X-Chat-Principal-Id: <uuid>`；生产环境仍需接入正式 OIDC。

```text
GET  /health/live
GET  /health/ready
GET  /admin/tool-config
PATCH /admin/tool-config
POST /sessions
GET  /sessions
GET  /sessions/{session_id}
GET  /sessions/{session_id}/graph
POST /sessions/{session_id}/turns
POST /units/{unit_id}/regenerate
POST /units/{unit_id}/edit-and-regenerate
GET  /turns/{turn_id}
POST /turns/{turn_id}/interrupt
GET  /turns/{turn_id}/events
GET  /turns/{turn_id}/events/stream
```

SSE 首次连接：

```http
GET /api/chat/v1/turns/{turn_id}/events/stream?after=0
Accept: text/event-stream
```

前端使用基于 `fetch` 的 SSE reader，以便同时发送开发身份头和 `Last-Event-ID`。
重连也会显式传 `after=<last_sequence>`；两者同时存在时服务端采用较大的游标。

## Provider 配置

默认配置使用 Fake Provider。真实 Responses：

```dotenv
CHATV2_PROVIDER=openai
CHATV2_MODEL=gpt-5.4
CHATV2_OPENAI_API_KEY=...
# CHATV2_OPENAI_BASE_URL=https://api.openai.com/v1
CHATV2_LITERATURE_API_BASE_URL=http://127.0.0.1:8020/api/v2
CHATV2_LITERATURE_SERVICE_TOKEN=...
```

也可以使用常规 `OPENAI_API_KEY`。Provider 会发送完整树路径作为 `input`，设置
`stream=true`，并将 Responses 流事件规范化后逐条写入 `turn_events`。

模型产生 `function_call` 后，Worker 会持久化完整 ModelStep/OutputItem，创建
`ToolExecution`，执行工具并将带原始 `call_id` 的 `function_call_output` 放入下一次
ModelStep。达到预算后，下一步请求不再提供工具，并要求模型基于已有结果给出最终回答。

Literature Tool 通过短期服务凭据向 Literature v2 传递当前 Principal；两个服务必须配置
相同的随机 `CHAT_LITERATURE_SERVICE_TOKEN`。本地 Compose 提供开发默认值，生产环境必须
覆盖。Retrieval Tool 只允许模型指定查询语句和 Document Database UUID；检索模式、Top K
和每篇文档的 chunk 数由服务端配置。DOI Tool 直接精确查询全局 CanonicalPaper，不进入
Library，也不接收 Library UUID。

管理员可通过 `GET/PATCH /admin/tool-config` 查看或修改这些服务端参数。更新使用
`expected_revision` 乐观并发控制，管理员身份以 Literature 的系统角色为准；环境变量仅是
数据库尚无配置记录时的默认值。

## 本地运行

```powershell
Set-Location 'C:\Users\Ludwig\Special Project\temp_for_agent\chat_workspace_v2'
Copy-Item .env.example .env
docker compose up -d --build
```

API 文档：`http://127.0.0.1:8030/api/chat/v1/docs`

Chat 前端：`http://127.0.0.1:5175`。开发版首次进入时输入一个 Principal UUID；该值仅
保存在浏览器 localStorage，并通过 `X-Chat-Principal-Id` 发送。生产环境接入 OIDC 后应
移除这一临时身份入口。

单独开发前端：

```powershell
Set-Location frontend
npm install
npm run dev
```

运行数据库集成测试前，先停止 Compose Worker，避免它领取测试 Turn：

```powershell
docker compose stop chat-worker
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m pytest -q -p no:cacheprovider
docker compose start chat-worker
```

静态检查：

```powershell
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m ruff check backend
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m mypy
```
