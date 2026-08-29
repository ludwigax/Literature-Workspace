import { FormEvent, useEffect, useMemo, useState } from "react";

import { documentApi } from "./documentApi";
import type { DocumentDatabase, Evidence, EvidenceSearchResult, PipelineDocumentContent } from "./types";

type DatabaseSelection = { selected: boolean; topK: number; weight: number };

function score(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(5);
}

export function RetrievalPanel() {
  const [databases, setDatabases] = useState<DocumentDatabase[]>([]);
  const [selections, setSelections] = useState<Record<string, DatabaseSelection>>({});
  const [result, setResult] = useState<EvidenceSearchResult | null>(null);
  const [selectedDocument, setSelectedDocument] = useState<PipelineDocumentContent | null>(null);
  const [expandedDocuments, setExpandedDocuments] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    void documentApi.databases().then((value) => {
      const available = value.databases.filter((database) => database.status === "ACTIVE" && database.current_release_id);
      setDatabases(available);
      setSelections(Object.fromEntries(available.map((database, index) => [database.database_id, { selected: index === 0, topK: 20, weight: 1 }])));
    }).catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Unable to load Document Databases"));
  }, []);

  const selectedCount = useMemo(() => Object.values(selections).filter((value) => value.selected).length, [selections]);

  function updateSelection(databaseId: string, value: Partial<DatabaseSelection>) {
    setSelections((current) => ({ ...current, [databaseId]: { ...(current[databaseId] ?? { selected: false, topK: 20, weight: 1 }), ...value } }));
  }

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const chosen = databases.filter((database) => selections[database.database_id]?.selected);
    if (chosen.length === 0) { setNotice("Select at least one published Document Database."); return; }
    setBusy(true); setNotice(""); setResult(null); setExpandedDocuments([]);
    try {
      setResult(await documentApi.retrieve({
        query: String(data.get("query") ?? ""),
        databases: chosen.map((database) => ({ database_id: database.database_id, top_k: selections[database.database_id].topK, weight: selections[database.database_id].weight })),
        mode: String(data.get("mode")) as "BM25" | "VECTOR" | "HYBRID",
        aggregation: String(data.get("aggregation")) as "MAX" | "INTEGRATE",
        database_top_k: Number(data.get("database_top_k") ?? 20),
        total_top_k: Number(data.get("total_top_k") ?? 20),
        chunk_top_k_per_document: Number(data.get("chunk_top_k_per_document") ?? 5),
        integrate_decay: Number(data.get("integrate_decay") ?? 0.5),
        rrf_k: Number(data.get("rrf_k") ?? 60),
        facet_1: String(data.get("facet_1") ?? "").trim() || undefined,
        facet_2: String(data.get("facet_2") ?? "").trim() || undefined,
      }));
    } catch (error) { setNotice(error instanceof Error ? error.message : "Retrieval failed"); }
    finally { setBusy(false); }
  }

  async function openDocument(documentId: string) {
    setBusy(true);
    try { setSelectedDocument(await documentApi.document(documentId)); }
    catch (error) { setNotice(error instanceof Error ? error.message : "Unable to load Document"); }
    finally { setBusy(false); }
  }

  const evidence = result?.global_evidence ?? result?.database_results.flatMap((value) => value.evidence) ?? [];

  return (
    <section className="feature-panel retrieval-panel">
      <header className="feature-panel-header">
        <div><span className="eyebrow">EVIDENCE RETRIEVAL</span><h1>Search published Document Databases</h1></div>
        <div className="feature-header-actions"><a href="/">Library</a></div>
      </header>
      {notice && <button className="feature-notice" onClick={() => setNotice("")}>{notice}</button>}
      <form
        className="retrieval-workspace"
        onSubmit={search}
        onInvalidCapture={(event) => {
          const field = event.target as HTMLInputElement;
          const details = field.closest("details");
          if (details) details.open = true;
          setNotice(`${field.name || "Retrieval option"}: ${field.validationMessage}`);
        }}
      >
        <aside className="retrieval-sources">
          <div className="feature-index-heading"><strong>SOURCES</strong><span>{selectedCount} selected</span></div>
          <div className="retrieval-database-list">
            {databases.map((database) => {
              const selection = selections[database.database_id] ?? { selected: false, topK: 20, weight: 1 };
              return <article className={selection.selected ? "selected" : ""} key={database.database_id}><label className="database-check"><input type="checkbox" checked={selection.selected} onChange={(event) => updateSelection(database.database_id, { selected: event.target.checked })} /><span><strong>{database.name}</strong><small>{database.retrieval_status} · release {database.current_release_id?.slice(0, 8)}</small></span></label><div className="source-weights"><label>Top K<input type="number" min="1" max="100" value={selection.topK} onChange={(event) => updateSelection(database.database_id, { topK: Number(event.target.value) })} /></label><label>Weight<input type="number" min="0.01" max="100" step="any" value={selection.weight} onChange={(event) => updateSelection(database.database_id, { weight: Number(event.target.value) })} /></label></div></article>;
            })}
            {databases.length === 0 && <p className="feature-empty">No published Document Database is available.</p>}
          </div>
        </aside>
        <main className="retrieval-main">
          <div className="retrieval-querybar"><textarea name="query" placeholder="Describe the evidence you are looking for…" required /><button className="primary-button" disabled={busy || selectedCount === 0}>{busy ? "Searching…" : "Search evidence"}</button></div>
          <details className="retrieval-options"><summary>Retrieval controls</summary><div><label>Mode<select name="mode" defaultValue="HYBRID"><option>HYBRID</option><option>BM25</option><option>VECTOR</option></select></label><label>Aggregation<select name="aggregation" defaultValue="MAX"><option>MAX</option><option>INTEGRATE</option></select></label><label>Total top K<input name="total_top_k" type="number" min="1" max="200" defaultValue="20" /></label><label>Default DB top K<input name="database_top_k" type="number" min="1" max="100" defaultValue="20" /></label><label>Chunks / document<input name="chunk_top_k_per_document" type="number" min="1" max="20" defaultValue="5" /></label><label>Integrate decay<input name="integrate_decay" type="number" min="0.01" max="1" step="any" defaultValue="0.5" /></label><label>RRF K<input name="rrf_k" type="number" min="1" max="1000" defaultValue="60" /></label><label>Facet 1<input name="facet_1" /></label><label>Facet 2<input name="facet_2" /></label></div></details>
          <section className="evidence-results">
            {result && <header className="results-heading"><div><strong>{evidence.length} evidence documents</strong><span>{result.mode} · {result.aggregation} · {result.status}</span></div>{result.status === "PARTIAL" && <small>Global fusion withheld because one or more databases failed.</small>}</header>}
            {result?.database_statuses.some((value) => value.status === "FAILED") && <div className="database-failures">{result.database_statuses.filter((value) => value.status === "FAILED").map((value) => <span key={value.database_id}>{databases.find((database) => database.database_id === value.database_id)?.name ?? value.database_id}: {value.error}</span>)}</div>}
            {evidence.map((value, index) => <EvidenceCard evidence={value} rank={index + 1} expanded={expandedDocuments.includes(value.document_id)} onToggle={() => setExpandedDocuments((current) => current.includes(value.document_id) ? current.filter((id) => id !== value.document_id) : [...current, value.document_id])} onOpen={() => void openDocument(value.document_id)} key={value.document_id} />)}
            {!result && <div className="feature-detail-empty"><span>⌕</span><p>Select databases and enter a research question.</p></div>}
            {result && evidence.length === 0 && <div className="feature-detail-empty"><span>◇</span><p>No matching evidence was found.</p></div>}
          </section>
        </main>
      </form>
      {selectedDocument && <div className="feature-modal-backdrop" onClick={() => setSelectedDocument(null)}><section className="document-reader" onClick={(event) => event.stopPropagation()}><header><div><span className="eyebrow">PIPELINE DOCUMENT</span><h2>{selectedDocument.display_title}</h2><small>{selectedDocument.word_count} words · {selectedDocument.chunk_count} chunks</small></div><button onClick={() => setSelectedDocument(null)}>Close</button></header><pre>{selectedDocument.content}</pre></section></div>}
    </section>
  );
}

function EvidenceCard({ evidence, rank, expanded, onToggle, onOpen }: { evidence: Evidence; rank: number; expanded: boolean; onToggle: () => void; onOpen: () => void }) {
  const scoreByChunk = new Map(evidence.chunk_scores.map((value) => [value.chunk_id, value]));
  const authors = evidence.paper.authors.map((value) => value.name).filter(Boolean).slice(0, 4).join(", ");
  return <article className="evidence-card"><header><span className="evidence-rank">{String(rank).padStart(2, "0")}</span><div><h2>{evidence.paper.title || evidence.document.display_title}</h2><p>{authors || "Unknown authors"} · {evidence.paper.publication_year ?? "Undated"} · {evidence.paper.venue || "No venue"}</p></div><div className="evidence-score"><strong>{score(evidence.cross_database_score ?? evidence.document_score.value)}</strong><small>{evidence.cross_database_score == null ? `${evidence.document_score.aggregation} document score` : "cross-database RRF"}</small></div></header><div className="evidence-meta"><span>{evidence.document.display_title}</span><span>{evidence.document.word_count} words</span><span>{evidence.document_score.matched_chunk_count} matched chunks</span><button type="button" onClick={onOpen}>Open document</button></div><button className="chunk-toggle" type="button" onClick={onToggle}>{expanded ? "Hide supporting chunks" : `Show ${evidence.chunks.length} supporting chunks`}</button>{expanded && <div className="evidence-chunks">{evidence.chunks.map((chunk) => { const chunkScore = scoreByChunk.get(chunk.chunk_id); return <article key={chunk.chunk_id}><header><strong>Chunk {chunk.ordinal + 1}</strong><span>rank {score(chunkScore?.ranking_score)} · BM25 {score(chunkScore?.bm25)} · vector {score(chunkScore?.embedding)}</span></header><p>{chunk.content}</p>{(chunk.facet_1 || chunk.facet_2) && <footer>{chunk.facet_1 && <span>{chunk.facet_1}</span>}{chunk.facet_2 && <span>{chunk.facet_2}</span>}</footer>}</article>; })}</div>}</article>;
}
