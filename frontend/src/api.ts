export type Principal = {
  principal_id: string;
  display_name: string;
  system_role: "ADMIN" | "USER";
};

export type SessionInfo = {
  authenticated: true;
  principal: Principal;
};

export type Library = {
  library_id: string;
  library_type: "PERSONAL" | "GROUP";
  name: string;
  status: string;
  role: "OWNER" | "EDITOR" | "VIEWER";
  revision: number;
  updated_at: string;
};

export type Member = {
  principal_id: string;
  display_name: string;
  role: "OWNER" | "EDITOR" | "VIEWER";
  status: string;
};

export type Invitation = {
  invitation_id: string;
  library_id: string;
  email: string;
  role: "EDITOR" | "VIEWER";
  status: string;
  expires_at: string;
  accept_token?: string;
};

export type Collection = {
  collection_id: string;
  library_id: string;
  parent_collection_id: string | null;
  name: string;
  status: string;
  revision: number;
  item_count: number;
};

export type LibraryTag = {
  tag_id: string;
  library_id: string;
  name: string;
  color: string | null;
  status: string;
  revision: number;
  item_count: number;
};

export type PaperMetadata = {
  title: string;
  abstract?: string | null;
  publication_year?: number | null;
  publication_month?: number | null;
  publication_day?: number | null;
  publication_date?: string | null;
  publication_date_precision?: "YEAR" | "MONTH" | "DAY" | null;
  work_type?: string | null;
  venue?: string | null;
  canonical_url?: string | null;
  publisher?: string | null;
  volume?: string | null;
  issue?: string | null;
  pages?: string | null;
  article_number?: string | null;
  language?: string | null;
  issn?: string[];
  isbn?: string[];
  authors?: Array<{ name?: string }>;
};

export type CatalogueItem = {
  library_item_id: string;
  library_id: string;
  canonical_paper_id: string;
  status: "ACTIVE" | "TRASHED";
  revision: number;
  metadata_source: "UNDEFINED" | "CROSSREF" | "OPENALEX" | "ARXIV" | "ZOTERO";
  metadata_revision: number;
  canonical_metadata: Record<string, unknown>;
  local_overrides: Record<string, unknown>;
  effective_metadata: PaperMetadata;
  identifiers: Array<{ scheme: string; value: string }>;
  collection_ids: string[];
  tag_ids: string[];
  pdf_attachment: {
    origin: "CANONICAL" | "OVERRIDE";
    artifact_type: string;
    filename: string | null;
    media_type: string;
    revision: number;
  } | null;
  asset_attachments: Array<{
    asset_id: string;
    filename: string;
    media_type: string;
    revision: number;
  }>;
  resource_summary: {
    primary_pdf: number;
    extracted_text: number;
    documents: number;
    assets: number;
  };
  created_at: string;
  updated_at: string;
};

export type ZoteroAttachment = {
  source_id: string;
  zotero_library_id: number;
  item_key: string;
  attachment_key: string;
  library_item_id: string;
  filename: string;
  relative_path: string;
  role: "PRIMARY_PDF" | "ASSET";
  imported: boolean;
};

export type ArtifactResource = {
  resource_kind: "ARTIFACT";
  artifact_key: string;
  artifact_type: "SOURCE_PDF" | "EXTRACTED_TEXT" | "SUPPLEMENT" | "PIPELINE_DOCUMENT";
  origin: "CANONICAL" | "OVERRIDE";
  filename: string | null;
  media_type: string;
  byte_size: number | null;
  revision: number;
  status: "ACTIVE" | "STALE" | "MISSING";
  document_id?: string;
  document_database_id?: string;
  pipeline_id?: string;
  pipeline_version_id?: string;
};

export type AssetResource = {
  resource_kind: "ASSET";
  asset_id: string;
  filename: string;
  media_type: string;
  byte_size: number | null;
  revision: number;
  status: "ACTIVE" | "MISSING";
  created_at: string;
  updated_at: string;
};

export type ItemResources = {
  library_item_id: string;
  canonical_paper_id: string;
  primary_pdf: ArtifactResource | null;
  documents: ArtifactResource[];
  canonical_attachments: ArtifactResource[];
  assets: AssetResource[];
};

export type AdvancedMetadataSearch = {
  title?: string;
  author?: string;
  identifier?: string;
  venue?: string;
  yearFrom?: number;
  yearTo?: number;
  workTypes?: string[];
  metadataSources?: string[];
  collectionIds?: string[];
  tagIds?: string[];
  tagMode?: "ANY" | "ALL";
  includeSubcollections?: boolean;
  hasPdf?: boolean;
  hasDocument?: boolean;
  hasAsset?: boolean;
  addedFrom?: string;
  addedTo?: string;
  modifiedFrom?: string;
  modifiedTo?: string;
  sort?: "ADDED" | "MODIFIED" | "TITLE" | "AUTHOR" | "YEAR";
  direction?: "ASC" | "DESC";
};

function cookie(name: string): string {
  const prefix = `${encodeURIComponent(name)}=`;
  const part = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : "";
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", cookie("litv2_csrf"));
  }
  const response = await fetch(path, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // Keep the status fallback for non-JSON infrastructure failures.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function currentSession(): Promise<SessionInfo | null> {
  const response = await fetch("/api/v2/auth/session", { credentials: "include" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`Unable to load session (${response.status})`);
  return (await response.json()) as SessionInfo;
}

export const api = {
  libraries: () => request<{ libraries: Library[] }>("/api/v2/libraries"),
  createGroup: (name: string) =>
    request<Library>("/api/v2/libraries", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  members: (libraryId: string) =>
    request<{ members: Member[] }>(`/api/v2/libraries/${libraryId}/members`),
  updateMemberRole: (libraryId: string, principalId: string, role: Member["role"]) =>
    request<Member>(`/api/v2/libraries/${libraryId}/members/${principalId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  removeMember: (libraryId: string, principalId: string) =>
    request<void>(`/api/v2/libraries/${libraryId}/members/${principalId}`, {
      method: "DELETE",
    }),
  invitations: (libraryId: string) =>
    request<{ invitations: Invitation[] }>(`/api/v2/libraries/${libraryId}/invitations`),
  invite: (libraryId: string, email: string, role: "EDITOR" | "VIEWER") =>
    request<Invitation>(`/api/v2/libraries/${libraryId}/invitations`, {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),
  regenerateInvitation: (libraryId: string, invitationId: string) =>
    request<Invitation>(
      `/api/v2/libraries/${libraryId}/invitations/${invitationId}/regenerate`,
      { method: "POST" },
    ),
  collections: (libraryId: string) =>
    request<{ collections: Collection[] }>(`/api/v2/libraries/${libraryId}/collections`),
  createCollection: (libraryId: string, name: string, parentCollectionId: string | null) =>
    request<Collection>(`/api/v2/libraries/${libraryId}/collections`, {
      method: "POST",
      body: JSON.stringify({ name, parent_collection_id: parentCollectionId }),
    }),
  updateCollection: (
    libraryId: string,
    collectionId: string,
    value: { name: string; parentCollectionId: string | null; expectedRevision: number },
  ) =>
    request<Collection>(`/api/v2/libraries/${libraryId}/collections/${collectionId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: value.name,
        parent_collection_id: value.parentCollectionId,
        expected_revision: value.expectedRevision,
      }),
    }),
  deleteCollection: (libraryId: string, collectionId: string, expectedRevision: number) =>
    request<void>(
      `/api/v2/libraries/${libraryId}/collections/${collectionId}` +
        `?expected_revision=${expectedRevision}`,
      { method: "DELETE" },
    ),
  addItemToCollection: (libraryId: string, collectionId: string, itemId: string) =>
    request<void>(
      `/api/v2/libraries/${libraryId}/collections/${collectionId}/items/${itemId}`,
      { method: "PUT" },
    ),
  removeItemFromCollection: (libraryId: string, collectionId: string, itemId: string) =>
    request<void>(
      `/api/v2/libraries/${libraryId}/collections/${collectionId}/items/${itemId}`,
      { method: "DELETE" },
    ),
  tags: (libraryId: string) =>
    request<{ tags: LibraryTag[] }>(`/api/v2/libraries/${libraryId}/tags`),
  createTag: (libraryId: string, name: string, color: string | null = null) =>
    request<LibraryTag>(`/api/v2/libraries/${libraryId}/tags`, {
      method: "POST",
      body: JSON.stringify({ name, color }),
    }),
  deleteTag: (libraryId: string, tagId: string, expectedRevision: number) =>
    request<void>(
      `/api/v2/libraries/${libraryId}/tags/${tagId}?expected_revision=${expectedRevision}`,
      { method: "DELETE" },
    ),
  items: (
    libraryId: string,
    collectionId?: string | null,
    status = "ACTIVE",
    options: {
      tagId?: string | null;
      cursor?: string | null;
      limit?: number;
      query?: string | null;
      advanced?: AdvancedMetadataSearch;
    } = {},
  ) => {
    const search = new URLSearchParams({
      status,
      limit: String(options.limit ?? 24),
    });
    if (collectionId) search.set("collection_id", collectionId);
    if (options.tagId) search.set("tag_id", options.tagId);
    if (options.cursor) search.set("cursor", options.cursor);
    if (options.query) search.set("q", options.query);
    const advanced = options.advanced;
    if (advanced) {
      const scalarValues: Array<[string, string | number | boolean | undefined]> = [
        ["title", advanced.title],
        ["author", advanced.author],
        ["identifier", advanced.identifier],
        ["venue", advanced.venue],
        ["year_from", advanced.yearFrom],
        ["year_to", advanced.yearTo],
        ["tag_mode", advanced.tagMode],
        ["include_subcollections", advanced.includeSubcollections],
        ["has_pdf", advanced.hasPdf],
        ["has_document", advanced.hasDocument],
        ["has_asset", advanced.hasAsset],
        ["added_from", advanced.addedFrom],
        ["added_to", advanced.addedTo],
        ["modified_from", advanced.modifiedFrom],
        ["modified_to", advanced.modifiedTo],
        ["sort", advanced.sort],
        ["direction", advanced.direction],
      ];
      for (const [key, value] of scalarValues) {
        if (value !== undefined && value !== "") search.set(key, String(value));
      }
      for (const value of advanced.workTypes ?? []) search.append("work_type", value);
      for (const value of advanced.metadataSources ?? []) search.append("metadata_source", value);
      for (const value of advanced.collectionIds ?? []) search.append("collection_ids", value);
      for (const value of advanced.tagIds ?? []) search.append("tag_ids", value);
    }
    return request<{ items: CatalogueItem[]; next_cursor: string | null }>(
      `/api/v2/libraries/${libraryId}/items?${search}`,
    );
  },
  createItem: (
    libraryId: string,
    value: {
      metadata: PaperMetadata;
      doi: string;
      collectionIds: string[];
      tagIds?: string[];
    },
  ) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items`, {
      method: "POST",
      body: JSON.stringify({
        metadata: {
          ...value.metadata,
          provenance: { source: "manual" },
        },
        identifiers: [{ scheme: "DOI", value: value.doi }],
        collection_ids: value.collectionIds,
        tag_ids: value.tagIds ?? [],
      }),
    }),
  item: (libraryId: string, itemId: string) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items/${itemId}`),
  itemResources: (libraryId: string, itemId: string) =>
    request<ItemResources>(`/api/v2/libraries/${libraryId}/items/${itemId}/resources`),
  uploadPrimaryPdf: (
    libraryId: string,
    itemId: string,
    file: File,
    expectedRevision?: number,
  ) => {
    const search = new URLSearchParams({ filename: file.name });
    if (expectedRevision !== undefined) {
      search.set("expected_revision", String(expectedRevision));
    }
    return request<{ primary_pdf: ArtifactResource; canonical_promoted: boolean }>(
      `/api/v2/libraries/${libraryId}/items/${itemId}/resources/primary-pdf?${search}`,
      {
        method: "PUT",
        body: file,
        headers: { "Content-Type": "application/pdf" },
      },
    );
  },
  cancelPrimaryPdf: (
    libraryId: string,
    itemId: string,
    expectedRevision: number,
  ) =>
    request<{ primary_pdf: ArtifactResource | null }>(
      `/api/v2/libraries/${libraryId}/items/${itemId}/resources/primary-pdf` +
        `?expected_revision=${expectedRevision}`,
      { method: "DELETE" },
    ),
  uploadAsset: (libraryId: string, itemId: string, file: File) =>
    request<AssetResource>(
      `/api/v2/libraries/${libraryId}/items/${itemId}/resources/assets` +
        `?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        body: file,
        headers: { "Content-Type": file.type || "application/octet-stream" },
      },
    ),
  renameAsset: (
    libraryId: string,
    itemId: string,
    assetId: string,
    displayName: string,
    expectedRevision: number,
  ) =>
    request<AssetResource>(
      `/api/v2/libraries/${libraryId}/items/${itemId}/resources/assets/${assetId}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName, expected_revision: expectedRevision }),
      },
    ),
  deleteAsset: (
    libraryId: string,
    itemId: string,
    assetId: string,
    expectedRevision: number,
  ) =>
    request<void>(
      `/api/v2/libraries/${libraryId}/items/${itemId}/resources/assets/${assetId}` +
        `?expected_revision=${expectedRevision}`,
      { method: "DELETE" },
    ),
  artifactContentPath: (libraryId: string, itemId: string, artifactKey: string) =>
    `/api/v2/libraries/${libraryId}/items/${itemId}/resources/artifacts/` +
    `${encodeURIComponent(artifactKey)}/content`,
  assetContentPath: (libraryId: string, itemId: string, assetId: string) =>
    `/api/v2/libraries/${libraryId}/items/${itemId}/resources/assets/${assetId}/content`,
  refreshMetadata: (
    libraryId: string,
    itemIds: string[],
    refreshMode: "AUTO" | "MANUAL" = "MANUAL",
  ) =>
    request<{
      request_id: string;
      jobs: Array<{ job_id: string; library_item_id: string; status: string }>;
    }>(`/api/v2/libraries/${libraryId}/items/metadata-refresh`, {
      method: "POST",
      body: JSON.stringify({
        library_item_ids: itemIds,
        request_id: crypto.randomUUID(),
        refresh_mode: refreshMode,
      }),
    }),
  importCitations: (libraryId: string, file: File) =>
    request<{ job_id: string; status: string; filename: string }>(
      `/api/v2/libraries/${libraryId}/imports/citations?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        body: file,
        headers: { "Content-Type": file.type || "application/octet-stream" },
      },
    ),
  importZotero: (libraryId: string, file: File) =>
    request<{ job_id: string; status: string; filename: string }>(
      `/api/v2/libraries/${libraryId}/imports/zotero?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        body: file,
        headers: { "Content-Type": "application/vnd.sqlite3" },
      },
    ),
  zoteroAttachments: (libraryId: string, jobId: string) =>
    request<{ source_id: string; attachments: ZoteroAttachment[] }>(
      `/api/v2/libraries/${libraryId}/imports/zotero/${jobId}/attachments`,
    ),
  importZoteroAttachment: (
    libraryId: string,
    jobId: string,
    attachment: ZoteroAttachment,
    file: File,
  ) => {
    const search = new URLSearchParams({
      zotero_library_id: String(attachment.zotero_library_id),
      item_key: attachment.item_key,
    });
    return request<{
      library_item_id: string;
      attachment_key: string;
      role: "PRIMARY_PDF" | "ASSET";
      blob_id: string;
      canonical_promoted: boolean;
    }>(
      `/api/v2/libraries/${libraryId}/imports/zotero/${jobId}/attachments/${encodeURIComponent(attachment.attachment_key)}?${search}`,
      {
        method: "POST",
        body: file,
        headers: { "Content-Type": "application/pdf" },
      },
    );
  },
  importPdf: (libraryId: string, file: File, collectionId: string | null) => {
    const search = new URLSearchParams({ filename: file.name });
    if (collectionId) search.set("collection_id", collectionId);
    return request<{
      job_id: string;
      status: string;
      filename: string;
      library_item_id: string;
      initial_item: CatalogueItem | null;
      reused: boolean;
    }>(`/api/v2/libraries/${libraryId}/imports/pdfs?${search}`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": "application/pdf" },
    });
  },
  job: (libraryId: string, jobId: string) =>
    request<{
      job_id: string;
      status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
      attempt_count: number;
      max_attempts: number;
      progress_current: number;
      progress_total: number;
      progress_message: string | null;
      result: Record<string, unknown> | null;
      error: Record<string, unknown> | null;
    }>(`/api/v2/libraries/${libraryId}/jobs/${jobId}`),
  trashItem: (libraryId: string, itemId: string, expectedRevision: number) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items/${itemId}/trash`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    }),
  restoreItem: (libraryId: string, itemId: string, expectedRevision: number) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items/${itemId}/restore`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    }),
  updateItemOverrides: (
    libraryId: string,
    itemId: string,
    expectedRevision: number,
    overrides: Record<string, unknown>,
  ) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items/${itemId}/overrides`, {
      method: "PATCH",
      body: JSON.stringify({ expected_revision: expectedRevision, overrides }),
    }),
  updateItem: (
    libraryId: string,
    itemId: string,
    expectedRevision: number,
    overrides: Record<string, unknown>,
    collectionIds: string[],
    tagIds: string[],
  ) =>
    request<CatalogueItem>(`/api/v2/libraries/${libraryId}/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        overrides,
        collection_ids: collectionIds,
        tag_ids: tagIds,
      }),
    }),
  bulkOrganize: (
    libraryId: string,
    items: Array<{ library_item_id: string; expected_revision: number }>,
    action:
      | "ADD_COLLECTION"
      | "REMOVE_COLLECTION"
      | "ADD_TAG"
      | "REMOVE_TAG"
      | "TRASH"
      | "RESTORE",
    targetId: string | null,
  ) =>
    request<{ updated: number; action: string }>(
      `/api/v2/libraries/${libraryId}/items/bulk-organize`,
      {
        method: "POST",
        body: JSON.stringify({ items, action, target_id: targetId }),
      },
    ),
  acceptInvitation: (token: string) =>
    request<Library>("/api/v2/library-invitations/accept", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  logout: () =>
    request<{ status: string; provider_logout_url: string | null }>(
      "/api/v2/auth/logout",
      { method: "POST" },
    ),
};
