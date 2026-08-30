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
  Library,
  LoaderCircle,
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

import type { SessionInfo } from "../../api";
import { chatApi, streamTurnEvents } from "./chatApi";
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
import "./chat.css";

const TERMINAL_EVENTS = new Set(["turn.completed", "turn.failed", "turn.interrupted"]);

export function ChatPage({ session }: { session: SessionInfo }) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState(sessionIdFromPath);
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
    const response = await chatApi.listSessions();
    setSessions(response.sessions);
    setSelectedSessionId((current) =>
      response.sessions.some((value) => value.session_id === current)
        ? current
        : response.sessions[0]?.session_id ?? "",
    );
  }, []);

  const loadGraph = useCallback(async (sessionId: string) => {
    const value = await chatApi.graph(sessionId);
    setGraph(value);
    setSelectedBranchId((current) =>
      value.branches.some((branch) => branch.branch_id === current)
        ? current
        : value.branches.at(-1)?.branch_id ?? "",
    );
    return value;
  }, []);

  useEffect(() => {
    void loadSessions()
      .catch((caught: unknown) => setError(errorMessage(caught)))
      .finally(() => setLoading(false));
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedSessionId) {
      setGraph(null);
      window.history.replaceState({}, "", "/chat");
      return;
    }
    window.history.replaceState({}, "", `/chat/sessions/${selectedSessionId}`);
    setContextUnitId("");
    setPartialText("");
    setTools([]);
    void loadGraph(selectedSessionId).catch((caught: unknown) => setError(errorMessage(caught)));
  }, [loadGraph, selectedSessionId]);

  const activeTurn = useMemo(
    () =>
      [...(graph?.turns ?? [])]
        .reverse()
        .find((turn) => ACTIVE_TURN_STATUSES.has(turn.status)) ?? null,
    [graph?.turns],
  );
  const selectedBranch =
    graph?.branches.find((branch) => branch.branch_id === selectedBranchId) ??
    graph?.branches.at(-1) ??
    null;
  const leafId = selectedBranch?.head_unit_id ?? "";
  const chain = useMemo(
    () => (graph ? branchChain(graph.units, leafId) : []),
    [graph, leafId],
  );
  const inspectedTurn = useMemo(() => {
    if (activeTurn) return activeTurn;
    return (
      [...(graph?.turns ?? [])]
        .reverse()
        .find((turn) => turn.branch_id === selectedBranch?.branch_id) ??
      graph?.turns.at(-1) ??
      null
    );
  }, [activeTurn, graph?.turns, selectedBranch?.branch_id]);

  const refreshTools = useCallback(async (turnId: string) => {
    const response = await chatApi.tools(turnId);
    setTools(response.tool_executions);
  }, []);

  useEffect(() => {
    if (!inspectedTurn) {
      setTools([]);
      return;
    }
    void refreshTools(inspectedTurn.turn_id).catch(() => undefined);
  }, [inspectedTurn, refreshTools]);

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
        const latest = (await chatApi.turn(turn.turn_id)).turn;
        terminal = !ACTIVE_TURN_STATUSES.has(latest.status);
      }
      if (!controller.signal.aborted) {
        await Promise.all([
          loadGraph(selectedSessionId),
          loadSessions(),
          refreshTools(turn.turn_id),
        ]);
      }
    }
    void followTurn().catch((caught: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(caught));
    });
    return () => controller.abort();
  }, [activeTurn, loadGraph, loadSessions, refreshTools, selectedSessionId]);

  async function createSession() {
    if (!newTitle.trim()) return;
    setError("");
    try {
      const response = await chatApi.createSession(newTitle.trim());
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

  if (loading) {
    return (
      <main className="chat-page chat-route-state">
        <LoaderCircle className="chat-spin" size={22} /> Opening Chat workspace…
      </main>
    );
  }

  return (
    <main className="chat-page">
      <header className="chat-topbar">
        <div className="chat-service-brand">
          <span className="chat-brand-mark"><MessageSquare size={15} /></span>
          <div><strong>Literature Workspace</strong><small>Chat</small></div>
        </div>
        <nav>
          <a href="/"><Library size={13} /> Library</a>
          <span className="chat-api-state"><i /> API connected</span>
          <span>{session.principal.display_name}</span>
        </nav>
      </header>

      <section className="chat-grid">
        <aside className="chat-panel chat-sessions">
          <PanelHeader eyebrow="CONVERSATIONS" title="Sessions">
            <button className="chat-icon-button accent" title="New session" onClick={() => setCreating(true)}><Plus size={16} /></button>
          </PanelHeader>
          {creating && (
            <div className="chat-session-create">
              <input autoFocus value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="Session title" onKeyDown={(event) => { if (event.key === "Enter") void createSession(); }} />
              <button disabled={!newTitle.trim()} onClick={() => void createSession()}><Check size={13} /></button>
              <button onClick={() => setCreating(false)}><X size={13} /></button>
            </div>
          )}
          <nav className="chat-session-list">
            {sessions.map((value) => (
              <button className={value.session_id === selectedSessionId ? "selected" : ""} key={value.session_id} onClick={() => setSelectedSessionId(value.session_id)}>
                <MessageSquare size={14} />
                <span><strong>{value.title}</strong><small>rev {value.revision} · {relativeTime(value.updated_at)}</small></span>
                {value.session_id === selectedSessionId && <ChevronRight size={13} />}
              </button>
            ))}
            {!sessions.length && (
              <div className="chat-empty-list"><MessageSquare size={22} /><span>No conversations yet</span><button onClick={() => setCreating(true)}>Create the first session</button></div>
            )}
          </nav>
        </aside>

        <section className="chat-panel chat-conversation">
          <PanelHeader eyebrow="ACTIVE THREAD" title={graph?.session.title ?? "Select a session"}>
            {graph && <code>revision {graph.session.revision}</code>}
          </PanelHeader>
          {error && <button className="chat-error" onClick={() => setError("")}><CircleAlert size={14} /><span>{error}</span><X size={13} /></button>}
          <Conversation
            chain={chain}
            partialText={partialText}
            activeTurn={activeTurn}
            liveStage={liveStage}
            contextUnitId={contextUnitId}
            disabled={!graph || Boolean(activeTurn)}
            onContext={setContextUnitId}
            onEdit={(unit, content) => graph && beginTurn(() => chatApi.editAndRegenerate(unit.unit_id, content, graph.session.revision, 10))}
            onRegenerate={(unit) => graph && beginTurn(() => chatApi.regenerate(unit.unit_id, graph.session.revision, 10))}
            onInterrupt={() => activeTurn && chatApi.interrupt(activeTurn.turn_id).then(() => loadGraph(activeTurn.session_id)).catch((caught: unknown) => setError(errorMessage(caught)))}
          />
          <Composer
            disabled={!graph || Boolean(activeTurn)}
            waiting={activeTurn?.status === "WAITING"}
            context={contextUnitId ? graph?.units.find((unit) => unit.unit_id === contextUnitId) ?? null : null}
            onClearContext={() => setContextUnitId("")}
            onSend={(content, maxToolCalls) => {
              if (!graph) return;
              return beginTurn(() => chatApi.createTurn(graph.session.session_id, {
                content,
                branch_id: selectedBranch?.branch_id ?? graph.session.default_branch_id,
                parent_unit_id: contextUnitId || leafId || null,
                base_revision: graph.session.revision,
                max_tool_calls: maxToolCalls,
              })).then(() => setContextUnitId(""));
            }}
          />
        </section>

        <aside className="chat-panel chat-activity">
          <PanelHeader eyebrow="EXECUTION" title="Turn activity"><Activity size={16} /></PanelHeader>
          <TurnSummary turn={inspectedTurn} liveStage={liveStage} />
          <ToolTimeline tools={tools} />
          <BranchNavigator branches={graph?.branches ?? []} selectedId={selectedBranch?.branch_id ?? ""} units={graph?.units ?? []} onSelect={(branch) => { setSelectedBranchId(branch.branch_id); setContextUnitId(""); }} />
        </aside>
      </section>
    </main>
  );
}

function PanelHeader({ eyebrow, title, children }: { eyebrow: string; title: string; children?: React.ReactNode }) {
  return <header className="chat-panel-header"><div><span>{eyebrow}</span><strong>{title}</strong></div>{children}</header>;
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
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [chain.length, partialText]);
  return (
    <div className="chat-scroll" ref={scrollRef}>
      {!chain.length && !activeTurn && <div className="chat-conversation-empty"><Bot size={26} /><h2>Begin a considered conversation.</h2><p>Ask directly, or let the model organize research with the available tools.</p></div>}
      {chain.map((unit) => <MessageUnit key={unit.unit_id} unit={unit} selectedContext={contextUnitId === unit.unit_id} disabled={disabled} onContext={() => onContext(unit.unit_id)} onEdit={(content) => onEdit(unit, content)} onRegenerate={() => onRegenerate(unit)} />)}
      {activeTurn && (
        <article className="chat-message assistant live">
          <Avatar user={false} />
          <div className="chat-message-body">
            {partialText ? <div className="chat-markdown"><ReactMarkdown>{partialText}</ReactMarkdown><span className="chat-stream-caret" /></div> : <div className="chat-thinking"><LoaderCircle className="chat-spin" size={14} />{liveStage || stageLabel(activeTurn.status)}</div>}
            <button className="chat-interrupt" onClick={onInterrupt}><CircleStop size={13} /> Interrupt</button>
          </div>
        </article>
      )}
    </div>
  );
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
  return (
    <article className={`chat-message ${user ? "user" : "assistant"} ${selectedContext ? "context" : ""}`}>
      <Avatar user={user} />
      <div className="chat-message-body">
        {editing ? (
          <div className="chat-message-editor">
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={5} />
            <div><button onClick={() => { setEditing(false); setDraft(unit.display_text); }}><X size={12} /> Cancel</button><button disabled={!draft.trim()} onClick={() => { setEditing(false); onEdit(draft.trim()); }}><Check size={12} /> Send edit</button></div>
          </div>
        ) : user ? <div className="chat-user-copy">{unit.display_text}</div> : <div className="chat-markdown"><ReactMarkdown>{unit.display_text || "_No display text returned._"}</ReactMarkdown></div>}
        {unit.interrupted && <span className="chat-interrupted"><CircleStop size={11} /> Interrupted response retained</span>}
        {!editing && (
          <div className="chat-message-actions">
            <button title="Copy" onClick={() => void navigator.clipboard.writeText(unit.display_text)}><Copy size={12} /></button>
            {user && <button title="Edit and regenerate" disabled={disabled} onClick={() => setEditing(true)}><Pencil size={12} /></button>}
            <button title="Regenerate from here" disabled={disabled} onClick={onRegenerate}><RefreshCw size={12} /></button>
            <button className={selectedContext ? "active" : ""} title="Continue from this unit" disabled={disabled} onClick={onContext}><GitFork size={12} /> Continue here</button>
          </div>
        )}
      </div>
    </article>
  );
}

function Avatar({ user }: { user: boolean }) {
  return <div className={`chat-avatar ${user ? "user" : ""}`}>{user ? <UserRound size={15} /> : <Bot size={15} />}</div>;
}

function Composer({ disabled, waiting, context, onClearContext, onSend }: {
  disabled: boolean;
  waiting: boolean;
  context: ConversationUnit | null;
  onClearContext: () => void;
  onSend: (content: string, maxToolCalls: number) => Promise<void> | void;
}) {
  const [draft, setDraft] = useState("");
  const [budget, setBudget] = useState(10);
  async function submit() {
    const content = draft.trim();
    if (!content || disabled) return;
    setDraft("");
    await onSend(content, budget);
  }
  return (
    <div className="chat-composer-wrap">
      {context && <div className="chat-context-chip"><GitFork size={12} /><span>Continue from {context.unit_type === "USER_INPUT" ? "your message" : "assistant reply"}: {truncate(context.display_text, 68)}</span><button onClick={onClearContext}><X size={12} /></button></div>}
      {waiting && <div className="chat-queue-note"><LoaderCircle className="chat-spin" size={12} /> Busy line · waiting for execution capacity</div>}
      <div className="chat-composer">
        <textarea value={draft} onChange={(event) => setDraft(event.target.value)} disabled={disabled} rows={3} placeholder={disabled ? "Wait for the current turn to settle" : "Message Chat"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} />
        <div><label title="Maximum tool executions"><Wrench size={12} /> Tool budget <input type="number" min={0} max={100} value={budget} onChange={(event) => setBudget(Math.max(0, Math.min(100, Number(event.target.value))))} /></label><span>Enter to send · Shift+Enter for newline</span><button className="chat-send" disabled={disabled || !draft.trim()} onClick={() => void submit()}><Send size={15} /></button></div>
      </div>
    </div>
  );
}

function TurnSummary({ turn, liveStage }: { turn: TurnRun | null; liveStage: string }) {
  if (!turn) return <div className="chat-activity-empty"><Activity size={18} /> No turns in this branch</div>;
  return (
    <section className="chat-turn-summary">
      <div><span className={`chat-status ${turn.status.toLowerCase()}`} /><p><strong>{stageLabel(turn.status)}</strong><small>{liveStage && ACTIVE_TURN_STATUSES.has(turn.status) ? liveStage : turn.completion_reason || relativeTime(turn.created_at)}</small></p></div>
      <dl><div><dt>Tools</dt><dd>{turn.used_tool_calls} / {turn.max_tool_calls}</dd></div><div><dt>Turn</dt><dd>{shortId(turn.turn_id)}</dd></div></dl>
      {turn.error && <div className="chat-turn-error">{turn.error}</div>}
    </section>
  );
}

function ToolTimeline({ tools }: { tools: ToolExecution[] }) {
  return (
    <section className="chat-tools">
      <header><span>TOOL EXECUTIONS</span><small>{tools.length}</small></header>
      {!tools.length && <p className="chat-tool-empty">Model activity appears here when a tool is called.</p>}
      <div className="chat-tool-list">{tools.map((tool, index) => <ToolCard key={tool.execution_id} tool={tool} index={index + 1} />)}</div>
    </section>
  );
}

function ToolCard({ tool, index }: { tool: ToolExecution; index: number }) {
  const output = nestedObject(tool.result, "output");
  const plan = Array.isArray(output?.plan) ? output.plan as Array<Record<string, unknown>> : [];
  const query = typeof tool.arguments.query === "string" ? tool.arguments.query : "";
  const doi = typeof tool.arguments.doi === "string" ? tool.arguments.doi : "";
  const label = tool.tool_name === "plan_board" ? "Plan board" : tool.tool_name === "document_retrieval" ? "Document retrieval" : tool.tool_name === "document_get_by_doi" ? "Document by DOI" : tool.tool_name;
  const icon = tool.tool_name === "plan_board" ? <Boxes size={13} /> : tool.tool_name === "document_retrieval" ? <Search size={13} /> : <Database size={13} />;
  return (
    <details className="chat-tool-card" open={tool.status === "RUNNING"}>
      <summary><code>{String(index).padStart(2, "0")}</code><i>{icon}</i><span><strong>{label}</strong><small>{query || doi || tool.status.toLowerCase()}</small></span><em className={tool.status.toLowerCase()}>{tool.status}</em></summary>
      <div className="chat-tool-detail">
        {plan.length > 0 && <ol>{plan.map((entry, itemIndex) => <li key={itemIndex}><span>{entry.status === "completed" ? <Check size={10} /> : "·"}</span>{String(entry.step)}</li>)}</ol>}
        {query && <p><b>Query</b>{query}</p>}{doi && <p><b>DOI</b>{doi}</p>}
        {typeof output?.status === "string" && <p><b>Result</b>{output.status}</p>}
        <pre>{JSON.stringify(tool.status === "FAILED" ? tool.error : tool.arguments, null, 2)}</pre>
      </div>
    </details>
  );
}

function BranchNavigator({ branches, selectedId, units, onSelect }: {
  branches: ChatBranch[];
  selectedId: string;
  units: ConversationUnit[];
  onSelect: (branch: ChatBranch) => void;
}) {
  return (
    <section className="chat-branches">
      <header><span>BRANCHES</span><small>{branches.length}</small></header>
      <div>{branches.map((branch, index) => { const head = units.find((unit) => unit.unit_id === branch.head_unit_id); return <button className={branch.branch_id === selectedId ? "selected" : ""} key={branch.branch_id} onClick={() => onSelect(branch)}><GitBranch size={13} /><span><strong>{index === 0 ? "Main" : `Branch ${index + 1}`}</strong><small>{head ? truncate(head.display_text, 42) : "Empty branch"}</small></span></button>; })}</div>
    </section>
  );
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
  while (current && !seen.has(current.unit_id)) {
    seen.add(current.unit_id);
    chain.push(current);
    current = current.parent_unit_id ? byId.get(current.parent_unit_id) : undefined;
  }
  return chain.reverse();
}

function stageLabel(status: TurnRun["status"]) {
  return ({ WAITING: "Waiting for capacity", STARTING: "Starting turn", RUNNING_MODEL: "Model is thinking", RUNNING_TOOLS: "Running tools", INTERRUPT_REQUESTED: "Stopping safely", COMPLETED: "Turn completed", INTERRUPTED_PARTIAL: "Interrupted · partial reply retained", FAILED: "Turn failed" })[status];
}

function nestedObject(value: Record<string, unknown>, key: string) {
  const result = value[key];
  return result && typeof result === "object" && !Array.isArray(result) ? result as Record<string, unknown> : null;
}
function shortId(value: string) { return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : "—"; }
function truncate(value: string, length: number) { const clean = value.replaceAll(/\s+/g, " ").trim(); return clean.length > length ? `${clean.slice(0, length)}…` : clean; }
function relativeTime(value: string) { const delta = Date.now() - new Date(value).getTime(); if (!Number.isFinite(delta)) return value; if (delta < 60_000) return "just now"; if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`; if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`; return new Date(value).toLocaleDateString(); }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "The operation failed"; }
function sessionIdFromPath() { return decodeURIComponent(window.location.pathname.match(/^\/chat\/sessions\/([^/]+)/)?.[1] ?? ""); }
