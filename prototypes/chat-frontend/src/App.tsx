import {
  Activity,
  Bot,
  Boxes,
  Check,
  ChevronRight,
  CircleAlert,
  CircleStop,
  Copy,
  Database,
  GitBranch,
  GitFork,
  LoaderCircle,
  LogOut,
  MessageSquare,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Send,
  UserRound,
  Wrench,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import ReactMarkdown from "react-markdown";

import { api, getPrincipalId, setPrincipalId, streamTurnEvents } from "./api";
import {
  ACTIVE_TURN_STATUSES,
  type ChatBranch,
  type ChatGraph,
  type ChatSession,
  type ConversationUnit,
  type ToolExecution,
  type TurnEvent,
  type TurnRun,
} from "./types";

const TERMINAL_EVENTS = new Set(["turn.completed", "turn.failed", "turn.interrupted"]);

export function App() {
  const [principalId, setIdentity] = useState(getPrincipalId);
  if (!principalId) return <IdentityGate onConnect={setIdentity} />;
  return <ChatApplication principalId={principalId} onDisconnect={() => setIdentity("")} />;
}

function IdentityGate({ onConnect }: { onConnect: (value: string) => void }) {
  const [value, setValue] = useState("");
  const valid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.trim());
  return (
    <main className="identity-shell">
      <section className="identity-card">
        <div className="brand-mark"><MessageSquare size={18} /></div>
        <span className="eyebrow">CHAT SERVICE · DEVELOPMENT ACCESS</span>
        <h1>Enter the conversation workspace.</h1>
        <p>This first frontend uses the backend's temporary principal header. Formal identity will replace this gate when OIDC is connected.</p>
        <label>
          <span>Principal UUID</span>
          <input autoFocus value={value} onChange={(event) => setValue(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" />
        </label>
        <button className="primary-button" disabled={!valid} onClick={() => { const next = value.trim(); setPrincipalId(next); onConnect(next); }}>
          Open Chat Workspace <ChevronRight size={15} />
        </button>
        <small>Development only · stored in this browser</small>
      </section>
    </main>
  );
}

function ChatApplication({ principalId, onDisconnect }: { principalId: string; onDisconnect: () => void }) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState(() => sessionIdFromPath());
  const [graph, setGraph] = useState<ChatGraph | null>(null);
  const [selectedBranchId, setSelectedBranchId] = useState("");
  const [contextUnitId, setContextUnitId] = useState("");
  const [partialText, setPartialText] = useState("");
  const [liveStage, setLiveStage] = useState("");
  const [tools, setTools] = useState<ToolExecution[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");

  const loadSessions = useCallback(async () => {
    const response = await api.listSessions();
    setSessions(response.sessions);
    setSelectedSessionId((current) =>
      response.sessions.some((session) => session.session_id === current)
        ? current
        : response.sessions[0]?.session_id || "",
    );
  }, []);

  const loadGraph = useCallback(async (sessionId: string) => {
    const value = await api.graph(sessionId);
    setGraph(value);
    setSelectedBranchId((current) => value.branches.some((branch) => branch.branch_id === current)
      ? current
      : value.branches.at(-1)?.branch_id ?? "");
    return value;
  }, []);

  useEffect(() => {
    void (async () => {
      setLoading(true);
      try {
        await loadSessions();
      } catch (caught) {
        setError(errorMessage(caught));
      } finally {
        setLoading(false);
      }
    })();
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedSessionId) { setGraph(null); return; }
    window.history.replaceState({}, "", `/sessions/${selectedSessionId}`);
    setContextUnitId("");
    setPartialText("");
    setTools([]);
    void loadGraph(selectedSessionId).catch((caught) => setError(errorMessage(caught)));
  }, [loadGraph, selectedSessionId]);

  const activeTurn = useMemo(
    () => [...(graph?.turns ?? [])].reverse().find((turn) => ACTIVE_TURN_STATUSES.has(turn.status)) ?? null,
    [graph?.turns],
  );
  const selectedBranch = graph?.branches.find((branch) => branch.branch_id === selectedBranchId) ?? graph?.branches.at(-1) ?? null;
  const leafId = selectedBranch?.head_unit_id ?? "";
  const chain = useMemo(() => graph ? branchChain(graph.units, leafId) : [], [graph, leafId]);
  const inspectedTurn = useMemo(() => {
    if (activeTurn) return activeTurn;
    return [...(graph?.turns ?? [])].reverse().find((turn) => turn.branch_id === selectedBranch?.branch_id) ?? graph?.turns.at(-1) ?? null;
  }, [activeTurn, graph?.turns, selectedBranch?.branch_id]);

  const refreshTools = useCallback(async (turnId: string) => {
    const response = await api.tools(turnId);
    setTools(response.tool_executions);
  }, []);

  useEffect(() => {
    if (!inspectedTurn) { setTools([]); return; }
    void refreshTools(inspectedTurn.turn_id).catch(() => undefined);
  }, [inspectedTurn?.turn_id, refreshTools]);

  useEffect(() => {
    if (!activeTurn || !selectedSessionId) return;
    const turn = activeTurn;
    const controller = new AbortController();
    let terminal = false;
    let cursor = 0;
    setPartialText("");
    setLiveStage(stageLabel(turn.status));
    async function followTurn() {
      while (!controller.signal.aborted && !terminal) {
        cursor = await streamTurnEvents(turn.turn_id, cursor, controller.signal, (event) => {
          handleLiveEvent(event, setPartialText, setLiveStage);
          if (event.type.startsWith("tool.") || event.type === "turn.tool_budget_exhausted") {
            void refreshTools(turn.turn_id);
          }
          if (TERMINAL_EVENTS.has(event.type)) terminal = true;
        });
        if (controller.signal.aborted || terminal) break;
        const latest = (await api.turn(turn.turn_id)).turn;
        terminal = !ACTIVE_TURN_STATUSES.has(latest.status);
        if (!terminal) await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
      if (!controller.signal.aborted) {
        await Promise.all([
          loadGraph(selectedSessionId),
          loadSessions(),
          refreshTools(turn.turn_id),
        ]);
      }
    }
    void followTurn().catch((caught) => {
      if (!controller.signal.aborted) setError(errorMessage(caught));
    });
    return () => controller.abort();
  }, [activeTurn?.turn_id, loadGraph, loadSessions, refreshTools, selectedSessionId]);

  async function createSession() {
    if (!newTitle.trim()) return;
    setError("");
    try {
      const response = await api.createSession(newTitle.trim());
      setNewTitle("");
      setCreating(false);
      await loadSessions();
      setSelectedSessionId(response.session.session_id);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  async function beginTurn(action: () => Promise<{ turn: TurnRun }>) {
    setError("");
    setPartialText("");
    try {
      const response = await action();
      setSelectedBranchId(response.turn.branch_id);
      await Promise.all([loadGraph(response.turn.session_id), loadSessions()]);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  function disconnect() {
    localStorage.removeItem("chat-v2-principal-id");
    window.history.replaceState({}, "", "/");
    onDisconnect();
  }

  if (loading) return <div className="centered-state"><LoaderCircle className="spin" size={22} /><span>Opening Chat service</span></div>;

  return (
    <main className="chat-shell">
      <header className="topbar">
        <div className="service-brand">
          <div className="brand-mark"><MessageSquare size={16} /></div>
          <div><strong>Chat Workspace</strong><span>Independent conversation service</span></div>
        </div>
        <div className="topbar-meta">
          <span className="service-state"><i /> API connected</span>
          <span className="principal-label">{shortId(principalId)}</span>
          <button className="icon-button" title="Change development identity" onClick={disconnect}><LogOut size={15} /></button>
        </div>
      </header>

      <section className="workspace-grid">
        <aside className="session-panel panel">
          <header className="panel-header"><div><span className="eyebrow">CONVERSATIONS</span><strong>Sessions</strong></div><button className="icon-button accent" title="New session" onClick={() => setCreating(true)}><Plus size={16} /></button></header>
          {creating && <div className="session-create"><input autoFocus value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="Session title" onKeyDown={(event) => { if (event.key === "Enter") void createSession(); }} /><button title="Create" disabled={!newTitle.trim()} onClick={() => void createSession()}><Check size={14} /></button><button title="Cancel" onClick={() => setCreating(false)}><X size={14} /></button></div>}
          <nav className="session-list">
            {sessions.map((session) => {
              const selected = session.session_id === selectedSessionId;
              return <button className={`session-row ${selected ? "selected" : ""}`} key={session.session_id} onClick={() => setSelectedSessionId(session.session_id)}><MessageSquare size={14} /><span><strong>{session.title}</strong><small>rev {session.revision} · {relativeTime(session.updated_at)}</small></span>{selected && <ChevronRight size={13} />}</button>;
            })}
            {!sessions.length && <div className="empty-list"><MessageSquare size={22} /><span>No conversations yet</span><button onClick={() => setCreating(true)}>Create the first session</button></div>}
          </nav>
          <footer className="session-footer"><span>Principal</span><code>{shortId(principalId)}</code></footer>
        </aside>

        <section className="conversation-panel panel">
          <header className="panel-header conversation-header">
            <div><span className="eyebrow">ACTIVE THREAD</span><strong>{graph?.session.title ?? "Select a session"}</strong></div>
            {graph && <div className="revision-chip">revision {graph.session.revision}</div>}
          </header>
          {error && <button className="error-banner" onClick={() => setError("")}><CircleAlert size={14} /><span>{error}</span><X size={13} /></button>}
          <Conversation
            chain={chain}
            partialText={partialText}
            activeTurn={activeTurn}
            liveStage={liveStage}
            contextUnitId={contextUnitId}
            disabled={!graph || Boolean(activeTurn)}
            onContext={setContextUnitId}
            onEdit={(unit, content) => graph && beginTurn(() => api.editAndRegenerate(unit.unit_id, content, graph.session.revision, 10))}
            onRegenerate={(unit) => graph && beginTurn(() => api.regenerate(unit.unit_id, graph.session.revision, 10))}
            onInterrupt={() => activeTurn && api.interrupt(activeTurn.turn_id).then(() => loadGraph(activeTurn.session_id)).catch((caught) => setError(errorMessage(caught)))}
          />
          <Composer
            disabled={!graph || Boolean(activeTurn)}
            waiting={activeTurn?.status === "WAITING"}
            context={contextUnitId ? graph?.units.find((unit) => unit.unit_id === contextUnitId) ?? null : null}
            onClearContext={() => setContextUnitId("")}
            onSend={(content, maxToolCalls) => {
              if (!graph) return;
              return beginTurn(() => api.createTurn(graph.session.session_id, {
                content,
                branch_id: selectedBranch?.branch_id ?? graph.session.default_branch_id,
                parent_unit_id: contextUnitId || leafId || null,
                base_revision: graph.session.revision,
                max_tool_calls: maxToolCalls,
              })).then(() => setContextUnitId(""));
            }}
          />
        </section>

        <aside className="activity-panel panel">
          <header className="panel-header"><div><span className="eyebrow">EXECUTION</span><strong>Turn activity</strong></div><Activity size={16} /></header>
          <TurnSummary turn={inspectedTurn} liveStage={liveStage} />
          <ToolTimeline tools={tools} />
          <BranchNavigator branches={graph?.branches ?? []} selectedId={selectedBranch?.branch_id ?? ""} units={graph?.units ?? []} onSelect={(branch) => { setSelectedBranchId(branch.branch_id); setContextUnitId(""); }} />
        </aside>
      </section>
    </main>
  );
}

function Conversation({ chain, partialText, activeTurn, liveStage, contextUnitId, disabled, onContext, onEdit, onRegenerate, onInterrupt }: {
  chain: ConversationUnit[];
  partialText: string;
  activeTurn: TurnRun | null;
  liveStage: string;
  contextUnitId: string;
  disabled: boolean;
  onContext: (unitId: string) => void;
  onEdit: (unit: ConversationUnit, content: string) => void;
  onRegenerate: (unit: ConversationUnit) => void;
  onInterrupt: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [chain.length, partialText]);
  return <div className="conversation-scroll" ref={scrollRef}>
    {!chain.length && !activeTurn && <div className="conversation-empty"><div><Bot size={25} /></div><h2>Begin a considered conversation.</h2><p>Ask a question directly, or let the model organize research with the available tools.</p></div>}
    {chain.map((unit) => <MessageUnit key={unit.unit_id} unit={unit} selectedContext={contextUnitId === unit.unit_id} disabled={disabled} onContext={() => onContext(unit.unit_id)} onEdit={(content) => onEdit(unit, content)} onRegenerate={() => onRegenerate(unit)} />)}
    {activeTurn && <article className="message-row assistant live-message"><div className="avatar"><Bot size={15} /></div><div className="message-body">{partialText ? <div className="markdown"><ReactMarkdown>{partialText}</ReactMarkdown><span className="stream-caret" /></div> : <div className="thinking"><LoaderCircle className="spin" size={14} /><span>{liveStage || stageLabel(activeTurn.status)}</span></div>}<button className="interrupt-button" onClick={onInterrupt}><CircleStop size={13} /> Interrupt</button></div></article>}
  </div>;
}

function MessageUnit({ unit, selectedContext, disabled, onContext, onEdit, onRegenerate }: {
  unit: ConversationUnit;
  selectedContext: boolean;
  disabled: boolean;
  onContext: () => void;
  onEdit: (content: string) => void;
  onRegenerate: () => void;
}) {
  const user = unit.unit_type === "USER_INPUT";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(unit.display_text);
  return <article className={`message-row ${user ? "user" : "assistant"} ${selectedContext ? "context-selected" : ""}`}>
    <div className="avatar">{user ? <UserRound size={15} /> : <Bot size={15} />}</div>
    <div className="message-body">
      {editing ? <div className="message-editor"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={5} /><div><button onClick={() => { setEditing(false); setDraft(unit.display_text); }}><X size={13} /> Cancel</button><button disabled={!draft.trim()} onClick={() => { setEditing(false); onEdit(draft.trim()); }}><Check size={13} /> Send edit</button></div></div> : user ? <div className="user-copy">{unit.display_text}</div> : <div className="markdown"><ReactMarkdown>{unit.display_text || "_No display text returned._"}</ReactMarkdown></div>}
      {unit.interrupted && <span className="interrupted-note"><CircleStop size={11} /> Interrupted response retained</span>}
      {!editing && <div className="message-actions"><button title="Copy" onClick={() => void navigator.clipboard.writeText(unit.display_text)}><Copy size={12} /></button>{user && <button title="Edit and regenerate" disabled={disabled} onClick={() => setEditing(true)}><Pencil size={12} /></button>}<button title="Regenerate from here" disabled={disabled} onClick={onRegenerate}><RefreshCw size={12} /></button><button className={selectedContext ? "active" : ""} title="Continue from this unit" disabled={disabled} onClick={onContext}><GitFork size={12} /> Continue here</button></div>}
    </div>
  </article>;
}

function Composer({ disabled, waiting, context, onClearContext, onSend }: { disabled: boolean; waiting: boolean; context: ConversationUnit | null; onClearContext: () => void; onSend: (content: string, maxToolCalls: number) => Promise<void> | void }) {
  const [draft, setDraft] = useState("");
  const [budget, setBudget] = useState(10);
  async function submit() { const content = draft.trim(); if (!content || disabled) return; setDraft(""); await onSend(content, budget); }
  return <div className="composer-wrap">
    {context && <div className="context-chip"><GitFork size={12} /><span>Continue from {context.unit_type === "USER_INPUT" ? "your message" : "assistant reply"}: {truncate(context.display_text, 68)}</span><button onClick={onClearContext}><X size={12} /></button></div>}
    {waiting && <div className="queue-note"><LoaderCircle className="spin" size={12} /> Busy line · this turn is waiting for execution capacity</div>}
    <div className="composer"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} disabled={disabled} rows={3} placeholder={disabled ? "Wait for the current turn to settle" : "Message Chat Workspace"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} /><div className="composer-footer"><label title="Maximum tool executions for this turn"><Wrench size={12} /><span>Tool budget</span><input type="number" min={0} max={100} value={budget} onChange={(event) => setBudget(Math.max(0, Math.min(100, Number(event.target.value))))} /></label><span>Enter to send · Shift+Enter for newline</span><button className="send-button" title="Send" disabled={disabled || !draft.trim()} onClick={() => void submit()}><Send size={15} /></button></div></div>
  </div>;
}

function TurnSummary({ turn, liveStage }: { turn: TurnRun | null; liveStage: string }) {
  if (!turn) return <div className="activity-empty"><Activity size={19} /><span>No turns in this branch</span></div>;
  return <section className="turn-summary"><div className="turn-status-line"><span className={`status-dot ${turn.status.toLowerCase()}`} /><div><strong>{stageLabel(turn.status)}</strong><small>{liveStage && ACTIVE_TURN_STATUSES.has(turn.status) ? liveStage : turn.completion_reason || relativeTime(turn.created_at)}</small></div></div><dl><div><dt>Tools</dt><dd>{turn.used_tool_calls} / {turn.max_tool_calls}</dd></div><div><dt>Turn</dt><dd>{shortId(turn.turn_id)}</dd></div></dl>{turn.error && <div className="turn-error">{turn.error}</div>}</section>;
}

function ToolTimeline({ tools }: { tools: ToolExecution[] }) {
  return <section className="tool-section"><header><span>TOOL EXECUTIONS</span><small>{tools.length}</small></header>{!tools.length && <div className="tool-empty">Model activity will appear here when a tool is called.</div>}<div className="tool-list">{tools.map((tool, index) => <ToolCard key={tool.execution_id} tool={tool} index={index + 1} />)}</div></section>;
}

function ToolCard({ tool, index }: { tool: ToolExecution; index: number }) {
  const output = nestedObject(tool.result, "output");
  const plan = Array.isArray(output?.plan) ? output.plan as Array<Record<string, unknown>> : [];
  const query = typeof tool.arguments.query === "string" ? tool.arguments.query : "";
  const doi = typeof tool.arguments.doi === "string" ? tool.arguments.doi : "";
  const evidence = Array.isArray(output?.global_evidence) ? output.global_evidence.length : null;
  const label = tool.tool_name === "plan_board" ? "Plan board" : tool.tool_name === "document_retrieval" ? "Document retrieval" : tool.tool_name === "document_get_by_doi" ? "Document by DOI" : tool.tool_name;
  const icon = tool.tool_name === "plan_board" ? <Boxes size={13} /> : tool.tool_name === "document_retrieval" ? <Search size={13} /> : <Database size={13} />;
  return <details className="tool-card" open={tool.status === "RUNNING"}><summary><span className="tool-index">{String(index).padStart(2, "0")}</span><span className="tool-icon">{icon}</span><span><strong>{label}</strong><small>{query || doi || tool.status.toLowerCase()}</small></span><em className={tool.status.toLowerCase()}>{tool.status === "RUNNING" && <LoaderCircle className="spin" size={10} />}{tool.status}</em></summary><div className="tool-detail">{plan.length > 0 && <ol className="plan-list">{plan.map((entry, itemIndex) => <li className={String(entry.status)} key={itemIndex}><span>{entry.status === "completed" ? <Check size={11} /> : <i />}</span>{String(entry.step)}</li>)}</ol>}{query && <p><b>Query</b>{query}</p>}{evidence !== null && <p><b>Evidence</b>{evidence} ranked documents</p>}{doi && <p><b>DOI</b>{doi}</p>}{typeof output?.status === "string" && <p><b>Result</b>{output.status}</p>}<pre>{JSON.stringify(tool.status === "FAILED" ? tool.error : tool.arguments, null, 2)}</pre></div></details>;
}

function BranchNavigator({ branches, selectedId, units, onSelect }: { branches: ChatBranch[]; selectedId: string; units: ConversationUnit[]; onSelect: (branch: ChatBranch) => void }) {
  return <section className="branch-section"><header><span>BRANCHES</span><small>{branches.length}</small></header><div className="branch-list">{branches.map((branch, index) => { const head = units.find((unit) => unit.unit_id === branch.head_unit_id); return <button className={branch.branch_id === selectedId ? "selected" : ""} key={branch.branch_id} onClick={() => onSelect(branch)}><GitBranch size={13} /><span><strong>{index === 0 ? "Main" : `Branch ${index + 1}`}</strong><small>{head ? truncate(head.display_text, 42) : "Empty branch"}</small></span></button>; })}</div></section>;
}

function handleLiveEvent(event: TurnEvent, setPartial: Dispatch<SetStateAction<string>>, setStage: (value: string) => void) {
  if (event.type === "response.output_text.delta") setPartial((current) => current + String(event.payload.delta ?? ""));
  if (event.type === "model.step.started") setStage("Model is thinking");
  if (event.type === "tool.execution.started") setStage(`Running ${String(event.payload.tool_name ?? "tool")}`);
  if (event.type === "tool.execution.completed") setStage(`Completed ${String(event.payload.tool_name ?? "tool")}`);
  if (event.type === "turn.tool_budget_exhausted") setStage("Tool budget exhausted · composing final response");
  if (event.type === "turn.interrupt_requested") setStage("Interrupt requested");
}

function branchChain(units: ConversationUnit[], leafId: string) {
  const byId = new Map(units.map((unit) => [unit.unit_id, unit]));
  const chain: ConversationUnit[] = [];
  const seen = new Set<string>();
  let current = byId.get(leafId);
  while (current && !seen.has(current.unit_id)) { seen.add(current.unit_id); chain.push(current); current = current.parent_unit_id ? byId.get(current.parent_unit_id) : undefined; }
  return chain.reverse();
}

function stageLabel(status: TurnRun["status"]) {
  return ({ WAITING: "Waiting for capacity", STARTING: "Starting turn", RUNNING_MODEL: "Model is thinking", RUNNING_TOOLS: "Running tools", INTERRUPT_REQUESTED: "Stopping safely", COMPLETED: "Turn completed", INTERRUPTED_PARTIAL: "Interrupted · partial reply retained", FAILED: "Turn failed" })[status];
}

function nestedObject(value: Record<string, unknown>, key: string) { const result = value[key]; return result && typeof result === "object" && !Array.isArray(result) ? result as Record<string, unknown> : null; }
function shortId(value: string) { return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : "—"; }
function truncate(value: string, length: number) { const clean = value.replaceAll(/\s+/g, " ").trim(); return clean.length > length ? `${clean.slice(0, length)}…` : clean; }
function relativeTime(value: string) { const delta = Date.now() - new Date(value).getTime(); if (!Number.isFinite(delta)) return value; if (delta < 60_000) return "just now"; if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`; if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`; return new Date(value).toLocaleDateString(); }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "The operation failed"; }
function sessionIdFromPath() { return decodeURIComponent(window.location.pathname.match(/^\/sessions\/([^/]+)/)?.[1] ?? ""); }
