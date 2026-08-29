# Repository guidance

This repository is one website with one frontend and one modular application backend.

- `frontend/` is the only deployable browser application.
- `services/app/` is the unified modular backend. It owns identity, Literature domains, and Chat.
- Chat HTTP routes run in the application API; Chat turns are executed by the separate `chat-worker` process.
- `prototypes/chat-frontend/` is reference code only and must not become a second site.
- Never commit `.env`, API keys, tokens, generated build output, caches, or local data.
- Preserve the distinction between global CanonicalPaper/Document access and Library projection permissions.
- Browser requests use the shared WebSession and CSRF contract; do not add service-token impersonation headers.
