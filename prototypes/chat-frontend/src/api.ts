import type {
  ChatGraph,
  ChatSession,
  ToolExecution,
  TurnEvent,
  TurnRun,
} from "./types";

const API_ROOT = "/api/chat/v1";
const PRINCIPAL_KEY = "chat-v2-principal-id";

export function getPrincipalId() {
  return localStorage.getItem(PRINCIPAL_KEY) ?? "";
}

export function setPrincipalId(value: string) {
  localStorage.setItem(PRINCIPAL_KEY, value);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const principalId = getPrincipalId();
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Chat-Principal-Id": principalId,
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listSessions: () => request<{ sessions: ChatSession[] }>("/sessions"),
  createSession: (title: string) =>
    request<{ session: ChatSession }>("/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  graph: (sessionId: string) =>
    request<ChatGraph>(`/sessions/${encodeURIComponent(sessionId)}/graph`),
  createTurn: (
    sessionId: string,
    body: {
      content: string;
      branch_id: string | null;
      parent_unit_id: string | null;
      base_revision: number;
      max_tool_calls: number;
    },
  ) =>
    request<{ turn: TurnRun }>(`/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  regenerate: (unitId: string, baseRevision: number, maxToolCalls: number) =>
    request<{ turn: TurnRun }>(`/units/${encodeURIComponent(unitId)}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ base_revision: baseRevision, max_tool_calls: maxToolCalls }),
    }),
  editAndRegenerate: (
    unitId: string,
    content: string,
    baseRevision: number,
    maxToolCalls: number,
  ) =>
    request<{ turn: TurnRun }>(
      `/units/${encodeURIComponent(unitId)}/edit-and-regenerate`,
      {
        method: "POST",
        body: JSON.stringify({
          content,
          base_revision: baseRevision,
          max_tool_calls: maxToolCalls,
        }),
      },
    ),
  interrupt: (turnId: string) =>
    request<{ turn: TurnRun }>(`/turns/${encodeURIComponent(turnId)}/interrupt`, {
      method: "POST",
      body: "{}",
    }),
  turn: (turnId: string) => request<{ turn: TurnRun }>(`/turns/${encodeURIComponent(turnId)}`),
  tools: (turnId: string) =>
    request<{ tool_executions: ToolExecution[] }>(
      `/turns/${encodeURIComponent(turnId)}/tool-executions`,
    ),
};

export async function streamTurnEvents(
  turnId: string,
  after: number,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
) {
  let cursor = after;
  while (!signal.aborted) {
    const response = await fetch(
      `${API_ROOT}/turns/${encodeURIComponent(turnId)}/events/stream?after=${cursor}`,
      {
        signal,
        headers: {
          Accept: "text/event-stream",
          "Last-Event-ID": String(cursor),
          "X-Chat-Principal-Id": getPrincipalId(),
        },
      },
    );
    if (!response.ok || !response.body) {
      throw new Error(`Event stream failed (${response.status})`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const parsed = parseSseFrame(frame, turnId);
        if (!parsed || parsed.sequence <= cursor) continue;
        cursor = parsed.sequence;
        onEvent(parsed);
      }
      if (done) return cursor;
    }
  }
  return cursor;
}

function parseSseFrame(frame: string, turnId: string): TurnEvent | null {
  if (!frame || frame.startsWith(":")) return null;
  let sequence = 0;
  let type = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) sequence = Number(line.slice(3).trim());
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!sequence || !data.length) return null;
  const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
  return {
    event_id: 0,
    turn_id: turnId,
    sequence,
    type,
    payload,
    created_at: new Date().toISOString(),
  };
}
