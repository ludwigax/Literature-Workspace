import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { documentApi } from "./documentApi";
import type {
  DocumentBuildRun,
  DocumentBuildTask,
  DocumentDatabase,
  DocumentPipeline,
  DocumentRelease,
  DocumentScope,
  PipelineVersion,
  PipelineVersionInput,
} from "./types";

type AdminSection = "PIPELINES" | "DATABASES" | "RUNS";

function jsonObject(value: FormDataEntryValue | null, label: string): Record<string, unknown> {
  const text = String(value ?? "").trim();
  if (!text) return {};
  const parsed: unknown = JSON.parse(text);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label} must be a JSON object`);
  return parsed as Record<string, unknown>;
}

function versionInput(form: HTMLFormElement): PipelineVersionInput {
  const data = new FormData(form);
  const executionMode = String(data.get("execution_mode") ?? "DIRECT_TEXT");
  return {
    system_prompt: String(data.get("system_prompt") ?? ""),
    user_prompt: String(data.get("user_prompt") ?? ""),
    model: String(data.get("model") ?? ""),
    input_config: { source: "canonical_pdf_text", execution_mode: executionMode },
    model_config: jsonObject(data.get("model_config"), "Model config"),
    splitter_type: String(data.get("splitter_type") ?? "PARAGRAPH") as PipelineVersionInput["splitter_type"],
    splitter_config: jsonObject(data.get("splitter_config"), "Splitter config"),
  };
}

function shortTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function DocumentAdminPanel() {
  const [section, setSection] = useState<AdminSection>("DATABASES");
  const [pipelines, setPipelines] = useState<DocumentPipeline[]>([]);
  const [databases, setDatabases] = useState<DocumentDatabase[]>([]);
  const [runs, setRuns] = useState<DocumentBuildRun[]>([]);
  const [selectedPipelineId, setSelectedPipelineId] = useState("");
  const [selectedDatabaseId, setSelectedDatabaseId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [versions, setVersions] = useState<PipelineVersion[]>([]);
  const [scope, setScope] = useState<DocumentScope | null>(null);
  const [releases, setReleases] = useState<DocumentRelease[]>([]);
  const [tasks, setTasks] = useState<DocumentBuildTask[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [versionOpen, setVersionOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const selectedPipeline = pipelines.find((value) => value.pipeline_id === selectedPipelineId) ?? null;
  const selectedDatabase = databases.find((value) => value.database_id === selectedDatabaseId) ?? null;
  const selectedRun = runs.find((value) => value.run_id === selectedRunId) ?? null;
  const activeVersion = versions.find((value) => value.pipeline_version_id === selectedPipeline?.active_version_id) ?? versions[0] ?? null;

  const loadOverview = useCallback(async () => {
    const [pipelineValue, databaseValue, runValue] = await Promise.all([
      documentApi.pipelines(), documentApi.databases(), documentApi.runs(),
    ]);
    setPipelines(pipelineValue.pipelines);
    setDatabases(databaseValue.databases);
    setRuns(runValue.runs);
    setSelectedPipelineId((current) => pipelineValue.pipelines.some((value) => value.pipeline_id === current) ? current : pipelineValue.pipelines[0]?.pipeline_id ?? "");
    setSelectedDatabaseId((current) => databaseValue.databases.some((value) => value.database_id === current) ? current : databaseValue.databases[0]?.database_id ?? "");
    setSelectedRunId((current) => runValue.runs.some((value) => value.run_id === current) ? current : runValue.runs[0]?.run_id ?? "");
  }, []);

  useEffect(() => { void loadOverview().catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Unable to load Document administration")); }, [loadOverview]);

  useEffect(() => {
    if (!selectedPipelineId) { setVersions([]); return; }
    void documentApi.pipelineVersions(selectedPipelineId).then((value) => setVersions(value.versions)).catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Unable to load versions"));
  }, [selectedPipelineId]);

  const loadDatabaseDetails = useCallback(async () => {
    if (!selectedDatabaseId) { setScope(null); setReleases([]); return; }
    const [scopeValue, releaseValue, runValue] = await Promise.all([
      documentApi.scope(selectedDatabaseId), documentApi.releases(selectedDatabaseId), documentApi.runs(selectedDatabaseId),
    ]);
    setScope(scopeValue);
    setReleases(releaseValue.releases);
    setRuns((current) => [...runValue.runs, ...current.filter((value) => value.database_id !== selectedDatabaseId)]);
  }, [selectedDatabaseId]);

  useEffect(() => { void loadDatabaseDetails().catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Unable to load database details")); }, [loadDatabaseDetails]);

  useEffect(() => {
    if (!selectedRunId) { setTasks([]); return; }
    void documentApi.run(selectedRunId).then((value) => { setTasks(value.tasks); setRuns((current) => current.map((run) => run.run_id === value.run.run_id ? value.run : run)); }).catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Unable to load build run"));
  }, [selectedRunId]);

  const hasRunning = useMemo(() => runs.some((value) => value.status === "RUNNING"), [runs]);
  const indexRows = useMemo(() => {
    if (section === "PIPELINES") return pipelines.map((value) => ({ id: value.pipeline_id, title: value.name, meta: value.status }));
    if (section === "DATABASES") return databases.map((value) => ({ id: value.database_id, title: value.name, meta: value.retrieval_status }));
    return runs.map((value) => ({ id: value.run_id, title: `${value.build_mode} · ${value.phase}`, meta: value.status }));
  }, [databases, pipelines, runs, section]);
  useEffect(() => {
    if (!hasRunning) return;
    const timer = window.setInterval(() => void loadOverview().then(loadDatabaseDetails).catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
  }, [hasRunning, loadDatabaseDetails, loadOverview]);

  async function action(operation: () => Promise<unknown>, success: string) {
    setBusy(true);
    try { await operation(); setNotice(success); await loadOverview(); await loadDatabaseDetails(); }
    catch (error) { setNotice(error instanceof Error ? error.message : "Operation failed"); }
    finally { setBusy(false); }
  }

  async function createPipeline(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    await action(async () => {
      const value = await documentApi.createPipeline({ name: String(data.get("name") ?? ""), description: String(data.get("description") ?? ""), initial_version: versionInput(form) });
      setSelectedPipelineId(value.pipeline.pipeline_id); setCreateOpen(false); form.reset();
    }, "Pipeline created.");
  }

  async function createDatabase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget; const data = new FormData(form);
    await action(async () => {
      const value = await documentApi.createDatabase({
        pipeline_id: String(data.get("pipeline_id") ?? ""), name: String(data.get("name") ?? ""), description: String(data.get("description") ?? ""),
        range_mode: String(data.get("range_mode") ?? "EXPLICIT") as DocumentDatabase["range_mode"],
        bm25_profile: jsonObject(data.get("bm25_profile"), "BM25 profile"),
        embedding_profile: jsonObject(data.get("embedding_profile"), "Embedding profile"),
      });
      setSelectedDatabaseId(value.database_id); setCreateOpen(false); form.reset();
    }, "Document Database created.");
  }

  async function savePipeline(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedPipeline) return;
    const data = new FormData(event.currentTarget);
    await action(() => documentApi.updatePipeline(selectedPipeline.pipeline_id, { name: String(data.get("name")), description: String(data.get("description")), status: String(data.get("status")) as DocumentPipeline["status"] }), "Pipeline details saved.");
  }

  async function addVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedPipeline) return;
    await action(async () => { await documentApi.addPipelineVersion(selectedPipeline.pipeline_id, versionInput(event.currentTarget)); setVersionOpen(false); }, "Pipeline version activated.");
  }

  async function saveDatabase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedDatabase) return;
    const data = new FormData(event.currentTarget);
    await action(() => documentApi.updateDatabase(selectedDatabase.database_id, {
      name: String(data.get("name")), description: String(data.get("description")), status: String(data.get("status")) as DocumentDatabase["status"],
      range_mode: String(data.get("range_mode")) as DocumentDatabase["range_mode"], embedding_profile: jsonObject(data.get("embedding_profile"), "Embedding profile"), bm25_profile: jsonObject(data.get("bm25_profile"), "BM25 profile"),
    }), "Database configuration saved.");
  }

  async function saveScope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedDatabase) return;
    const values = String(new FormData(event.currentTarget).get("paper_ids") ?? "").split(/[\s,]+/).map((value) => value.trim()).filter(Boolean);
    await action(() => documentApi.replaceScope(selectedDatabase.database_id, [...new Set(values)]), "Explicit paper scope saved.");
  }

  return (
    <section className="feature-panel document-admin-panel">
      <header className="feature-panel-header">
        <div><span className="eyebrow">DOCUMENT SYSTEM</span><h1>Pipeline &amp; database administration</h1></div>
        <div className="feature-header-actions"><a href="/">Library</a><a href="/retrieval">Retrieval</a><button disabled={busy} onClick={() => void loadOverview()}>Refresh</button></div>
      </header>
      <nav className="feature-tabs">
        {(["PIPELINES", "DATABASES", "RUNS"] as AdminSection[]).map((value) => <button className={section === value ? "active" : ""} key={value} onClick={() => { setSection(value); setCreateOpen(false); }}>{value.toLowerCase()}</button>)}
      </nav>
      {notice && <button className="feature-notice" onClick={() => setNotice("")}>{notice}</button>}
      <div className="feature-workspace">
        <aside className="feature-index">
          <div className="feature-index-heading"><strong>{section}</strong>{section !== "RUNS" && <button onClick={() => setCreateOpen(true)}>+ New</button>}</div>
          {indexRows.map((value) => {
            const selected = section === "PIPELINES" ? value.id === selectedPipelineId : section === "DATABASES" ? value.id === selectedDatabaseId : value.id === selectedRunId;
            return <button className={`feature-index-row ${selected ? "active" : ""}`} key={value.id} onClick={() => section === "PIPELINES" ? setSelectedPipelineId(value.id) : section === "DATABASES" ? setSelectedDatabaseId(value.id) : setSelectedRunId(value.id)}><strong>{value.title}</strong><span>{value.meta}</span></button>;
          })}
          {((section === "PIPELINES" && pipelines.length === 0) || (section === "DATABASES" && databases.length === 0) || (section === "RUNS" && runs.length === 0)) && <p className="feature-empty">No records yet.</p>}
        </aside>
        <main className="feature-detail">
          {section === "PIPELINES" && selectedPipeline && <PipelineDetail pipeline={selectedPipeline} versions={versions} activeVersion={activeVersion} busy={busy} versionOpen={versionOpen} setVersionOpen={setVersionOpen} onSave={savePipeline} onAddVersion={addVersion} />}
          {section === "DATABASES" && selectedDatabase && <DatabaseDetail database={selectedDatabase} pipeline={pipelines.find((value) => value.pipeline_id === selectedDatabase.pipeline_id)} scope={scope} releases={releases} runs={runs.filter((value) => value.database_id === selectedDatabase.database_id)} busy={busy} onSave={saveDatabase} onSaveScope={saveScope} onPolicy={(enabled) => action(() => documentApi.setReconcilePolicy(selectedDatabase.database_id, enabled), enabled ? "Automatic reconcile enabled." : "Automatic reconcile disabled.")} onRun={(mode) => action(() => documentApi.reconcile(selectedDatabase.database_id, mode), `${mode} build submitted.`)} />}
          {section === "RUNS" && selectedRun && <RunDetail run={selectedRun} tasks={tasks} busy={busy} onCancel={() => action(() => documentApi.cancelRun(selectedRun.run_id), "Build cancelled.")} onRetry={() => action(() => documentApi.retryRun(selectedRun.run_id), "Retry submitted.")} />}
          {((section === "PIPELINES" && !selectedPipeline) || (section === "DATABASES" && !selectedDatabase) || (section === "RUNS" && !selectedRun)) && <div className="feature-detail-empty"><span>◇</span><p>Select an item from the index.</p></div>}
        </main>
      </div>
      {createOpen && <div className="feature-modal-backdrop" onClick={() => setCreateOpen(false)}><section className="feature-modal" onClick={(event) => event.stopPropagation()}><header><div><span className="eyebrow">CREATE</span><h2>New {section === "PIPELINES" ? "Pipeline" : "Document Database"}</h2></div><button onClick={() => setCreateOpen(false)}>Close</button></header>{section === "PIPELINES" ? <PipelineCreateForm busy={busy} onSubmit={createPipeline} /> : <DatabaseCreateForm pipelines={pipelines} busy={busy} onSubmit={createDatabase} />}</section></div>}
    </section>
  );
}

function PipelineDetail({ pipeline, versions, activeVersion, busy, versionOpen, setVersionOpen, onSave, onAddVersion }: { pipeline: DocumentPipeline; versions: PipelineVersion[]; activeVersion: PipelineVersion | null; busy: boolean; versionOpen: boolean; setVersionOpen: (value: boolean) => void; onSave: (event: FormEvent<HTMLFormElement>) => void; onAddVersion: (event: FormEvent<HTMLFormElement>) => void }) {
  return <div className="admin-detail-stack"><header className="detail-heading"><div><span className="record-kind">PIPELINE</span><h2>{pipeline.name}</h2><p>{pipeline.pipeline_id}</p></div><button className="primary-button" onClick={() => setVersionOpen(!versionOpen)}>New version</button></header><form className="admin-form compact" key={`${pipeline.pipeline_id}:${pipeline.updated_at}`} onSubmit={onSave}><label>Name<input name="name" defaultValue={pipeline.name} required /></label><label>Status<select name="status" defaultValue={pipeline.status}><option>ACTIVE</option><option>ARCHIVED</option></select></label><label className="wide">Description<textarea name="description" defaultValue={pipeline.description} /></label><div className="form-actions wide"><button className="secondary-button" disabled={busy}>Save details</button></div></form>{versionOpen && <section className="detail-section"><h3>Create or reuse a version</h3><p>Identical configuration reuses the existing version; changed configuration advances the active version.</p><PipelineVersionForm initial={activeVersion} busy={busy} onSubmit={onAddVersion} /></section>}<section className="detail-section"><div className="section-title"><h3>Versions</h3><span>{versions.length}</span></div><div className="release-list">{versions.map((value) => <article key={value.pipeline_version_id}><div><strong>v{value.version} {value.pipeline_version_id === pipeline.active_version_id && <em>CURRENT</em>}</strong><span>{value.input_config.execution_mode === "DIRECT_TEXT" ? "Direct text" : value.model} · {value.splitter_type}</span></div><code>{value.config_hash.slice(0, 12)}</code></article>)}</div></section></div>;
}

function PipelineVersionForm({ initial, busy, onSubmit }: { initial?: PipelineVersion | null; busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form className="admin-form" onSubmit={onSubmit}><label>Execution<select name="execution_mode" defaultValue={String(initial?.input_config.execution_mode ?? "DIRECT_TEXT")}><option value="DIRECT_TEXT">Direct PDF text</option><option value="LLM">LLM extraction</option></select></label><label>Model<input name="model" defaultValue={initial?.model ?? ""} placeholder="Required for LLM" /></label><label>Splitter<select name="splitter_type" defaultValue={initial?.splitter_type ?? "PARAGRAPH"}><option>WHOLE</option><option>JSON</option><option>PARAGRAPH</option><option>MARKDOWN</option><option>ADVANCED</option></select></label><label>Splitter config<textarea name="splitter_config" defaultValue={JSON.stringify(initial?.splitter_config ?? { chunk_size: 500 }, null, 2)} /></label><label className="wide">System prompt<textarea className="prompt-box" name="system_prompt" defaultValue={initial?.system_prompt ?? ""} /></label><label className="wide">User prompt<textarea className="prompt-box" name="user_prompt" defaultValue={initial?.user_prompt ?? ""} /></label><label className="wide">Model config<textarea name="model_config" defaultValue={JSON.stringify(initial?.model_config ?? {}, null, 2)} /></label><div className="form-actions wide"><button className="primary-button" disabled={busy}>Activate version</button></div></form>;
}

function PipelineCreateForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <form className="admin-form" onSubmit={onSubmit}><label>Name<input name="name" required /></label><label className="wide">Description<textarea name="description" /></label><div className="wide"><PipelineVersionFormFields /></div><div className="form-actions wide"><button className="primary-button" disabled={busy}>Create Pipeline</button></div></form>; }
function PipelineVersionFormFields() { return <div className="admin-form nested"><label>Execution<select name="execution_mode" defaultValue="DIRECT_TEXT"><option value="DIRECT_TEXT">Direct PDF text</option><option value="LLM">LLM extraction</option></select></label><label>Model<input name="model" /></label><label>Splitter<select name="splitter_type" defaultValue="PARAGRAPH"><option>WHOLE</option><option>JSON</option><option>PARAGRAPH</option><option>MARKDOWN</option><option>ADVANCED</option></select></label><label>Splitter config<textarea name="splitter_config" defaultValue={'{\n  "chunk_size": 500\n}'} /></label><label className="wide">System prompt<textarea className="prompt-box" name="system_prompt" /></label><label className="wide">User prompt<textarea className="prompt-box" name="user_prompt" /></label><label className="wide">Model config<textarea name="model_config" defaultValue="{}" /></label></div>; }

function DatabaseCreateForm({ pipelines, busy, onSubmit }: { pipelines: DocumentPipeline[]; busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <form className="admin-form" onSubmit={onSubmit}><label>Name<input name="name" required /></label><label>Pipeline<select name="pipeline_id" required>{pipelines.filter((value) => value.status === "ACTIVE").map((value) => <option key={value.pipeline_id} value={value.pipeline_id}>{value.name}</option>)}</select></label><label>Range<select name="range_mode" defaultValue="EXPLICIT"><option>EXPLICIT</option><option>ALL_VERIFIED</option></select></label><label className="wide">Description<textarea name="description" /></label><label className="wide">Embedding profile<textarea name="embedding_profile" defaultValue="{}" /></label><label className="wide">BM25 profile<textarea name="bm25_profile" defaultValue="{}" /></label><div className="form-actions wide"><button className="primary-button" disabled={busy || pipelines.length === 0}>Create database</button></div></form>; }

function DatabaseDetail({ database, pipeline, scope, releases, runs, busy, onSave, onSaveScope, onPolicy, onRun }: { database: DocumentDatabase; pipeline?: DocumentPipeline; scope: DocumentScope | null; releases: DocumentRelease[]; runs: DocumentBuildRun[]; busy: boolean; onSave: (event: FormEvent<HTMLFormElement>) => void; onSaveScope: (event: FormEvent<HTMLFormElement>) => void; onPolicy: (enabled: boolean) => void; onRun: (mode: "FULL" | "UPDATE") => void }) {
  return <div className="admin-detail-stack"><header className="detail-heading"><div><span className="record-kind">DOCUMENT DATABASE</span><h2>{database.name}</h2><p>{pipeline?.name ?? database.pipeline_id}</p></div><span className={`state-badge ${database.retrieval_status.toLowerCase()}`}>{database.retrieval_status}</span></header><div className="database-metrics"><span><small>Current release</small><strong>{database.current_release_id?.slice(0, 8) ?? "None"}</strong></span><span><small>Building</small><strong>{database.building_release_id?.slice(0, 8) ?? "Idle"}</strong></span><span><small>Range revision</small><strong>{database.range_revision}</strong></span><span><small>Resolved papers</small><strong>{scope?.canonical_paper_ids.length ?? "—"}</strong></span></div><form className="admin-form" key={`${database.database_id}:${database.updated_at}`} onSubmit={onSave}><label>Name<input name="name" defaultValue={database.name} required /></label><label>Status<select name="status" defaultValue={database.status}><option>ACTIVE</option><option>ARCHIVED</option></select></label><label>Range mode<select name="range_mode" defaultValue={database.range_mode}><option>EXPLICIT</option><option>ALL_VERIFIED</option></select></label><label className="wide">Description<textarea name="description" defaultValue={database.description} /></label><label className="wide">Embedding profile<textarea name="embedding_profile" defaultValue={JSON.stringify(database.embedding_profile, null, 2)} /></label><label className="wide">BM25 profile<textarea name="bm25_profile" defaultValue={JSON.stringify(database.bm25_profile, null, 2)} /></label><div className="form-actions wide"><label className="check-control"><input type="checkbox" checked={database.auto_reconcile_enabled} onChange={(event) => onPolicy(event.target.checked)} /> Automatic reconcile</label><button className="secondary-button" disabled={busy}>Save configuration</button></div></form>{database.range_mode === "EXPLICIT" && <section className="detail-section"><h3>Explicit paper scope</h3><p>One canonical paper UUID per line. Saving a changed scope advances the range revision.</p><form onSubmit={onSaveScope}><textarea className="scope-editor" name="paper_ids" key={scope?.range_revision} defaultValue={scope?.explicit_canonical_paper_ids.join("\n") ?? ""} /><div className="form-actions"><button className="secondary-button" disabled={busy}>Save scope</button></div></form></section>}<section className="build-controls"><div><h3>Reconcile published corpus</h3><p>UPDATE reuses unchanged Documents. FULL regenerates the target corpus.</p></div><button disabled={busy || !!database.building_release_id} onClick={() => onRun("UPDATE")}>Run update</button><button className="danger-outline" disabled={busy || !!database.building_release_id} onClick={() => onRun("FULL")}>Run full build</button></section><section className="detail-section"><div className="section-title"><h3>Releases</h3><span>{releases.length}</span></div><div className="release-list">{releases.map((value) => <article key={value.release_id}><div><strong>Release {value.release_number} <em>{value.status}</em></strong><span>{value.completed_count}/{value.expected_count} complete · {value.failed_count} failed · retrieval {value.retrieval_status}</span></div><time>{shortTime(value.published_at ?? value.created_at)}</time></article>)}</div></section><section className="detail-section"><div className="section-title"><h3>Recent runs</h3><span>{runs.length}</span></div><div className="release-list">{runs.slice(0, 8).map((value) => <article key={value.run_id}><div><strong>{value.build_mode} <em>{value.status}</em></strong><span>{value.phase} · {value.trigger_reason}</span></div><time>{shortTime(value.created_at)}</time></article>)}</div></section></div>;
}

function RunDetail({ run, tasks, busy, onCancel, onRetry }: { run: DocumentBuildRun; tasks: DocumentBuildTask[]; busy: boolean; onCancel: () => void; onRetry: () => void }) { return <div className="admin-detail-stack"><header className="detail-heading"><div><span className="record-kind">BUILD RUN</span><h2>{run.build_mode} · {run.phase}</h2><p>{run.run_id}</p></div><span className={`state-badge ${run.status.toLowerCase()}`}>{run.status}</span></header><div className="database-metrics"><span><small>Range revision</small><strong>{run.range_revision}</strong></span><span><small>Release</small><strong>{run.release_id?.slice(0, 8) ?? "Pending"}</strong></span><span><small>Created</small><strong>{shortTime(run.created_at)}</strong></span><span><small>Finished</small><strong>{shortTime(run.finished_at)}</strong></span></div><div className="run-actions">{run.status === "RUNNING" && <button className="danger-outline" disabled={busy} onClick={onCancel}>Cancel run</button>}{["FAILED", "CANCELLED"].includes(run.status) && <button className="primary-button" disabled={busy} onClick={onRetry}>Retry run</button>}</div><section className="detail-section"><div className="section-title"><h3>Tasks</h3><span>{tasks.length}</span></div><div className="task-list">{tasks.map((task) => <article key={task.task_id}><div><strong>{task.task_type}</strong><span>{task.progress_message || task.subject_key}</span></div><div><span className={`state-badge ${task.status.toLowerCase()}`}>{task.status}</span><progress max={Math.max(1, task.progress_total)} value={task.progress_current} /></div></article>)}</div></section>{run.error && <pre className="error-block">{JSON.stringify(run.error, null, 2)}</pre>}</div>; }
