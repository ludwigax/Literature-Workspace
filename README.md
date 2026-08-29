# Literature Workspace

Literature Workspace 是一个网站项目，而不是 Literature 与 Chat 两个独立网站。

当前仓库由原 `literature_workspace_v2` 与 `chat_workspace_v2` 的有效代码整理而来：

```text
frontend/                    唯一的网站前端（当前以 Literature v2 为基础）
services/literature/         文献、馆藏、Document 与用户身份相关后端
services/chat/               Chat 会话、消息树、Turn、模型调用与工具执行后端
prototypes/chat-frontend/    待合并的 Chat 界面原型，不参与最终部署
docs/                        跨服务架构与迁移决策
```

## 当前状态

两个后端服务的领域拆分仍然保留，但产品边界调整为：

- 用户只访问一个前端和一个站点域名。
- Literature 与 Chat 共用一套登录会话和用户身份。
- Chat 不拥有第二套用户系统，也不要求用户输入 Principal UUID。
- Chat 调用 Literature 检索属于服务间调用，不等同于用户认证。
- `prototypes/chat-frontend` 只用于将已有 Chat 交互逐步移植进 `frontend`。

根级 Compose 暂未建立。这是有意为之：在统一认证契约和网关路由确定前，不继续固化
两个独立站点的部署结构。现有服务各自的 README 与 `.env.example` 保留在对应目录。

下一步见 [docs/architecture.md](docs/architecture.md)。
