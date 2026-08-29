import { request } from "../../api";
import type {
  DocumentBuildRun,
  DocumentBuildTask,
  DocumentDatabase,
  DocumentPipeline,
  DocumentRelease,
  DocumentScope,
  EvidenceSearchResult,
  PipelineDocumentContent,
  PipelineVersion,
  PipelineVersionInput,
} from "./types";

const base = "/api/v2";

export const documentApi = {
  pipelines: () => request<{ pipelines: DocumentPipeline[] }>(`${base}/document-pipelines`),
  pipelineVersions: (pipelineId: string) =>
    request<{ versions: PipelineVersion[] }>(`${base}/document-pipelines/${pipelineId}/versions`),
  createPipeline: (value: { name: string; description: string; initial_version: PipelineVersionInput }) =>
    request<{ pipeline: DocumentPipeline; active_version: PipelineVersion }>(`${base}/document-pipelines`, {
      method: "POST",
      body: JSON.stringify(value),
    }),
  updatePipeline: (pipelineId: string, value: Partial<Pick<DocumentPipeline, "name" | "description" | "status">>) =>
    request<DocumentPipeline>(`${base}/document-pipelines/${pipelineId}`, {
      method: "PATCH",
      body: JSON.stringify(value),
    }),
  addPipelineVersion: (pipelineId: string, value: PipelineVersionInput) =>
    request<{ version: PipelineVersion; created: boolean }>(`${base}/document-pipelines/${pipelineId}/versions`, {
      method: "POST",
      body: JSON.stringify(value),
    }),
  databases: () => request<{ databases: DocumentDatabase[] }>(`${base}/document-databases`),
  createDatabase: (value: {
    pipeline_id: string;
    name: string;
    description: string;
    range_mode: DocumentDatabase["range_mode"];
    embedding_profile?: Record<string, unknown>;
    bm25_profile: Record<string, unknown>;
  }) => request<DocumentDatabase>(`${base}/document-databases`, { method: "POST", body: JSON.stringify(value) }),
  updateDatabase: (databaseId: string, value: Partial<Pick<DocumentDatabase, "name" | "description" | "status" | "range_mode" | "embedding_profile" | "bm25_profile">>) =>
    request<DocumentDatabase>(`${base}/document-databases/${databaseId}`, { method: "PATCH", body: JSON.stringify(value) }),
  scope: (databaseId: string) => request<DocumentScope>(`${base}/document-databases/${databaseId}/scope`),
  replaceScope: (databaseId: string, paperIds: string[]) =>
    request<{ database_id: string; changed: boolean }>(`${base}/document-databases/${databaseId}/scope`, {
      method: "PUT",
      body: JSON.stringify({ canonical_paper_ids: paperIds }),
    }),
  setReconcilePolicy: (databaseId: string, enabled: boolean) =>
    request<DocumentDatabase>(`${base}/document-databases/${databaseId}/reconcile-policy`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  reconcile: (databaseId: string, buildMode: "FULL" | "UPDATE") =>
    request<DocumentBuildRun>(`${base}/document-databases/${databaseId}/reconcile`, {
      method: "POST",
      body: JSON.stringify({ build_mode: buildMode }),
    }),
  releases: (databaseId: string) =>
    request<{ releases: DocumentRelease[] }>(`${base}/document-databases/${databaseId}/releases`),
  runs: (databaseId?: string) => {
    const query = databaseId ? `?database_id=${encodeURIComponent(databaseId)}` : "";
    return request<{ runs: DocumentBuildRun[] }>(`${base}/document-build-runs${query}`);
  },
  run: (runId: string) =>
    request<{ run: DocumentBuildRun; tasks: DocumentBuildTask[] }>(`${base}/document-build-runs/${runId}`),
  cancelRun: (runId: string) => request<DocumentBuildRun>(`${base}/document-build-runs/${runId}/cancel`, { method: "POST" }),
  retryRun: (runId: string) => request<DocumentBuildRun>(`${base}/document-build-runs/${runId}/retry`, { method: "POST" }),
  retrieve: (value: {
    query: string;
    databases: Array<{ database_id: string; top_k: number; weight: number }>;
    mode: "BM25" | "VECTOR" | "HYBRID";
    aggregation: "MAX" | "INTEGRATE";
    database_top_k: number;
    total_top_k: number;
    chunk_top_k_per_document: number;
    integrate_decay: number;
    rrf_k: number;
    facet_1?: string;
    facet_2?: string;
  }) => request<EvidenceSearchResult>(`${base}/retrieval/search`, { method: "POST", body: JSON.stringify(value) }),
  document: (documentId: string) => request<PipelineDocumentContent>(`${base}/documents/${documentId}`),
};
