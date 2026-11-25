/**
 * CodegenChatPanel — the Workspace-embedded chat for generating SQL, dbt,
 * and FlinkSQL code with Claude.
 *
 * This is intentionally one panel for all three targets (a dropdown picks
 * the target, not three separate UIs) — see the architecture note in
 * app/services/codegen.py. It is separate from the platform's broader
 * Intelligence chat surface; this one is scoped to authoring code inside
 * the file the user has open.
 *
 * Flow:
 *   1. User picks a target (sql / dbt / flinksql) and types a prompt.
 *   2. POST /api/v1/codegen/sessions/{id}/messages returns 202 immediately;
 *      the reply streams over the caller's own live WebSocket (the same
 *      connection execution.* events use — see features/codegen/codegenSlice.ts) as
 *      ws/codegen.* actions, keyed by the ws_session_id sent with the turn.
 *   3. Tool calls (list_sources, get_table_schema, compile_code) appear
 *      live as the agent works.
 *   4. On codegen.completed with validation_status === 'passed', an
 *      "Apply to file" button calls /apply, which writes the artifact into
 *      the workspace file tree using the same optimistic-concurrency path
 *      as a manual save.
 */
import { useEffect, useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import {
  workspaceApi,
  useCreateCodegenSessionMutation,
  useListCodegenMessagesQuery,
  useSubmitCodegenPromptMutation,
  useApplyCodegenArtifactMutation,
} from "../../../api/client";
import { beginCodegenTurn, endCodegenTurn, failCodegenTurn, setCodegenTarget, setCodegenEngine, setCodegenSession, clearCodegenSession, clearCodegenHistory } from "../codegenSlice";
import type { RootState, AppDispatch } from "../../../store";
import type { CodegenTarget, UUID } from "../../../types";
import { apiErrorMessage, apiErrorStatus } from "../../../api/errors";
import { isLocalBufferId } from "../../workspace/localBuffers";
import SearchSelect from "../../../shared/ui/SearchSelect";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import { ENGINE_OPTIONS, targetUsesEngine } from "../engines";

/** The two header dropdowns share a row once each can be this wide, and wrap onto separate rows below that. */
const CHOICE_MIN_WIDTH = 118;
const CHOICE_STYLE = { flex: `1 1 ${CHOICE_MIN_WIDTH}px`, minWidth: 0 } as const;

interface Props {
  workspaceId: UUID;
  activeFileId: UUID | null;
  activeFileContent: string | null;
  activeFileLanguage: string | null; // "sql" | "ipynb" | ... — used to default the target
  // The live workspace Session id this tab's WebSocket is connected to.
  // Required to submit a turn: without it there's no channel to stream the
  // reply on (see submit_codegen_prompt's ws_session_id, WS-AI-011).
  execSessionId: UUID | null;
  onArtifactApplied: () => void; // parent refetches the file tree / editor content
  onInsertCode: (code: string, target: CodegenTarget) => void; // Claude-style: put code into the open editor
}

// Ceiling on how long to wait for the WS reply before surfacing a timeout.
// This is a single deadline check against locally-held WS-driven state, not
// a poll — nothing is re-fetched while waiting (WS-AI-011: polling itself
// was the problem, driven by codegen events landing on a channel nobody was
// listening to).
const TURN_TIMEOUT_MS = 120_000;

const TARGET_LABELS: Record<CodegenTarget, string> = {
  sql: "SQL",
  python: "Python",
  dbt: "dbt model",
  flinksql: "FlinkSQL job",
};

// Codegen-agent stages (codegen.agent_step `stage`) -> progress label.
const STAGE_LABELS: Record<string, string> = {
  ground: "looking up tables",
  plan: "planning",
  repair: "fixing a validation error",
  loop_detected: "stopping a repeat attempt",
};

export function CodegenChatPanel({ workspaceId, activeFileId, activeFileContent, activeFileLanguage, execSessionId, onArtifactApplied, onInsertCode }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  // ONE continuous session + selected target live in the store, so the chat is
  // NOT cleared when the panel is closed/reopened OR the model is switched. The
  // target is sent per-turn (see handleSend), so switching model just changes
  // what the NEXT message generates — the transcript stays intact.
  const target = useSelector((s: RootState) => s.codegen.activeTarget) as CodegenTarget;
  const sessionId = useSelector((s: RootState) => s.codegen.panelSessionByWorkspace[workspaceId] ?? null);
  const setTarget = (t: CodegenTarget) => dispatch(setCodegenTarget(t));
  // The SQL engine for sql / dbt turns. python and FlinkSQL have one engine each, so the picker is hidden for them and nothing is sent.
  const engine = useSelector((s: RootState) => s.codegen.activeEngine);
  const showEngine = targetUsesEngine(target);

  // Opening a file of another kind switches the assistant to the matching
  // target; the user can still pick another one for the next message.
  useEffect(() => {
    const t = targetForLanguage(activeFileLanguage);
    if (t) dispatch(setCodegenTarget(t));
  }, [activeFileLanguage, dispatch]);
  const [prompt, setPrompt] = useState("");
  const activeConnectionId = useSelector((s: RootState) => s.ui.activeConnectionId);

  const [createSession, { isLoading: creatingSession }] = useCreateCodegenSessionMutation();
  const [submitPrompt, { isLoading: sending }] = useSubmitCodegenPromptMutation();
  const [applyArtifact, { isLoading: applying }] = useApplyCodegenArtifactMutation();
  const { data: history, error: historyError } = useListCodegenMessagesQuery(sessionId ?? ("" as UUID), { skip: !sessionId });

  // The stored session id survives reloads, but the session belongs to the user
  // who created it. One recorded under a different identity — or deleted
  // server-side — answers 403/404 forever, and the panel would sit on an empty
  // transcript with no way out. Drop the pointer so the next prompt starts a
  // fresh session instead.
  useEffect(() => {
    const status = apiErrorStatus(historyError);
    if (sessionId && (status === 403 || status === 404)) {
      dispatch(clearCodegenSession({ workspaceId }));
    }
  }, [historyError, sessionId, workspaceId, dispatch]);
  const live = useSelector((s: RootState) => s.codegen);
  // What the transcript shows. Gated on the session pointer explicitly: a query that is skipped can still hand back the last result it had,
  // and "Clear history" must empty the chat at once, not whenever the cache catches up.
  const messages = sessionId ? history ?? [] : [];
  const [contextNote, setContextNote] = useState<string | null>(null);
  // WS-AI-011: the open file goes to the model only when the user turns this
  // on (per turn; the choice is remembered per browser). Off by default.
  const [includeFile, setIncludeFile] = useState<boolean>(() => readIncludeFilePref());
  const toggleIncludeFile = (on: boolean) => {
    setIncludeFile(on);
    try { localStorage.setItem(INCLUDE_FILE_PREF_KEY, on ? "1" : "0"); } catch { /* private mode */ }
  };
  const turnDeadline = useRef<number | null>(null);

  // No auto-scroll: the transcript stays wherever the user left it. Jumping to
  // the bottom on every history/stream update made earlier turns impossible to
  // read while a reply was coming in.

  const ensureSession = async (): Promise<UUID> => {
    if (sessionId) return sessionId;
    const session = await createSession({
      workspace_id: workspaceId,
      target,
      // An unsaved file ("Untitled-1", id "local:…") has no server record: sending that id is a 422 and the session is never created.
      file_id: activeFileId && !isLocalBufferId(activeFileId) ? activeFileId : undefined,
    }).unwrap();
    dispatch(setCodegenSession({ workspaceId, sessionId: session.id }));
    return session.id;
  };

  const handleSend = async () => {
    if (!prompt.trim()) return;
    if (!execSessionId) {
      dispatch(failCodegenTurn("No active workspace session yet — try again in a moment."));
      return;
    }
    let sid: UUID;
    try {
      sid = await ensureSession();
    } catch (e: unknown) {
      // Without this the click did nothing at all: the prompt stayed in the box and no message appeared.
      dispatch(failCodegenTurn(apiErrorMessage(e, "Could not start a chat session.", { prefer: "detail" })));
      return;
    }
    dispatch(beginCodegenTurn(sid));
    setContextNote(null);
    const text = prompt;
    setPrompt("");
    turnDeadline.current = Date.now() + TURN_TIMEOUT_MS;
    try {
      const accepted = await submitPrompt({
        sessionId: sid,
        body: {
          prompt: text,
          // Never sent unless the user opted in for this turn.
          current_file_content: includeFile ? activeFileContent ?? undefined : undefined,
          include_current_file: includeFile && !!activeFileContent,
          target,
          engine: showEngine ? engine : undefined,
          ws_session_id: execSessionId,
          // The connection the editor is working against: the assistant reads ITS real tables and columns instead of guessing.
          connection_id: activeConnectionId ?? undefined,
        },
      }).unwrap();
      if (includeFile && activeFileContent && !accepted.context_included) {
        setContextNote(contextWithheldMessage(accepted.context_decision_reason));
      }
    } catch (e: unknown) {
      dispatch(failCodegenTurn(apiErrorMessage(e, "Failed to start generation.", { prefer: "detail" })));
    }
    // The reply streams over the WS connection this component already
    // subscribes to (codegenSlice's wsCodegen* reducers) — the effect below
    // reacts to `live.status` reaching a terminal state. No REST polling.
  };

  // Terminal transition on the WS-driven live turn: refresh the persisted
  // transcript once the reply lands, or surface the failure. A single
  // deadline guards against a dropped connection without ever re-fetching.
  useEffect(() => {
    if (live.sessionId !== sessionId || !sessionId) return;
    if (live.status === "completed") {
      dispatch(workspaceApi.util.invalidateTags([{ type: "CodegenMessage", id: sessionId }]));
      dispatch(endCodegenTurn());
      turnDeadline.current = null;
    } else if (live.status === "failed") {
      turnDeadline.current = null;
    }
  }, [live.status, live.sessionId, sessionId, dispatch]);

  // Deadline check while a turn is in flight — ticks locally, never touches
  // the network, so it isn't the polling this replaces.
  useEffect(() => {
    if (live.status !== "thinking" || live.sessionId !== sessionId) return;
    const id = setInterval(() => {
      if (turnDeadline.current !== null && Date.now() > turnDeadline.current) {
        turnDeadline.current = null;
        dispatch(failCodegenTurn("Timed out waiting for a reply. Please try again."));
      }
    }, 2000);
    return () => clearInterval(id);
  }, [live.status, live.sessionId, sessionId, dispatch]);

  // Empties THIS workspace's chat immediately (no reload, no tab change needed): the pointer is forgotten in the store and in
  // localStorage, the live turn is dropped, and the next message starts a fresh session. Other workspaces keep their chats.
  const handleClearHistory = () => {
    dispatch(clearCodegenHistory({ workspaceId }));
    setContextNote(null);
    turnDeadline.current = null;
  };

  const handleApply = async (messageId: UUID) => {
    if (!sessionId) return;
    await applyArtifact({ sessionId, body: { message_id: messageId } }).unwrap();
    onArtifactApplied();
  };

  // The in-flight turn (live.* from WS) isn't in `history` yet until it's
  // persisted; show it as a trailing pseudo-message while streaming.
  const showLiveTurn = live.sessionId === sessionId && live.status !== "idle";
  const hasHistory = messages.length > 0 || showLiveTurn;

  return (
    <aside style={{
      // minmax(0, 1fr): a grid's default column is as wide as its widest content, which pushed the panel's right side (the second
      // dropdown, the Send button) out of a narrow panel where it was clipped.
      display: "grid", gridTemplateRows: "auto 1fr auto", gridTemplateColumns: "minmax(0, 1fr)", height: "100%",
      borderLeft: "1px solid #E3E8E2", background: "#fff", color: "#14201B", minWidth: 0,
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px", borderBottom: "1px solid #E3E8E2", background: "#F8FAF9" }}>
        {/* "Generate", then the choices: side by side when the panel is wide enough for both, wrapping the engine under the target when
            it is narrow (each takes at least CHOICE_MIN_WIDTH and grows to share the row). */}
        <div style={{ height: 28, display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
          <i className="ti ti-sparkles" style={{ fontSize: 15, color: "#0A7D5A" }} />
          <span style={{ fontSize: 12, fontWeight: 600, color: "#5B6D62" }}>Generate</span>
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexWrap: "wrap", gap: 6 }}>
          <SearchSelect
            size="sm"
            ariaLabel="What to generate"
            noun="target"
            menuMinWidth={200}
            style={CHOICE_STYLE}
            value={target}
            onChange={(v) => setTarget(v as CodegenTarget)}
            options={(Object.keys(TARGET_LABELS) as CodegenTarget[]).map((t) => ({ value: t, label: TARGET_LABELS[t] }))}
          />
          {showEngine && (
            <SearchSelect
              size="sm"
              ariaLabel="SQL engine"
              noun="engine"
              title="The SQL dialect the assistant writes, and the engine its dbt check runs on"
              menuMinWidth={170}
              style={CHOICE_STYLE}
              value={engine}
              onChange={(v) => dispatch(setCodegenEngine(v))}
              options={ENGINE_OPTIONS}
            />
          )}
        </div>
        <IconAction
          size="sm"
          icon={ACTION_ICON.delete}
          label={hasHistory ? "Clear this workspace's chat history" : "Nothing to clear"}
          danger
          disabled={!hasHistory}
          style={{ flexShrink: 0 }}
          onClick={handleClearHistory}
        />
      </div>

      <div style={{ minHeight: 0, overflowY: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
        {messages.map((m) => (
          <ChatBubble
            key={m.id}
            role={m.role}
            text={m.content.find((c) => c.type === "text")?.text ?? ""}
            artifactPath={m.generated_path}
            artifactCode={m.generated_code}
            validationStatus={m.validation_status}
            onApply={m.validation_status === "passed" && m.generated_path ? () => handleApply(m.id) : undefined}
            onInsert={m.generated_code ? () => onInsertCode(m.generated_code!, target) : undefined}
            applying={applying}
          />
        ))}

        {showLiveTurn && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {contextNote && (
              <div style={{ fontSize: 11, color: "#A8470A", fontStyle: "italic" }}>{contextNote}</div>
            )}
            {live.toolCalls.map((tc, i) => (
              <div key={i} style={{ fontSize: 11, color: tc.resultOk ? "#2F6B14" : "#B23B3B", fontFamily: "var(--mono)" }}>
                {tc.resultOk ? "✓" : "✗"}{" "}
                {tc.tool === "validate"
                  ? `validate (attempt ${(tc.attempt ?? i) + 1}): ${tc.validationStatus ?? (tc.resultOk ? "passed" : "failed")}`
                  : `${tc.tool}(${Object.values(tc.input ?? {}).map(String).join(", ").slice(0, 60)})`}
              </div>
            ))}
            {live.status === "thinking" && (
              <div style={{ fontSize: 12, color: "#086347", fontStyle: "italic" }}>
                Working…{live.stage && STAGE_LABELS[live.stage] ? ` ${STAGE_LABELS[live.stage]}` : ""}
              </div>
            )}
            {live.status === "completed" && (
              <ChatBubble
                role="assistant"
                text={live.replyText ?? ""}
                artifactPath={live.artifactPath}
                artifactCode={live.artifactCode}
                validationStatus={live.validationStatus}
                onApply={undefined /* applied via persisted message once history refetches */}
                onInsert={live.artifactCode ? () => onInsertCode(live.artifactCode!, target) : undefined}
                applying={applying}
              />
            )}
            {live.status === "failed" && (
              <div style={{ fontSize: 12, color: "#B23B3B", background: "#FBE9E9", border: "1px solid #FECACA", borderRadius: 8, padding: "8px 10px" }}>Generation failed: {live.error}</div>
            )}
          </div>
        )}
      </div>

      <label
        title="Send the file open in the editor with your message. Secrets and personal data are redacted, and your organization's policy can still withhold it."
        style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 10px 0", fontSize: 12, color: activeFileContent ? "#2E3D36" : "#73827A", borderTop: "1px solid #E3E8E2" }}
      >
        <input
          type="checkbox"
          checked={includeFile}
          disabled={!activeFileContent}
          onChange={(e) => toggleIncludeFile(e.target.checked)}
          style={{ accentColor: "#0A7D5A" }}
        />
        Include the open file
      </label>
      <div style={{ display: "flex", gap: 6, padding: 10 }}>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleSend(); }
          }}
          placeholder={`Describe the ${TARGET_LABELS[target].toLowerCase()} you want…`}
          rows={2}
          style={{
            flex: 1, minWidth: 0, resize: "none", background: "#fff", color: "#14201B", outline: "none",
            border: "1px solid #E3E8E2", borderRadius: 8, fontSize: 13, padding: 8,
          }}
        />
        <button
          onClick={handleSend}
          disabled={creatingSession || sending || !prompt.trim()}
          style={{
            alignSelf: "flex-end", background: "#0A7D5A", color: "#fff", border: "none", fontWeight: 600,
            borderRadius: 8, padding: "8px 14px", fontSize: 13,
            cursor: creatingSession || sending || !prompt.trim() ? "not-allowed" : "pointer",
            opacity: creatingSession || sending || !prompt.trim() ? 0.5 : 1,
          }}
        >
          Send
        </button>
      </div>
    </aside>
  );
}

const INCLUDE_FILE_PREF_KEY = "codegen.includeOpenFile";

function targetForLanguage(language: string | null): CodegenTarget | null {
  switch (language) {
    case "sql": return "sql";
    case "flinksql": return "flinksql";
    case "py":
    case "ipynb": return "python";
    default: return null;
  }
}

function readIncludeFilePref(): boolean {
  try { return localStorage.getItem(INCLUDE_FILE_PREF_KEY) === "1"; } catch { return false; }
}

function contextWithheldMessage(reason: string | null | undefined): string {
  if (reason === "org_policy_disabled") {
    return "The assistant didn't see your open file — your organization hasn't enabled sharing file content with the assistant.";
  }
  if (reason === "provider_not_allowlisted") {
    return "The assistant didn't see your open file — this model isn't on your organization's approved list.";
  }
  if (reason?.startsWith("ucs_denied") || reason === "ucs_object_not_configured") {
    return "The assistant didn't see your open file — you don't have permission to share source with this model.";
  }
  return "The assistant didn't see your open file for this turn.";
}

function ChatBubble({
  role, text, artifactPath, artifactCode, validationStatus, onApply, onInsert, applying,
}: {
  role: string;
  text: string;
  artifactPath: string | null;
  artifactCode: string | null;
  validationStatus: string | null;
  onApply: (() => void) | undefined;
  onInsert: (() => void) | undefined;
  applying: boolean;
}) {
  const isUser = role === "user";
  // Show the code only in the code card below — strip the fenced block(s) from
  // the prose so the bubble isn't a giant duplicate of the code.
  const prose = artifactCode ? stripCodeFences(text) : text;
  return (
    <div style={{ alignSelf: isUser ? "flex-end" : "flex-start", maxWidth: "92%", minWidth: 0 }}>
      {prose && (
      <div style={{
        background: isUser ? "#E3F6EA" : "#EEF2ED", color: "#14201B",
        border: `1px solid ${isUser ? "#B7E0CB" : "#E3E8E2"}`, borderRadius: 8, padding: "8px 10px", fontSize: 13, whiteSpace: "pre-wrap", overflowWrap: "anywhere",
      }}>
        {prose}
      </div>
      )}
      {artifactCode && (
        <div style={{ marginTop: 6, border: "1px solid #E3E8E2", borderRadius: 8, overflow: "hidden" }}>
          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            padding: "5px 8px", background: "#F8FAF9", fontSize: 11, color: "#5B6D62", borderBottom: "1px solid #E3E8E2",
          }}>
            <span style={{ fontFamily: "var(--mono)" }}>{artifactPath ?? "generated code"}</span>
            <ValidationBadge status={validationStatus} />
          </div>
          <pre style={{ margin: 0, padding: 10, fontSize: 12, lineHeight: 1.6, maxHeight: 360, overflow: "auto", background: "#14201B", color: "#E3E8E2", fontFamily: "var(--mono)", whiteSpace: "pre" }}>
            {artifactCode}
          </pre>
          {(onInsert || onApply) && (
            <div style={{ padding: 6, borderTop: "1px solid #E3E8E2", background: "#fff", display: "flex", gap: 6 }}>
              {onInsert && (
                <button
                  onClick={onInsert}
                  title="Insert this code into the open editor / notebook cell"
                  style={{
                    background: "#0A7D5A", color: "#fff", border: "none", borderRadius: 7, fontWeight: 600,
                    padding: "5px 10px", fontSize: 12, cursor: "pointer",
                  }}
                >
                  <i className="ti ti-arrow-bar-to-down" style={{ fontSize: 12, marginRight: 4 }} />Insert into editor
                </button>
              )}
              {onApply && (
                <button
                  onClick={onApply}
                  disabled={applying}
                  style={{
                    background: "#fff", color: "#086347", border: "1px solid #B7E0CB", borderRadius: 7, fontWeight: 600,
                    padding: "5px 10px", fontSize: 12, cursor: "pointer",
                  }}
                >
                  {applying ? "Applying…" : "Apply to file"}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ValidationBadge({ status }: { status: string | null }) {
  if (!status) return null;
  const [color, bg] = status === "passed" ? ["#2F6B14", "#EAF3E1"] : status === "failed" ? ["#B23B3B", "#FBE9E9"] : ["#5B6D62", "#EEF2ED"];
  return <span style={{ color, background: bg, fontWeight: 600, borderRadius: 20, padding: "1px 8px" }}>{status}</span>;
}

// Remove fenced code block(s) from an assistant reply so the prose bubble
// doesn't duplicate the code shown in the code card below it.
function stripCodeFences(text: string): string {
  return (text ?? "")
    .replace(/```[\w.+-]*\n[\s\S]*?```/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
