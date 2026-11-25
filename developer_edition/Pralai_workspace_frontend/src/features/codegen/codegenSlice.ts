/**
 * Codegen chat slice.
 *
 * Listens for ws/codegen.* actions (re-dispatched generically by the WS
 * middleware from the gateway envelope — no middleware changes needed,
 * see ws/middleware.ts) and builds up the live state of the in-flight
 * turn: which agent step it's on, which tools it called, and the final
 * validated artifact once codegen.completed arrives.
 *
 * Historical turns (the persisted transcript) come from
 * useListCodegenMessagesQuery; this slice only tracks the *current*
 * in-flight turn's live progress so the chat panel can show "thinking",
 * tool calls, and validation status as they happen.
 */
import { createAction, createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { CodegenValidationStatus, UUID } from "../../types";
import { DEFAULT_ENGINE, isSelectableEngine, type CodegenEngine } from "./engines";
import type {
  CodegenCompletedPayload,
  CodegenFailedPayload,
  CodegenSessionEvent,
  CodegenToolCallPayload,
} from "../../ws/types";

/** The ws middleware dispatches plain objects as `ws/<envelope.type>` with the envelope as
 *  the payload. Passing those strings straight to addCase types the action as Action<T>,
 *  which has no `payload` — so `PayloadAction<X>` did not match and the three typed cases
 *  failed to compile, while the two that "worked" only did so by being `any`. Declaring the
 *  creators gives every case a real payload type. The type strings are unchanged, so nothing
 *  about dispatch or the wire format moves. */
// Payload shapes live in src/ws/types.ts (WS-only — no OpenAPI to generate from).
export const wsCodegenStarted   = createAction<CodegenSessionEvent>("ws/codegen.started");
export const wsCodegenAgentStep = createAction<CodegenSessionEvent>("ws/codegen.agent_step");
export const wsCodegenToolCall  = createAction<CodegenToolCallPayload>("ws/codegen.tool_call");
export const wsCodegenCompleted = createAction<CodegenCompletedPayload>("ws/codegen.completed");
export const wsCodegenFailed    = createAction<CodegenFailedPayload>("ws/codegen.failed");

// Persist the codegen session per workspace to localStorage so the transcript
// survives a full browser refresh (the messages live on the server; we only
// need to remember which session to re-attach to). Keyed by workspace so
// switching workspaces shows the right conversation.
const LS_SESSIONS = "codegen.panelSessions.v1";
const LS_TARGET = "codegen.activeTarget.v1";
const LS_ENGINE = "codegen.activeEngine.v1";

function loadSessions(): Record<string, UUID> {
  try { return JSON.parse(localStorage.getItem(LS_SESSIONS) || "{}"); } catch { return {}; }
}
function saveSessions(m: Record<string, UUID>): void {
  try { localStorage.setItem(LS_SESSIONS, JSON.stringify(m)); } catch { /* ignore quota/denied */ }
}
function loadEngine(): CodegenEngine {
  try {
    const v = localStorage.getItem(LS_ENGINE);
    return isSelectableEngine(v) ? v : DEFAULT_ENGINE;
  } catch { return DEFAULT_ENGINE; }
}
function loadTarget(): string {
  try { return localStorage.getItem(LS_TARGET) || "sql"; } catch { return "sql"; }
}

interface ToolCallEvent {
  tool: string;
  input: Record<string, unknown>;
  resultOk: boolean;
  attempt?: number;
  validationStatus?: string;
}

interface CodegenUIState {
  sessionId: UUID | null;
  status: "idle" | "thinking" | "completed" | "failed";
  step: number;
  // Current codegen-agent stage, for the "Working…" label.
  stage: string | null;
  toolCalls: ToolCallEvent[];
  replyText: string | null;
  artifactPath: string | null;
  artifactCode: string | null;
  companionPath: string | null;
  companionCode: string | null;
  validationStatus: CodegenValidationStatus | null;
  validationDetail: Record<string, unknown> | null;
  error: string | null;
  // Persisted (localStorage) so the chat survives closing the panel, switching
  // model, AND a full browser refresh: the selected target (sent per turn) and
  // one continuous session id per workspace.
  activeTarget: string;
  // The SQL engine chosen for sql / dbt turns (persisted like the target). Never sent for python or flinksql.
  activeEngine: CodegenEngine;
  panelSessionByWorkspace: Record<string, UUID>;
}

const initialState: CodegenUIState = {
  sessionId: null,
  status: "idle",
  step: 0,
  stage: null,
  toolCalls: [],
  replyText: null,
  artifactPath: null,
  artifactCode: null,
  companionPath: null,
  companionCode: null,
  validationStatus: null,
  validationDetail: null,
  error: null,
  activeTarget: loadTarget(),
  activeEngine: loadEngine(),
  panelSessionByWorkspace: loadSessions(),
};

export const codegenSlice = createSlice({
  name: "codegen",
  initialState,
  reducers: {
    setCodegenTarget(state, action: PayloadAction<string>) {
      state.activeTarget = action.payload;
      try { localStorage.setItem(LS_TARGET, action.payload); } catch { /* ignore */ }
    },
    setCodegenEngine(state, action: PayloadAction<string>) {
      if (!isSelectableEngine(action.payload)) return;       // ClickHouse and anything unknown cannot be chosen
      state.activeEngine = action.payload;
      try { localStorage.setItem(LS_ENGINE, action.payload); } catch { /* ignore */ }
    },
    setCodegenSession(state, action: PayloadAction<{ workspaceId: string; sessionId: UUID }>) {
      state.panelSessionByWorkspace[action.payload.workspaceId] = action.payload.sessionId;
      saveSessions({ ...state.panelSessionByWorkspace });
    },
    /**
     * Forget a workspace's stored session.
     *
     * The pointer outlives the browser but the session is owned by whoever
     * created it, so a session recorded under a different identity (or one
     * deleted server-side) comes back 403/404 forever. Without dropping it the
     * panel just renders an empty transcript with no way to recover.
     */
    clearCodegenSession(state, action: PayloadAction<{ workspaceId: string }>) {
      delete state.panelSessionByWorkspace[action.payload.workspaceId];
      saveSessions({ ...state.panelSessionByWorkspace });
    },
    /**
     * "Clear history" for ONE workspace: forget its session (the next message starts a fresh one, so the model also loses the old
     * conversation as context), and drop the live turn if it belongs to this workspace — or to nobody (a stale error). The live turn
     * is one slot shared by every workspace: another workspace's reply that is still streaming must not be touched.
     */
    clearCodegenHistory(state, action: PayloadAction<{ workspaceId: string }>) {
      const old = state.panelSessionByWorkspace[action.payload.workspaceId];
      delete state.panelSessionByWorkspace[action.payload.workspaceId];
      saveSessions({ ...state.panelSessionByWorkspace });
      if (state.sessionId === null || state.sessionId === old) {
        state.sessionId = null;
        state.status = "idle";
        state.step = 0;
        state.stage = null;
        state.toolCalls = [];
        state.replyText = null;
        state.artifactPath = null;
        state.artifactCode = null;
        state.companionPath = null;
        state.companionCode = null;
        state.validationStatus = null;
        state.validationDetail = null;
        state.error = null;
      }
    },
    beginCodegenTurn(state, action: PayloadAction<UUID>) {
      state.sessionId = action.payload;
      state.status = "thinking";
      state.step = 0;
      state.stage = null;
      state.toolCalls = [];
      state.replyText = null;
      state.artifactPath = null;
      state.artifactCode = null;
      state.companionPath = null;
      state.companionCode = null;
      state.validationStatus = null;
      state.validationDetail = null;
      state.error = null;
    },
    // Turn finished (the persisted assistant message is now in history via the
    // REST poll) — drop the live "thinking" pseudo-message.
    endCodegenTurn(state) {
      state.status = "idle";
      state.sessionId = null;
    },
    failCodegenTurn(state, action: PayloadAction<string>) {
      state.status = "failed";
      state.error = action.payload;
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(wsCodegenStarted, (state, action) => {
        if (action.payload?.session_id === state.sessionId) state.status = "thinking";
      })
      .addCase(wsCodegenAgentStep, (state, action) => {
        if (action.payload?.session_id === state.sessionId) {
          state.step = action.payload.step ?? state.step;
          state.stage = action.payload.stage ?? state.stage;
        }
      })
      .addCase(wsCodegenToolCall, (state, action: PayloadAction<CodegenToolCallPayload>) => {
        if (action.payload.session_id === state.sessionId) {
          state.toolCalls.push({
            tool: action.payload.tool,
            // Envelopes without tool arguments (the agent's validate step) must
            // not leave `input` undefined — the panel renders it.
            input: action.payload.input ?? {},
            resultOk: action.payload.result_ok,
            attempt: action.payload.attempt,
            validationStatus: action.payload.validation_status,
          });
        }
      })
      .addCase(wsCodegenCompleted, (state, action: PayloadAction<CodegenCompletedPayload>) => {
        if (action.payload.session_id === state.sessionId) {
          state.status = "completed";
          state.replyText = action.payload.reply_text;
          state.artifactPath = action.payload.artifact_path;
          state.artifactCode = action.payload.artifact_code;
          state.companionPath = action.payload.companion_path;
          state.companionCode = action.payload.companion_code;
          state.validationStatus = action.payload.validation_status;
          state.validationDetail = action.payload.validation_detail;
        }
      })
      .addCase(wsCodegenFailed, (state, action: PayloadAction<CodegenFailedPayload>) => {
        if (action.payload.session_id === state.sessionId) {
          state.status = "failed";
          state.error = action.payload.error_message;
        }
      });
  },
});

export const {
  beginCodegenTurn, endCodegenTurn, failCodegenTurn,
  setCodegenTarget, setCodegenEngine, setCodegenSession, clearCodegenSession, clearCodegenHistory,
} = codegenSlice.actions;
export default codegenSlice.reducer;
