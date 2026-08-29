# Repository guidance

This repository is one website with one frontend and two internal backend services.

- `frontend/` is the only deployable browser application.
- `services/literature/` owns literature-domain data and the existing OIDC integration.
- `services/chat/` owns chat execution state, concurrency, SSE, and tool calling.
- `prototypes/chat-frontend/` is reference code only and must not become a second site.
- Never commit `.env`, API keys, tokens, generated build output, caches, or local data.
- Preserve the distinction between global CanonicalPaper/Document access and Library projection permissions.
- Do not make Chat synchronously query Literature for user identity on every request; both services should validate the same identity authority.
