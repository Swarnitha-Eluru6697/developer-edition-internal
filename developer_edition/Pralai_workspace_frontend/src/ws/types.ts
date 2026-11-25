/**
 * WebSocket gateway message types.
 *
 * Hand-maintained on purpose: OpenAPI describes REST only, so the WS gateway
 * (backend app/ws/gateway.py, plus the codegen agent's ws events) has no
 * machine-readable contract to generate these from. Keep them in sync with
 * the gateway by hand — this is the one remaining hand-written wire contract
 * besides the few REST routes whose backend handlers return a bare dict
 * (see src/types/index.ts).
 *
 * Every envelope is re-dispatched by ws/middleware.ts as a Redux action
 * `ws/<type>` whose payload is the whole envelope.
 */
import type { CodegenValidationStatus, ISODate, UUID } from "../types";

export interface WSEnvelope<T = unknown> {
  v: 1;
  type: WSMessageType;
  ts: ISODate;
  payload: T;
}

export type WSMessageType =
  // server -> client
  | "execution.queued"
  | "execution.started"
  | "execution.schema"      // SQL schema rows
  | "execution.rows"        // SQL result row batch
  | "execution.chunk"       // SQL rows (+ columns on the first chunk)
  | "execution.stream"      // notebook stdout / stderr
  | "execution.display"     // notebook display_data
  | "execution.result"      // notebook execute_result
  | "execution.error"
  | "execution.succeeded"
  | "execution.completed"
  | "execution.failed"
  | "execution.cancelled"
  | "session.hello"
  | "session.bye"
  | "ping"
  // codegen chat (server -> client)
  | "codegen.started"
  | "codegen.agent_step"
  | "codegen.tool_call"
  | "codegen.completed"
  | "codegen.failed"
  // client -> server
  | "execute.submit"
  | "execute.cancel"
  | "pong";

/** Execution-event envelope as the execution slice reduces it: the gateway
 *  puts execution_id (and terminal stats) at the top level, row batches in
 *  the nested `payload`. */
export interface ExecutionEventEnvelope {
  execution_id?: UUID;
  duration_ms?: number;
  total_rows?: number;
  error_message?: string;
  /** "rows" for a row batch, "statement" when one statement of a script finishes. */
  output_type?: string;
  payload?: {
    columns?: { name: string; type: string }[];
    rows?: unknown[][];
    error_message?: string;
    /** Script statement this batch belongs to (0-based); absent for a single statement. */
    statement?: number;
    sql?: string;
    status?: string | null;
    truncated?: boolean;
  };
}

// ── Codegen chat events ───────────────────────────────────────────────────
export interface CodegenSessionEvent {
  session_id: string;
  /** The agent's step index. */
  step?: number;
  /** ground | plan | repair | loop_detected */
  stage?: string;
}

export interface CodegenToolCallPayload {
  session_id: UUID;
  tool: string;
  // Absent on the codegen agent's "validate" step — it reports `attempt` and
  // `validation_status` instead of tool arguments.
  input?: Record<string, unknown>;
  result_ok: boolean;
  attempt?: number;
  validation_status?: string;
}

export interface CodegenCompletedPayload {
  session_id: UUID;
  message_id: UUID;
  reply_text: string;
  artifact_path: string | null;
  artifact_code: string | null;
  companion_path: string | null;
  companion_code: string | null;
  validation_status: CodegenValidationStatus | null;
  validation_detail: Record<string, unknown>;
}

export interface CodegenFailedPayload {
  session_id: UUID;
  error_message: string;
}
