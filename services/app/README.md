# Literature Workspace v2：文献管理系统交接记录

状态：文献管理阶段收尾，框架已跑通并完成多轮人工验收  
文档日期：2026-08-28  
文档性质：本项目文献管理侧唯一权威记录

本文件以当前代码、迁移、API、前端和测试为准，取代此前的开发计划、里程碑验收稿、系统说明、文档审计和 ADR。后续实现变化应直接更新本文件，不再建立平行的“当前状态”文档。

## 1. 功能边界与交付判断

当前交付的是一套多用户文献管理系统，以及把规范论文转化为 AI 可检索 Evidence 的全局 Document 子系统。

边界内包括：

- 账号登录、注册、邮箱验证、找回密码和服务端会话；
- 个人/Group Library、多用户成员与权限隔离；
- Canonical Paper、Library Item、元数据、Collection、Tag 和 Trash；
- 手工、引用文件、PDF 批量和 Zotero 文件夹导入；
- DOI/arXiv 识别、Canonical 合并和外部元数据解析；
- Blob、Canonical Artifact、Item Artifact Override 和用户 Asset；
- 管理员维护的 Pipeline、Document Database、Document、Chunk 和 Release；
- PDF-to-text 适配、BM25、Embedding/FAISS、单库与跨库 Evidence 检索；
- Zotero 风格 Library UI、Document 管理 UI 和 Retrieval UI。

边界外包括：

- Chat/Work Session、消息树、分支、重新生成和中断协议；
- Codex Thread、进程、恢复、工具事件和工作区文件调度；
- Chat 输入附件如何固化为消息快照；
- Chat/Codex 如何选择、调用和展示 Evidence 的产品逻辑。

后续 Chat/Codex 系统是本系统的消费者，不应重新定义 Paper、Artifact、Document、Chunk、Release 或 Retrieval 的真值。

交付判断：当前版本适合内部试用、后续 Chat/Codex 对接和受控演示；功能框架完整，但尚未达到无需运维补强即可公开部署的生产状态。

## 2. 系统拓扑

```text
Browser -> React frontend (:5174) -> FastAPI API (:8020)
                                      |-> PostgreSQL
                                      |-> MinIO / S3
                                      `-> Keycloak / OIDC (:8081)

PostgreSQL jobs
  |-> metadata-worker（引用/PDF/Zotero 导入与元数据刷新）
  `-> document-worker
       |-> PDF-to-text HTTP service
       |-> 可选 Pipeline LLM HTTP service
       |-> OpenAI-compatible Embeddings HTTP service
       `-> 本地 BM25/Manifest 与 FAISS 构建
```

API 与 Worker 共用 Python 模块，但以不同进程职能运行。长任务不占用 FastAPI 请求生命周期。Library 导入任务和 Document Build 任务使用各自的持久化任务模型，共享 PostgreSQL 作为协调真值，而不依赖进程内队列。

| 组件 | 当前职责 |
| --- | --- |
| React/Vite | Library、Document Admin、Evidence Retrieval 三个路由 |
| FastAPI | HTTP 契约、鉴权、事务、任务提交、内容访问 |
| PostgreSQL | 关系真值、RLS、审计、任务租约、Release/Manifest |
| metadata-worker | Citation/PDF/Zotero 入库、Identifier 和 Metadata Refresh |
| document-worker | 固定阶段的 Document 构建 DAG、索引和发布 |
| MinIO/S3 | 按 SHA-256 管理的不可变 Blob |
| Keycloak | OIDC、注册、邮箱验证、密码重置；应用不存密码 |
| pdf-text-test | 基于 `pdfminer.six` 的串行测试服务，不是生产解析器 |

## 3. 身份、账号与授权

### 已实现

- OIDC Authorization Code + PKCE；浏览器只持有 HttpOnly 会话 Cookie。
- `(issuer, subject)` 幂等映射到 Principal；首次登录自动创建 Personal Library 和 OWNER Membership。
- Keycloak Realm 模板允许注册、邮箱作为用户名、邮箱验证和忘记密码。
- 登录、回调和退出支持受信 Browser Origin 映射；localhost、127.0.0.1 与服务器入口不会互相串跳，未配置 Host 被拒绝。
- 修改请求校验 CSRF；敏感操作记录 Audit Event。
- Library 角色为 OWNER、EDITOR、VIEWER；全局系统角色为 ADMIN、USER，两者相互独立。
- OWNER 可邀请成员、重新生成邀请链接、修改角色和移除成员；邀请绑定已验证邮箱。
- 应用层权限为主判定，受限 PostgreSQL 运行账号和 RLS 为 Library 隔离提供第二层保护。

| 能力 | VIEWER | EDITOR | OWNER | 系统 ADMIN |
| --- | ---: | ---: | ---: | ---: |
| 读取所属 Library | 是 | 是 | 是 | 仍需 Membership |
| 修改条目/Collection/Tag/资源 | 否 | 是 | 是 | 仍需 Membership |
| 管理 Group 成员与邀请 | 否 | 否 | 是 | 不自动取得 Group 所有权 |
| 查看已发布 Document/Chunk 与检索 | 是 | 是 | 是 | 是 |
| 配置 Pipeline/Database、执行 Build | 否 | 否 | 否 | 是 |
| 核验 Canonical PDF、分配系统角色 | 否 | 否 | 否 | 是 |

SMTP 凭据属于部署秘密，不写入仓库。测试环境已完成 Gmail SMTP 连通和真实发信验证；换环境后必须重新注入 SMTP、域名和 TLS 配置。

## 4. 文献与 Library 数据口径

```text
CanonicalPaper
  |-- CanonicalIdentifier（DOI / ARXIV / PMID / ISBN / OTHER）
  |-- CanonicalMetadata（唯一当前值）
  |-- Artifact（每个 artifact_key 唯一当前 Canonical 值）
  `-- LibraryItem（每个 Library 最多一个未 PURGED 条目）
       |-- sparse local_overrides
       |-- CollectionItem / ItemTag
       |-- ItemArtifactOverride
       `-- Asset
```

- Canonical Paper 是全系统共享的论文实体，不属于某个用户。
- Library Item 表示某个 Library 收录了该 Paper，并维护该 Library 的归类和局部覆盖。
- 同一 DOI/arXiv 在不同 Library 复用 Canonical Paper，但 Library Item、Collection、Tag 和资源选择独立。
- Canonical Metadata 只保存当前真值，不提供产品级版本历史。成功刷新会原子替换整份来源记录，不混合多个 Provider 字段。
- Library Edit 只写稀疏 `local_overrides`，不改写 Canonical Metadata 或其他用户视图。
- Item Trash 可恢复，保留 Collection、Tag、Override、Asset 和 Blob 关系；当前没有公开 Permanent Purge。

已支持字段：标题、作者、摘要、作品类型、出版年月日及精度、期刊/会议、Canonical URL、出版社、卷、期、页码、文章号、语言、ISSN、ISBN、扩展字段和 Provenance。添加/修改日期与 Metadata Source 只读显示。缺失字段保持空值，不伪造日期。

## 5. Identifier 与 Metadata Resolution

- DOI URL、`doi:` 前缀和大小写先规范化。
- `10.48550/arXiv...` 同时建立规范化 DOI Alias 与 ARXIV Identifier，并去除仅表示版本的尾缀。
- 普通 DOI：Crossref 优先；无可用记录时使用 OpenAlex。
- arXiv Identifier/DataCite arXiv DOI：先解析 arXiv；若记录声明正式 DOI，再尝试 Crossref。Crossref 成功则采用完整 Crossref 记录，否则保留完整 arXiv 记录。
- Provider 数据不逐字段混合。当前来源为 `UNDEFINED`、`CROSSREF`、`OPENALEX`、`ARXIV`、`ZOTERO`。
- 网络失败按任务策略重试；失败不覆盖现有元数据，用户可显式再次 Refresh。
- Canonical 匹配复用已有 Paper；Library 仍保留自己的 Item 和 Collection 位置，不将正常复用显示为异常。
- 外部文本入库前删除 NUL、将 CRLF 统一为 LF、把非法 Unicode/孤立 surrogate 替换为 `�`，保留换行、Tab 和正常学术字符。

## 6. 文献添加与异步任务

### Manual

当前 UI 要求 Title、DOI、Year；其他字段可空。创建或匹配 Canonical Paper、创建 Library Item，并可指定 Collection/Tag。Metadata Refresh 异步运行。

### Citation Import

- 支持 BibTeX、RIS 和 CSL-JSON。
- 文件先存 Blob，再由 Worker 解析。
- 有限信息先建立可见 Item；存在 Identifier 时再排队 Refresh。
- 无 DOI/arXiv 的记录仍可作为 `UNDEFINED` 条目存在。

### PDF Import

- 支持批量 PDF；默认单文件上限 100 MiB。
- 接收后立即建立有限 Item 与后台 Job，前端可见进度。
- 当前识别器依次检查 PDF Metadata Dictionary、前两页文本，最后检查文件名，提取 DOI/arXiv。
- 成功后执行 Canonical 合并与 Metadata Refresh；失败仍保留 PDF 和有限条目。
- 导入 PDF 总是成为该 Item 的显式 Primary PDF。若 Canonical 尚无 PDF，同一 Blob 同时晋升为 `UNVERIFIED` Canonical PDF。

### Zotero Folder Import

- 浏览器选择同时包含 `zotero.sqlite` 与 `storage` 的 Zotero 目录。
- SQLite Snapshot 先异步建立/匹配 Item、Metadata、Collection、Identifier 和 Attachment Manifest。
- 前端随后按 Manifest 上传存在的 PDF；缺失文件只计数，不阻塞 Metadata Import，也不产生虚假 Blob Link。
- 主 PDF 为 Primary PDF；同条目的额外 PDF 为 Asset。
- Snapshot Fingerprint 和 Attachment 状态会复用未变化数据，重复同步不会无条件重传已成功 PDF；它仍不是持续监听式双向 Zotero Sync。
- 页面刷新不会中断已提交的后端 Job；仅尚未由浏览器上传的本地文件需要页面继续参与。

### Library Job

- 状态：PENDING、RUNNING、SUCCEEDED、FAILED、CANCELLED。
- 持久化 Payload、Result、Progress、Attempt、Lease、Idempotency Key 和 Error。
- Worker 使用租约和 `SKIP LOCKED` 领取，可多槽并发；默认并发 4。
- Outbox 与 Audit 表已建立，但当前不依赖外部消息总线。
- 前端统一 Activity 展示导入/Refresh；Zotero Metadata 完成后刷新条目列表。

## 7. Blob、Artifact、Override 与 Asset

```text
CanonicalPaper -> Artifact(current) -> Blob
       ^
       `-- LibraryItem -> ItemArtifactOverride(optional) -> Blob
                     `-> Asset(user file) ----------------> Blob
```

- Blob 字节不可变，按 SHA-256 全局去重；Blob 复用不等于授权共享。
- Artifact 是 Canonical Paper 某资源键的唯一当前值，无用户版本历史。
- ItemArtifactOverride 是 Library Item 的显式当前选择；未指定时自动继承 Canonical。
- 取消 Override 只删除选择关系，不立即删除 Blob。
- Asset 是用户普通附件，不参与 Canonical 默认/覆盖；删除只将 Asset 置为 DELETED。
- 读取从有权限的 Library Item Resource 路径解析，不能凭 Blob ID 绕过权限。

| 类型 | 当前行为 |
| --- | --- |
| SOURCE_PDF | Canonical 或 Item Override；可在 Reader 打开 |
| EXTRACTED_TEXT | Canonical PDF 转录文本；存在时显示文本资源 |
| SUPPLEMENT | Canonical 补充材料入口，当前管理能力有限 |
| PIPELINE_DOCUMENT | 已发布 Document Database 投影，不是独立真值 |
| Asset | 用户文件，统一 File 图标并平铺 |

Library 资源树中 PDF、Text、Asset 直接显示；多个 Pipeline Document 放在 `Documents` 二级折叠项。只有实际存在且可解析的资源才显示。Document 读取走 `paper -> current release projection -> document_id`，列表投影只承担轻量标题/计数展示。

## 8. Library Search 与前端

`/` 是 Zotero 风格 Library 主页面，已实现：

- Personal/Group 切换、Group 创建、成员和邀请管理；
- Collection Tree、新建子级/改名/删除，Paper 可属于多个 Collection；后端支持 Re-parent，当前 UI 尚未提供拖拽/移动操作；
- Tag CRUD、筛选与批量归类；
- Active/Trash、批量 Trash/Restore、批量 Collection/Tag 操作；
- Cursor Pagination、滚动到底自动加载、选择全部匹配结果；
- Manual、Citation、PDF、Zotero Folder 四个添加入口；
- 条目详情与完整 Metadata Edit；
- Primary PDF Override、取消 Override、Asset 上传/改名/删除；
- PDF/Text/Document/Asset Reader；
- Background Activity 与统一操作反馈。

Metadata Search 是 PostgreSQL 查询，不是 RAG。高级搜索支持：title、author、identifier、venue、year range、work type、metadata source、Collection（可含子级）、Tag ANY/ALL、has PDF/Document/Asset、added/modified 日期，以及 ADDED/MODIFIED/TITLE/AUTHOR/YEAR 排序。

## 9. Document Pipeline、Database 与 Release

该模块是全局研究语料服务，不按用户或 Library 复制。一个 Document Database 只属于一个 Pipeline，其范围可覆盖用户尚未收录的 Canonical Paper。

```text
DocumentPipeline
  `-- DocumentPipelineVersion（不可变 recipe snapshot）

DocumentDatabase
  |-- range: EXPLICIT | ALL_VERIFIED
  |-- current_release_id（稳定、可检索）
  `-- building_release_id（构建中、不可检索）

DocumentDatabaseRelease
  `-- DocumentReleaseEntry（release -> paper -> document 快照映射）
       `-- PipelineDocument（不可变文本）
            `-- DocumentChunk（不可变文本、ordinal、facet_1/facet_2）
```

### 术语澄清：Release、CURRENT 与 BUILDING

`DocumentDatabaseRelease` 表示 Document Database 在某一次构建中形成的**完整候选快照**。`Release` 不是与 `Build` 相对的另一类版本：Build 是产生快照的执行过程，Release 是该过程创建并最终留下的数据快照。

- `building_release_id` 指向正在由 Build 填充和校验的候选 Release；此时其状态是 `BUILDING`，不能被检索。
- `current_release_id` 指向已经通过完整性与索引校验、正式发布的当前 Release；其状态是 `CURRENT`，对读取和检索稳定可见。
- 发布是一次原子指针切换：新的 BUILDING Release 变为 CURRENT，原 CURRENT 变为 ARCHIVED。
- Build 失败时，候选 Release 变为 FAILED 或被清理；`current_release_id` 保持不变，因此线上读取不受半成品影响。
- `DocumentDatabaseRelease` 表中可同时存在历史 ARCHIVED、当前 CURRENT 和至多一个候选 BUILDING；`DocumentDatabase` 上的两个 ID 只是快速指向当前快照与候选快照。

因此，更准确的关系是：

```text
Build（执行过程） -> 创建/填充 DocumentDatabaseRelease（候选快照）
                          BUILDING --校验并发布--> CURRENT --被替换--> ARCHIVED
```

### Pipeline 与 Splitter

- `DIRECT_TEXT`：Canonical EXTRACTED_TEXT 直接成为 Document，不调用 LLM。
- `LLM`：以 System Prompt、User Prompt、Source 和可选 User Note 调用部署指定 HTTP Executor。
- Recipe Config Hash 相同则复用 Version；实际激活不同配置才产生新 Version。改名/描述/归档不产生 Recipe Version。
- WHOLE：全文一个 Chunk。
- JSON：识别 fenced JSON 或全文 JSON；字典按一级 `key\nvalue`，列表按一级 Item 字符串化。
- PARAGRAPH：保留段落边界，按 `chunk_size_words` 聚合；超长段落按词数切开。
- MARKDOWN：只在指定的精确 Heading Level 切分。
- ADVANCED：仅允许后端受信函数，并可写 `facet_1`/`facet_2`。

Document 与 Chunk 都保存规范化文本。`chunk_id` 可定位 Document 和 Canonical Paper，Evidence 可按 Document 聚合多个 Chunk。

### Range、Build 与 Publish

- EXPLICIT：管理员提交 Canonical Paper ID 集合。
- ALL_VERIFIED：动态包含拥有 VERIFIED Canonical PDF 的全部 Paper。
- Scope/Range Mode 真变化才增加 `range_revision`；相同提交不增加。
- FULL 强制重建范围内全部 Document；UPDATE 复用输入、Pipeline Version 和 Splitter Config 均未变化的 Document，只生成新增/变化项并省略移除项。
- 有变化时创建 BUILDING Release；完整性和索引校验通过后原子切换为 CURRENT，旧 CURRENT 变为 ARCHIVED。
- 无变化 UPDATE 返回 `NO_CHANGE`，不创建无意义快照。
- Build 期间 Retrieval 始终读取旧 CURRENT，绝不触碰 BUILDING。
- Auto Reconcile 可按 Database 开关；当前固定每日检查，也可管理员手动 FULL/UPDATE。
- Build 期间再次发生 Scope/Pipeline 变化会合并为后续 UPDATE，避免并发写两个 Build。

### Document Worker DAG

```text
PDF_TO_TEXT（每批最多 4 篇，source queue 串行）
  -> BUILD_DOCUMENT（每篇独立，pipeline queue 可并发）
  -> BUILD_MANIFEST_BM25（release 级）
  -> BUILD_EMBEDDINGS（配置 embedding 时）
  -> VALIDATE_RELEASE
  -> PUBLISH_RELEASE
```

Task 持久化状态、Lease、Attempt、Progress 和 Error。BuildRun 可查、Cancel、Retry。API 只提交 Run；推进、索引和发布由 Document Worker 执行。

`VALIDATE_RELEASE` 是 BuildRun 的发布前检查阶段，不是与 BUILDING、CURRENT 并列的 Release 状态。它检查候选 Release 的 Entry 数量和状态是否完整、Manifest 行数是否与索引一致、BM25 是否 READY，以及启用 Embedding 时 FAISS Blob 与维度是否齐备。检查失败会让 Build 失败，并阻止创建 `PUBLISH_RELEASE` 后续任务，因此 CURRENT 不受影响。

`PUBLISH_RELEASE` 在真正同步 Library Artifact Projection 和切换 `current_release_id` 前，会再次检查 Entry 与索引的关键条件。这部分重复是最终事务防线：即使 Validation 通过后数据被意外改变，Publish 也不能放行半成品。当前 Validation 本身不修改 Release 的可见性；只有 Publish 才执行 `BUILDING -> CURRENT` 和旧 `CURRENT -> ARCHIVED`。将 Validation 保留为独立 Task 还能提供明确的失败阶段、重试边界，并为以后增加内容质量、Schema 或抽样检查留出位置。

CURRENT 发布后同步 `PIPELINE_DOCUMENT` Artifact Projection：新增则创建、变化则更新 Revision、复用项不变、移出 Range 的 Projection 被移除。Artifact 只提供 Library 列表/资源入口；Document Database Release 仍是 Document 真值。

## 10. BM25、Embedding/FAISS 与 Evidence Retrieval

每个 Release 拥有不可变索引，最多同时保留 CURRENT 和 BUILDING 两组可用索引；Archived Index Blob 可清理，Document/Chunk/Release Entry 可保留追溯。

- Manifest 为 `row_number <-> chunk_id` 映射，并缓存 Content Hash、Document/Paper ID 和 Facet。
- Facet 预构建 Bitmap，先过滤行再进行 BM25/Vector 查询。
- BM25 在 Worker 内按 Release 全库构建，缓存每个 Chunk 的稀疏词频和全库统计。
- Embedding 使用 OpenAI-compatible `/embeddings`，按响应 `index` 恢复顺序。
- 默认 BGE-M3、1024 维、Batch 32、估算 Token Budget 10000；可由 Database Profile 固化。
- 413/5xx 多输入请求按序二分；网络失败最多重试两次；校验数量、维度和 Finite 值。
- FAISS 使用 L2 归一化的 `IndexFlatIP`（Cosine），作为不可变 Blob 存储。
- UPDATE 按 Chunk ID + Content Hash 复用未变化向量，只请求缺失/变化项；新 Manifest 一次生成，不原地修改 CURRENT。

Retrieval 支持：

- 单库 BM25、VECTOR、HYBRID；Hybrid 以 Rank Fusion 合并 Chunk 排名。
- `facet_1`、`facet_2` 先过滤。
- Evidence 是 Document 聚合结果，不持久化为新实体。
- Chunk Score 可含 BM25、Embedding、Ranking Score。
- Document 聚合支持 MAX 或按排名递减权重求和的 INTEGRATE。
- 返回 Paper、Document 摘要和有限 `chunk_list + score_list`；全文按 `document_id` 单独读取。
- 跨库请求显式指定 Database ID、各库 Top K/Weight 和总 Top K，只查 CURRENT。
- 跨库使用加权 RRF，降低不同数据库分值尺度不可比的问题。
- 任一库失败时仍返回成功库的单库结果，但不返回误导性的全局融合，状态为 PARTIAL。

`/retrieval` 是独立 Retrieval Panel；`/documents/admin` 是独立 ADMIN Panel。二者没有与 Library 写成难拆分的复合页面，便于下一阶段嵌入 Chat 主页面侧栏、抽屉或主面板。

## 11. HTTP 能力分组

所有路径前缀为 `/api/v2`。

| 分组 | 主要能力 |
| --- | --- |
| `/health/*` | Liveness、Readiness |
| `/auth/*` | Login、Callback、Session、Logout |
| `/libraries` | 列表、Group、成员、邀请、链接再生成/接受 |
| `/libraries/{id}/collections` | Collection CRUD 与 Item Placement |
| `/libraries/{id}/tags` | Tag CRUD |
| `/libraries/{id}/items` | Search、Create/Edit、Override、Bulk、Trash/Restore |
| `/libraries/{id}/imports/*` | Citation、PDF、Zotero、Attachment、Job |
| `/libraries/{id}/items/*/resources` | Primary PDF、Asset、Artifact/Document Content |
| `/document-pipelines*` | USER 读取；ADMIN 创建、编辑、激活 Version |
| `/document-databases*` | USER 读取；ADMIN 配置 Scope/Policy、FULL/UPDATE |
| `/document-build-runs*` | Run/Task 查看；ADMIN Cancel/Retry |
| `/documents/{id}`、`/chunks/{id}` | 已发布内容读取 |
| `/document-databases/{id}/search` | 单库 Chunk Search |
| `/retrieval/search` | 多库 Document Evidence Search |
| `/admin/*` | Principal/System Role、Canonical PDF Verification |

运行中的 OpenAPI `/api/v2/docs` 是字段级接口真值；本文件定义稳定语义，不复制全部 Schema。

## 12. 已完成验证

仓库当前有 61 个后端测试，覆盖：

- OIDC Origin、首次 Provision、CSRF、RLS 隔离与邀请；
- Canonical/Library Item、Collection、Tag、Trash、Override 和 Resource Access；
- BibTeX/RIS/CSL-JSON、PDF Identifier、Zotero Snapshot/Attachment；
- Crossref/OpenAlex/arXiv、重试、成功才覆盖与 Canonical Merge；
- Splitter、文本清洗、FULL/UPDATE、ALL_VERIFIED；
- Worker DAG、Run/Task、Release Publish/Archive 与 Artifact Projection；
- Manifest/BM25/Facet Bitmap、FAISS、Embedding Batch/Retry/Bisection；
- ADMIN/USER 权限、单库检索、跨库 Evidence/RRF 与失败隔离。

验收脚本：

- `scripts/run_fake_pipeline_acceptance.py`：带延迟的 Fake 全链路。
- `scripts/run_real_pipeline_acceptance.py`：五篇 PDF，先 EXPLICIT 四篇，再切 ALL_VERIFIED 增加第五篇，验证 UPDATE、复用、BM25/FAISS、Projection 和 Hybrid Retrieval。

人工已验证账号/Group/邀请、Citation/PDF/Zotero 导入、300+ 条目、资源树与 Reader、Metadata Search、Document Admin、Retrieval，以及五篇真实 PDF 的两次 Release 流程。

## 13. 运行与验收

### 启动

```powershell
Set-Location 'C:\Users\Ludwig\Special Project\temp_for_agent\literature_workspace_v2'
docker compose up -d --build
docker compose --profile document up -d --build document-worker
```

- Library：`http://127.0.0.1:5174/`
- Retrieval：`http://127.0.0.1:5174/retrieval`
- Document Admin：`http://127.0.0.1:5174/documents/admin`
- API Docs：`http://127.0.0.1:8020/api/v2/docs`
- Keycloak：`http://127.0.0.1:8081`
- MinIO Console：`http://127.0.0.1:9003`

Document Worker 依赖 `LITV2_DOCUMENT_PDF_TEXT_URL`、Embedding 配置，并在 LLM Pipeline 启用时依赖 `LITV2_DOCUMENT_PIPELINE_URL`。Compose 的 `pdf-text-test` 是功能测试替身。

### 本地 Python 与检查

不要激活或注入 Conda，使用绝对解释器：

```powershell
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m pip install -r requirements-dev.lock
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m pip install --no-deps -e .
Copy-Item .env.example .env
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m alembic upgrade head

docker compose stop metadata-worker document-worker
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m ruff check backend/app backend/tests
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m mypy backend/app
& 'C:\Users\Ludwig\anaconda3\envs\research\python.exe' -m pytest -q -p no:cacheprovider
Set-Location frontend
npm ci
npm run typecheck
npm run build
```

迁移使用高权限 `LITV2_MIGRATION_DATABASE_URL`；API 使用受限 `literature_app`；Worker 使用自己的账号。部署时不要合并为同一高权限账号。

## 14. 后续优化清单

以下不阻塞本阶段收尾。

### A. 从内测走向可靠部署

- 用准确、可观测的生产 PDF Layout/OCR 服务替换测试服务；处理扫描件、双栏、公式、表格、图片和版面顺序。
- 为 PDF-to-text、Embedding 和 LLM 服务增加 TLS、鉴权、配额、超时预算和容量规划；不暴露无鉴权公网端口。
- 正式域名/HTTPS、生产 Secret 管理、SMTP、Keycloak/PostgreSQL/MinIO 备份恢复和凭据轮换。
- Structured Error、Correlation ID、Metrics、Tracing、Alert、失败任务运维界面和 Runbook。
- Rate Limit、恶意文件检测、更强 Content-Type 校验和安全审计。
- Browser E2E、并发/大库负载、故障注入、Accessibility 和多浏览器测试。

### B. 文献管理体验

- Library Archive/Delete；Permanent Purge、Retention 和 Reference-aware Blob GC。
- Canonical PDF 核验/替换管理前端；后端已有 Verification 接口，操作面板未完整产品化。
- Notes、PDF Annotation、高亮和引用导出。
- Zotero 增量同步、断点续传、差异预览和可选 Desktop Helper；当前是 Snapshot Import。
- Collection Drag/Re-parent、键盘操作、表格列配置和更顺手的高密度 UI。
- 更完整的 Group/账号管理 UI、邀请邮件模板和管理员治理。
- Canonical Merge/Split、重复项审计和误匹配修复工具。
- Supplement/SI 作为 Canonical Artifact 的管理员摄取和展示流程。

### C. Document 与 Retrieval 质量

- 生产 Pipeline LLM 协议、Prompt Preview、Cost、Rate Limit 和调用审计。
- Document 命名模板、Recipe 可用性和更完整的 Admin UI。
- Advanced Splitter、Facet Schema 和 Build 前抽样预览。
- BM25 多语言分词、停用词、词形归一、参数和离线评测。
- Embedding Profile 迁移、索引重建、吞吐和质量基准。
- Hybrid/RRF、MAX/INTEGRATE、跨库 Weight/Top K 的可解释性与评测集调参。
- Release/Index 保留周期、Failed Build/Orphan Blob 清理和大规模重建性能。
- Auto Reconcile 时间配置、补偿、范围变化审计和管理员通知。
- Document/Chunk Search 与 Metadata Search 的入口是否合并，待真实使用后决定。

### 下一阶段接口约束（不在本文设计 Chat）

- Chat/Work 只使用 CURRENT Release，请求显式传 Document Database ID。
- Chat Tool 可复用 `/retrieval/search`，再按 `document_id` 取全文；消息引用快照由 Chat 系统决定。
- DOI 精确取文档使用全局 `/canonical-papers/by-doi`，从 CanonicalPaper 查询其
  PipelineDocument，不经过 Library，也不要求论文已存在于用户馆藏。
- Chat Worker 可使用 `X-Literature-Service-Token` 与 `X-Act-As-Principal-Id`
  进行服务调用；只有配置了 `LITV2_CHAT_SERVICE_TOKEN` 才启用。Library 接口仍以目标
  Principal 执行 membership 校验；全局 CanonicalPaper、Document 与 Retrieval 接口不受
  Library membership 约束。该凭据不是管理员入口，生产环境应由正式的服务身份或
  token exchange 替换本地共享密钥。
- 是否限制为用户 Library Item 必须显式选择，不能默认把全局语料缩成用户收藏。
- 历史引用对应资源失效时应降级显示缺失，不让历史消息崩溃。

## 15. 已知限制与非承诺

- 这是功能全面的初版框架，不代表公共 SaaS 安全、可靠性和运维已完成。
- PDF 内容质量受测试解析器限制；Metadata Identifier 提取与全文 PDF-to-text 是两个不同链路。
- 注册/验证/重置依赖 Keycloak 和部署 SMTP；仓库不保存 SMTP Secret。
- Group Library 有权限隔离，但没有完整 Operator Tenant Console。
- Artifact/Asset 删除主要是逻辑删除/解除关系；物理 Blob 回收未完成。
- Document Database 是全局语料，不按用户复制；USER 可检索已发布内容，ADMIN 才可配置执行。
- Evidence 是实时聚合结果，不持久化为新真值。
- 当前没有 Chat/Codex 代码依赖 v2；下一阶段通过 HTTP API 对接。

## 16. 文档治理与旧系统标记

本次收尾把 v2 历史计划、阶段验收、重复系统说明、审计稿和 ADR 中仍有效的信息合并到本文件，并移除原文件，避免“一个说未实现、另一个说已实现”的漂移。

审计发现的主要过时点：

- 旧 `LIBRARY_SYSTEM.md` 将 Pipeline、Document Database、Chunk、BM25/FAISS 和 Retrieval 全部列为边界外或未实现，已与代码不符；
- 旧 `DOCUMENT_PIPELINE_SYSTEM.md` 声称 Admin UI 不在范围内，但 `/documents/admin` 已经实现；
- 旧 `DEVELOPMENT_PLAN.md` 仍把 Server-side Metadata Search、ALL_VERIFIED、Durable Build、Admin API/UI、BM25、Embedding/FAISS 和 Retrieval 列为未来工作；
- `M1/M2/M3` Acceptance 只证明当时阶段，不包含其后已经完成的导入、资源、Document 和 Retrieval 能力；
- `DOCUMENTATION_AUDIT.md` 自身是中途审计快照，其中保留了后来已失效的“未实现”判断；
- ADR 0001—0010 的有效原则已经进入代码和本文件，但修订链分散，继续单独保留会让读者误把早期限定当成现状。

因此移除了 v2 的 `DEVELOPMENT_PLAN.md`、`docs/README.md`、两份旧系统说明、三份阶段验收、一份旧审计和 `docs/adr/0001`—`0010`。v2 目录现在只有本文件这一份 Markdown 文献管理记录；运行接口以 OpenAPI、数据库结构以 Migration、可执行行为以代码和测试为最终证据。

旧目录 `literature_workspace/` 属于早期 Chat-first Demo，其中的 Chat/Codex/Document 文档尚未按本 v2 口径重构。本轮只添加 Legacy 标记，不删除、不继续设计。下一阶段 Chat/Codex 交接时，应先以本文件确定的 Library、Artifact、Document、Release 和 Retrieval 契约为上游事实，再审计和重写旧 Chat 文档。
