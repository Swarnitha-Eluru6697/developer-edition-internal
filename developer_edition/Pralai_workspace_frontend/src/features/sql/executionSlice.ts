/**
 * Execution slice — accumulates streamed results from the WebSocket gateway.
 *
 * Listens for ws/execution.* actions (re-dispatched from the WS envelope by
 * the middleware) and builds up:
 *   - the column schema for SQL previews
 *   - row batches as they stream in
 *   - terminal state (succeeded / failed / cancelled) + any error
 *
 * UI components subscribe via useAppSelector and re-render incrementally as
 * batches arrive.
 */
import { createAction, createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { ExecutionResult, ExecutionState, UUID } from "../../types";
import type { ExecutionEventEnvelope } from "../../ws/types";
import type { NbOutput } from "../notebook/notebookFormat";
import { emptyResultSet, resultSetsFromWire, type ResultSet } from "./results";

/** ws/middleware.ts re-dispatches every gateway envelope as `ws/<type>` with
 *  the envelope as payload; typed creators give each case a real payload type
 *  (the type strings — and so the wire format — are unchanged). */
export const wsExecutionQueued = createAction<ExecutionEventEnvelope>("ws/execution.queued");
export const wsExecutionStarted = createAction<ExecutionEventEnvelope>("ws/execution.started");
export const wsExecutionChunk = createAction<ExecutionEventEnvelope>("ws/execution.chunk");
export const wsExecutionCompleted = createAction<ExecutionEventEnvelope>("ws/execution.completed");
export const wsExecutionFailed = createAction<ExecutionEventEnvelope>("ws/execution.failed");
export const wsExecutionCancelled = createAction<ExecutionEventEnvelope>("ws/execution.cancelled");

/** Maps the persisted execution status onto the panel's live state. */
function terminalState(status: string, otherwise: ExecutionUIState["state"]): ExecutionUIState["state"] {
  return status === "completed" ? "succeeded" : status === "failed" ? "failed" : otherwise;
}

export interface ExecutionUIState {
  id: UUID | null;
  // The editor tab (file id / local buffer id) these results belong to. The Results pane shows them ONLY while that
  // tab is the active one — a different tab (e.g. a new "Untitled-1.sql") must not show another file's output.
  ownerTab: UUID | null;
  state: ExecutionState | "idle";
  columns: { name: string; type: string }[];
  rows: unknown[][];
  rowsTruncated: boolean;
  // One entry per statement of a multi-statement script (empty or one entry for a single
  // statement, which keeps using columns/rows above). The Results pane stacks these.
  results: ResultSet[];
  // What a non-SQL run (a .py file on the notebook kernel) printed or returned: streams, values,
  // tables, images, tracebacks. Empty for SQL.
  outputs: NbOutput[];
  error: string | null;
  startedAt: number | null;
  finishedAt: number | null;
  durationMs: number | null;
  // Set when viewing a past query from history — the SQL text shown above results.
  code: string | null;
  fromHistory: boolean;
}

const initialState: ExecutionUIState = {
  id: null,
  ownerTab: null,
  state: "idle",
  columns: [],
  rows: [],
  rowsTruncated: false,
  results: [],
  outputs: [],
  error: null,
  startedAt: null,
  finishedAt: null,
  durationMs: null,
  code: null,
  fromHistory: false,
};

export const executionSlice = createSlice({
  name: "execution",
  initialState,
  reducers: {
    resetExecution: () => initialState,
    // A new file is saved under a NEW id (and its tab replaced): the results it had move with it.
    reassignExecutionOwner(state, action: PayloadAction<{ from: UUID; to: UUID }>) {
      if (state.ownerTab === action.payload.from) state.ownerTab = action.payload.to;
    },
    beginExecution: {
      reducer(state, action: PayloadAction<{ id: UUID; ownerTab: UUID | null }>) {
        state.id = action.payload.id;
        state.ownerTab = action.payload.ownerTab;
        state.state = "queued";
        state.columns = [];
        state.rows = [];
        state.rowsTruncated = false;
        state.results = [];
        state.outputs = [];
        state.error = null;
        state.startedAt = null;
        state.finishedAt = null;
        state.durationMs = null;
        state.code = null;
        state.fromHistory = false;
      },
      prepare: (id: UUID, ownerTab: UUID | null = null) => ({ payload: { id, ownerTab } }),
    },
    // Load a past query's result into the Results pane (from the history panel).
    // Unlike setExecutionResult, this replaces the active execution id outright.
    showHistoricalExecution(state, action: PayloadAction<{
      execution_id: string; status: string; code_text?: string | null;
      columns: { name: string; type: string }[]; rows: unknown[][];
      row_count?: number | null; duration_ms?: number | null; error_message?: string | null;
      results?: ResultSet[];
      outputs?: NbOutput[];
      ownerTab?: UUID | null;
    }>) {
      const p = action.payload;
      state.id = p.execution_id;
      state.ownerTab = p.ownerTab ?? null;
      state.columns = p.columns ?? [];
      state.rows = p.rows ?? [];
      state.state = terminalState(p.status, "idle");
      state.error = p.error_message ?? null;
      state.code = p.code_text ?? null;
      state.durationMs = p.duration_ms ?? null;
      state.rowsTruncated = (p.row_count ?? 0) > (p.rows?.length ?? 0);
      state.results = p.results ?? [];
      state.outputs = p.outputs ?? [];
      state.startedAt = null;
      state.finishedAt = Date.now();
      state.fromHistory = true;
    },
    // Reliable REST fallback: fully replace the result from GET /executions/{id}/result.
    setExecutionResult(state, action: PayloadAction<ExecutionResult>) {
      const p = action.payload;
      if (p.execution_id !== state.id) return;
      state.columns = p.columns ?? [];
      state.rows = p.rows ?? [];
      state.results = resultSetsFromWire(p.results);
      state.outputs = p.outputs ?? [];
      state.state = terminalState(p.status, state.state);
      state.error = p.error_message ?? null;
      state.finishedAt = Date.now();
    },
  },
  extraReducers: (builder) => {
    // The middleware dispatches the full WS envelope as action.payload:
    //   { v, ts, type, execution_id, chunk_index?, output_type?, payload?{ rows, columns } }
    builder
      .addCase(wsExecutionQueued, (state, a) => {
        if (a.payload?.execution_id === state.id) state.state = "queued";
      })
      .addCase(wsExecutionStarted, (state, a) => {
        if (a.payload?.execution_id === state.id) {
          state.state = "running";
          state.startedAt = Date.now();
        }
      })
      // SQL rows stream as execution.chunk; columns ride along on the first chunk.
      .addCase(wsExecutionChunk, (state, a) => {
        if (a.payload?.execution_id !== state.id) return;
        const inner = a.payload.payload ?? {};
        if (inner.columns && state.columns.length === 0) state.columns = inner.columns;
        if (Array.isArray(inner.rows)) state.rows.push(...inner.rows);
        // A script of several statements tags every batch with its statement's index: keep each
        // statement's rows apart too, so they can be shown as separate tables.
        if (typeof inner.statement === "number") {
          while (state.results.length <= inner.statement) state.results.push(emptyResultSet(state.results.length));
          const r = state.results[inner.statement];
          if (inner.sql && !r.sql) r.sql = inner.sql;
          if (a.payload.output_type === "statement") {
            if (inner.status) r.status = inner.status;
            if (inner.truncated) r.truncated = true;
          } else {
            if (inner.columns && r.columns.length === 0) r.columns = inner.columns;
            if (Array.isArray(inner.rows)) r.rows.push(...inner.rows);
          }
        }
      })
      .addCase(wsExecutionCompleted, (state, a) => {
        if (a.payload?.execution_id !== state.id) return;
        state.state = "succeeded";
        state.finishedAt = Date.now();
        if (typeof a.payload.duration_ms === "number") state.durationMs = a.payload.duration_ms;
        if (typeof a.payload.total_rows === "number" && a.payload.total_rows > state.rows.length) {
          state.rowsTruncated = true;
        }
      })
      .addCase(wsExecutionFailed, (state, a) => {
        if (a.payload?.execution_id !== state.id) return;
        state.state = "failed";
        state.error = a.payload.error_message ?? a.payload.payload?.error_message ?? "Query failed";
        state.finishedAt = Date.now();
      })
      .addCase(wsExecutionCancelled, (state, a) => {
        if (a.payload?.execution_id === state.id) {
          state.state = "cancelled";
          state.finishedAt = Date.now();
        }
      });
  },
});

export const { resetExecution, reassignExecutionOwner, beginExecution, setExecutionResult, showHistoricalExecution } = executionSlice.actions;
export default executionSlice.reducer;

/** What the Results pane may show for `activeTab`: the execution if it belongs to that tab, else an idle (empty) one. A stable
 *  reference each time, so selecting it never causes a re-render by itself. */
export const IDLE_EXECUTION: ExecutionUIState = initialState;
export function visibleExecution(exec: ExecutionUIState, activeTab: UUID | null): ExecutionUIState {
  return exec.id !== null && exec.ownerTab === activeTab ? exec : IDLE_EXECUTION;
}
