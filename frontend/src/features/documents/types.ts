export type DocumentPipeline = {
  pipeline_id: string;
  name: string;
  description: string;
  status: "ACTIVE" | "ARCHIVED";
  active_version_id: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type PipelineVersion = {
  pipeline_version_id: string;
  pipeline_id: string;
  version: number;
  system_prompt: string;
  user_prompt: string;
  model: string;
  model_config: Record<string, unknown>;
  input_config: Record<string, unknown>;
  splitter_type: "WHOLE" | "JSON" | "PARAGRAPH" | "MARKDOWN" | "ADVANCED";
  splitter_config: Record<string, unknown>;
  config_hash: string;
  created_at: string | null;
};

export type PipelineVersionInput = {
  system_prompt: string;
  user_prompt: string;
  model: string;
  model_config: Record<string, unknown>;
  input_config: Record<string, unknown>;
  splitter_type: PipelineVersion["splitter_type"];
  splitter_config: Record<string, unknown>;
};

export type DocumentDatabase = {
  database_id: string;
  pipeline_id: string;
  name: string;
  description: string;
  status: "ACTIVE" | "ARCHIVED";
  range_mode: "EXPLICIT" | "ALL_VERIFIED";
  range_revision: number;
  current_release_id: string | null;
  building_release_id: string | null;
  embedding_profile: Record<string, unknown>;
  bm25_profile: Record<string, unknown>;
  retrieval_status: "NOT_CONFIGURED" | "PENDING" | "READY" | "FAILED";
  auto_reconcile_enabled: boolean;
  last_reconcile_checked_at: string | null;
  next_reconcile_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type DocumentScope = {
  database_id: string;
  range_mode: DocumentDatabase["range_mode"];
  range_revision: number;
  canonical_paper_ids: string[];
  explicit_canonical_paper_ids: string[];
};

export type DocumentRelease = {
  release_id: string;
  database_id: string;
  release_number: number;
  pipeline_version_id: string;
  range_revision: number;
  build_mode: "FULL" | "UPDATE";
  trigger_reason: string;
  status: "BUILDING" | "CURRENT" | "ARCHIVED" | "FAILED";
  expected_count: number;
  completed_count: number;
  failed_count: number;
  retrieval_status: DocumentDatabase["retrieval_status"];
  published_at: string | null;
  archived_at: string | null;
  created_at: string | null;
};

export type DocumentBuildRun = {
  run_id: string;
  database_id: string;
  release_id: string | null;
  pipeline_version_id: string;
  range_revision: number;
  build_mode: "FULL" | "UPDATE";
  trigger_reason: string;
  status: "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  phase: string;
  reconcile_requested: boolean;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
};

export type DocumentBuildTask = {
  task_id: string;
  task_type: string;
  queue_name: string;
  subject_key: string;
  status: string;
  progress_current: number;
  progress_total: number;
  progress_message: string | null;
  attempt_count: number;
  max_attempts: number;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
};

export type ChunkScore = {
  chunk_id: string;
  ranking_score: number;
  bm25: number | null;
  embedding: number | null;
};

export type EvidenceChunk = {
  chunk_id: string;
  ordinal: number;
  content: string;
  facet_1: string | null;
  facet_2: string | null;
};

export type Evidence = {
  document_id: string;
  paper: {
    canonical_paper_id: string;
    title: string | null;
    authors: Array<{ name?: string }>;
    publication_year: number | null;
    venue: string | null;
    identifiers: Array<{ scheme: string; value: string }>;
  };
  document: {
    display_title: string;
    pipeline_version_id: string;
    media_type: string;
    word_count: number;
  };
  chunks: EvidenceChunk[];
  chunk_scores: ChunkScore[];
  document_score: { value: number; aggregation: "MAX" | "INTEGRATE"; matched_chunk_count: number };
  database_rank?: number;
  cross_database_score?: number;
  database_matches?: Array<{ database_id: string; rank: number; weight: number }>;
};

export type EvidenceSearchResult = {
  query: string;
  mode: "BM25" | "VECTOR" | "HYBRID";
  aggregation: "MAX" | "INTEGRATE";
  status: "SUCCEEDED" | "PARTIAL";
  database_statuses: Array<{ database_id: string; status: "SUCCEEDED" | "FAILED"; error: string | null }>;
  database_results: Array<{
    database_id: string;
    database_name: string;
    release_id: string;
    weight: number;
    top_k: number;
    evidence: Evidence[];
  }>;
  global_evidence: Evidence[] | null;
};

export type PipelineDocumentContent = {
  document_id: string;
  canonical_paper_id: string;
  pipeline_version_id: string;
  display_title: string;
  media_type: string;
  content: string;
  content_sha256: string;
  word_count: number;
  chunk_count: number;
  provenance: Record<string, unknown>;
};
