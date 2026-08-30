export type TurnStatus =
  | "WAITING"
  | "STARTING"
  | "RUNNING_MODEL"
  | "RUNNING_TOOLS"
  | "INTERRUPT_REQUESTED"
  | "COMPLETED"
  | "INTERRUPTED_PARTIAL"
  | "FAILED";

export type ChatSession = {
  session_id: string;
  owner_principal_id: string;
  title: string;
  status: "ACTIVE" | "ARCHIVED" | "DELETED";
  revision: number;
  default_branch_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatBranch = {
  branch_id: string;
  session_id: string;
  name: string;
  root_unit_id: string | null;
  head_unit_id: string | null;
  created_from_unit_id: string | null;
};

export type ConversationUnit = {
  unit_id: string;
  session_id: string;
  parent_unit_id: string | null;
  unit_type: "USER_INPUT" | "MODEL_RESPONSE";
  status: "OPEN" | "SETTLED";
  turn_id: string | null;
  model_step_id: string | null;
  display_text: string;
  content: Record<string, unknown>;
  interrupted: boolean;
  created_at: string;
};

export type TurnRun = {
  turn_id: string;
  session_id: string;
  branch_id: string;
  input_unit_id: string;
  final_unit_id: string | null;
  status: TurnStatus;
  max_tool_calls: number;
  used_tool_calls: number;
  completion_reason: string;
  error: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type ChatGraph = {
  session: ChatSession;
  branches: ChatBranch[];
  units: ConversationUnit[];
  turns: TurnRun[];
};

export type TurnEvent = {
  event_id: number;
  turn_id: string;
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type ToolExecution = {
  execution_id: string;
  turn_id: string;
  source_step_id: string;
  source_item_id: string;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  result: Record<string, unknown>;
  error: Record<string, unknown>;
  attempt_count: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export const ACTIVE_TURN_STATUSES = new Set<TurnStatus>([
  "WAITING",
  "STARTING",
  "RUNNING_MODEL",
  "RUNNING_TOOLS",
  "INTERRUPT_REQUESTED",
]);
