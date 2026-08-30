import { request } from "../../api";
import type { ChatGraph, ChatSession, ToolExecution, TurnEvent, TurnRun } from "./types";

const ROOT = "/api/chat/v1";

export const chatApi = {
  listSessions: () => request<{ sessions: ChatSession[] }>(`${ROOT}/sessions`),
  createSession: (title: string) =>
    request<{ session: ChatSession }>(`${ROOT}/sessions`, {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  graph: (sessionId: string) =>
    request<ChatGraph>(`${ROOT}/sessions/${encodeURIComponent(sessionId)}/graph`),
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
    request<{ turn: TurnRun }>(
      `${ROOT}/sessions/${encodeURIComponent(sessionId)}/turns`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  regenerate: (unitId: string, baseRevision: number, maxToolCalls: number) =>
    request<{ turn: TurnRun }>(`${ROOT}/units/${encodeURIComponent(unitId)}/regenerate`, {
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
      `${ROOT}/units/${encodeURIComponent(unitId)}/edit-and-regenerate`,
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
    request<{ turn: TurnRun }>(`${ROOT}/turns/${encodeURIComponent(turnId)}/interrupt`, {
      method: "POST",
      body: "{}",
    }),
  turn: (turnId: string) =>
    request<{ turn: TurnRun }>(`${ROOT}/turns/${encodeURIComponent(turnId)}`),
  tools: (turnId: string) =>
    request<{ tool_executions: ToolExecution[] }>(
      `${ROOT}/turns/${encodeURIComponent(turnId)}/tool-executions`,
    ),
};

export async function streamTurnEvents(
  turnId: string,
  after: number,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
) {
  let cursor = after;
  const response = await fetch(
    `${ROOT}/turns/${encodeURIComponent(turnId)}/events/stream?after=${cursor}`,
    {
      signal,
      credentials: "include",
      headers: { Accept: "text/event-stream", "Last-Event-ID": String(cursor) },
    },
  );
  if (!response.ok || !response.body) throw new Error(`Event stream failed (${response.status})`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const event = parseFrame(frame, turnId);
      if (!event || event.sequence <= cursor) continue;
      cursor = event.sequence;
      onEvent(event);
    }
    if (done) break;
  }
  return cursor;
}

function parseFrame(frame: string, turnId: string): TurnEvent | null {
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
  return {
    event_id: 0,
    turn_id: turnId,
    sequence,
    type,
    payload: JSON.parse(data.join("\n")) as Record<string, unknown>,
    created_at: new Date().toISOString(),
  };
}
