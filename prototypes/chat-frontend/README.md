# Chat frontend prototype

这里保留已完成的 Chat v2 前端交互原型，供迁移到根目录 `frontend` 时参考。

它不是第二个网站，不应进入最终 Compose，也不应单独部署。尤其不要迁移其中的开发
Principal UUID 身份入口。目标实现应位于 `frontend/src/features/chat/`，并复用主站的应用
壳、路由和登录状态。
