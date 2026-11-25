import { useEffect, useRef, useState } from 'react';
import { api, type StreamEvent } from '../api';
import type { UploadedFileRef } from '../api';
import Icon from './Icon';
import SearchSelect from './SearchSelect';
import { NODE_TYPES } from '../constants';
import type { Agent, HitlPromptState, BatchRunResult } from '../types';
import type { InputField } from './AddInputFieldModal';
import { parseCsv, downloadCsv, downloadJson } from '../csv';

// §0.117, GAP_REPORT — a real row's own `error` field can now be
// either a real, plain string (dev/test, or a real, non-production
// deployment) or the real, structured
// `{ error_code, correlation_id }` shape a production deployment now
// returns for a genuinely unexpected failure — formats either real
// shape into one, real, human-readable line, so nothing downstream
// needs its own, separate branch to tell which one it got.
function formatRowError(error: string | { error_code: string; correlation_id: string } | null | undefined): string {
  if (!error) return '';
  if (typeof error === 'string') return error;
  return `${error.error_code} (ref: ${error.correlation_id})`;
}

export interface ChatMsg {
  role: 'user' | 'assistant';
  text: string;
  steps?: { phase: string; output: string }[] | null;
  strategy?: string | null;
  followUps?: string[] | null;
  citations?: string[] | null;
  hitl?: HitlPromptState;
}

export const APP_TYPE_LABEL: Record<string, string> = {
  workflow: 'Workflow', chatflow: 'Chatflow', chatbot: 'Chatbot', agent: 'Agent', text_generator: 'Text Generator',
};

export function isChatLikeAppType(appType: string) {
  return appType === 'chatbot' || appType === 'chatflow' || appType === 'agent';
}

/** The single source of truth for "does this agent get a chat or a form
 * UI" — used by BOTH the real published /app/:agentId page and the
 * Builder's Try It panel. Before this existed, AgentRunner had its own
 * separate (and wrong) check that defaulted a Data agent's missing
 * app_type to 'chatbot', silently giving real end users a chat interface
 * for an agent that was never meant to be conversational — Try It got it
 * right, the actual published page didn't. One function now, so the two
 * surfaces can't quietly diverge again. */
export function isChatLikeAgent(agent: Agent): boolean {
  if (agent.type !== 'App') return false; // Data agents are always form-style — single input/output, never chat
  return isChatLikeAppType(agent.app_type || 'chatbot');
}

function HitlForm({ hitl, busy, onValuesChange, onRespond }: {
  hitl: HitlPromptState; busy: boolean;
  onValuesChange: (values: Record<string, string>) => void; onRespond: (actionId: string, actionTitle: string) => void;
}) {
  if (hitl.resolved) return <div className="chat-bubble-text small muted">This request has been responded to.</div>;
  const values = hitl.values || {};
  const missingRequired = hitl.fields.some((f) => f.required && !(values[f.name] || '').trim());
  const setValue = (name: string, v: string) => onValuesChange({ ...values, [name]: v });
  return (
    <div className="hitl-form">
      <div className="chat-bubble-text" style={{ whiteSpace: 'pre-wrap' }}>{hitl.renderedContent}</div>
      {hitl.fields.length > 0 && (
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {hitl.fields.map((f) => (
            <div className="field" style={{ margin: 0 }} key={f.name}>
              <label>{f.name}{f.required ? ' *' : ''}</label>
              {f.type === 'paragraph' && <textarea rows={2} value={values[f.name] || ''} onChange={(e) => setValue(f.name, e.target.value)} />}
              {f.type === 'select' && (
                <SearchSelect ariaLabel={f.name} noun="option" placeholder="Choose…"
                  value={values[f.name] || ''}
                  options={(f.options || []).map((o) => ({ value: o, label: o }))}
                  onChange={(v) => setValue(f.name, v)} />
              )}
              {(f.type === 'single_file' || f.type === 'file_list') && (
                <input placeholder="No real file upload in this reference platform — type a filename placeholder" value={values[f.name] || ''} onChange={(e) => setValue(f.name, e.target.value)} />
              )}
            </div>
          ))}
        </div>
      )}
      <div className="row-split" style={{ gap: 8, marginTop: 10, justifyContent: 'flex-start' }}>
        {hitl.actions.map((a) => (
          <button key={a.id} className={`btn sm ${a.style === 'primary' ? '' : 'ghost'}`} disabled={busy || missingRequired}
            onClick={() => onRespond(a.id, a.title)}>{a.title}</button>
        ))}
      </div>
      {hitl.deliveryStatus && (
        <p className="small muted" style={{ marginTop: 8, marginBottom: 0 }}>
          {Object.entries(hitl.deliveryStatus).map(([m, s]) => `${m}: ${s}`).join(' · ')}
        </p>
      )}
    </div>
  );
}

/** invokingUser distinguishes a published app's real run from an author's
 * own preview run — both hit the exact same real /run endpoint and the exact
 * same UI, just tagged differently so real usage metrics/logs aren't
 * confused with a developer testing their own draft. Features are managed
 * from the Features button in the Try it panel's header, not from here. */
/** A result value that scrolls inside itself once it gets long.
 *
 * The scrollbar is deliberately hidden, which loses the affordance the
 * rest of the app keeps on purpose (see the note on .pal/.cfg in
 * styles.css: macOS hides overlay scrollbars until something moves, so a
 * clipped panel and a scrollable one look identical). The fade at the
 * bottom edge is what replaces it — set only while the content actually
 * overflows, so a short value is not given a shadow suggesting more
 * text that is not there.
 */
function ScrollValue({ children, className = '', maxHeight }:
                     { children: React.ReactNode; className?: string; maxHeight?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setOverflowing(el.scrollHeight > el.clientHeight + 1);
    measure();
    // Re-measured on resize because the same value wraps to a different
    // height in one column than in two.
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
  }, [children]);

  return (
    <div
      ref={ref}
      className={`so-value${overflowing ? ' so-value-more' : ''}${className ? ` ${className}` : ''}`}
      style={maxHeight ? { maxHeight } : undefined}
      tabIndex={overflowing ? 0 : -1}
    >
      {children}
    </div>
  );
}


export function ChatRunner({ agent, appType, busy, setBusy, invokingUser = 'user:end-user', preview }:
  { agent: Agent; appType: string; busy: boolean; setBusy: (b: boolean) => void; invokingUser?: string; preview?: boolean }) {
  const opener = agent.features?.conversation_opener;
  const openerText = (opener?.enabled && opener.text) ? opener.text : '';
  const [messages, setMessages] = useState<ChatMsg[]>(
    openerText ? [{ role: 'assistant', text: openerText }] : []
  );

  // A useState initializer runs once, on mount. Turning the opener on
  // (or editing its text) while this runner is already mounted left the
  // greeting absent until a remount, which looked like the feature not
  // working. Seeded here as well, but only while the conversation is
  // still empty — never injected into a conversation in progress.
  // Replay the stored transcript for this agent and identity.
  //
  // The rows were already being written and already being read — the
  // harness hands prior turns to the model, so it genuinely remembers.
  // Nothing fetched them for display, so closing the web app and
  // reopening it showed an empty chat while the agent carried on as
  // though the earlier turns had happened: it would answer a follow-up
  // to a question no longer on screen.
  //
  // `historyLoaded` gates the opener below, so a returning conversation
  // does not get a greeting pushed in front of it.
  const [historyLoaded, setHistoryLoaded] = useState(false);
  // Replaying a transcript is what the Chat History feature IS, so it
  // follows the switch. With it off nothing is stored for later turns
  // anyway, and showing an earlier conversation would imply a memory
  // the agent does not have.
  const chatHistoryEnabled = !!agent.features?.chat_history?.enabled;
  // The thread this runner is in. Null means "whatever the stored
  // history says", which is how a returning visitor lands back in
  // their last conversation without choosing one.
  useEffect(() => {
    if (!chatHistoryEnabled) { setHistoryLoaded(true); return; }
    let live = true;
    api.getChatHistory(agent.id, invokingUser)
      .then((rows) => {
        if (!live) return;
        if (rows.length === 0) {
          setMessages([]);
          return;
        }
        setMessages(rows.map((r) => ({ role: r.role, text: r.content })));
      })
      // No stored history, chat history switched off, or an older
      // deployment without the endpoint — start empty rather than
      // failing the panel.
      .catch(() => {})
      .finally(() => { if (live) setHistoryLoaded(true); });
    return () => { live = false; };
  }, [agent.id, invokingUser, chatHistoryEnabled]);

  useEffect(() => {
    if (!openerText || !historyLoaded) return;
    setMessages((prev) => (prev.length === 0 ? [{ role: 'assistant', text: openerText }] : prev));
  }, [openerText, historyLoaded]);
  const [input, setInput] = useState('');
  const [expandedSteps, setExpandedSteps] = useState<number | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView?.({ behavior: 'smooth' }); }, [messages]);

  const send = (overrideText?: string) => {
    const text = (overrideText ?? input).trim();
    if (!text || busy) return;
    setMessages((m) => [...m, { role: 'user', text }]);
    setInput('');
    setBusy(true);
    api.runAgent(agent.id, text, invokingUser, undefined, preview)
      .then((r) => {
        if (r.status === 'blocked') {
          setMessages((m) => [...m, { role: 'assistant', text: r.output || 'This message was blocked by this app\'s content moderation settings.' }]);
          return;
        }
        if (r.status === 'paused_for_human_input' && r.hitl_request_id) {
          setMessages((m) => [...m, {
            role: 'assistant', text: '',
            hitl: {
              requestId: r.hitl_request_id!, renderedContent: r.hitl_rendered_content || '',
              fields: r.hitl_fields || [], actions: r.hitl_actions || [],
              deliveryStatus: r.hitl_delivery_status, values: {},
            },
          }]);
          return;
        }
        setMessages((m) => [...m, {
          role: 'assistant',
          text: r.output || (r.status === 'success' ? '(no output)' : `This app hit an error (${r.status}${r.fail_class ? `: ${r.fail_class}` : ''}) — real failure, not a placeholder.`),
          steps: appType === 'agent' ? r.agent_steps : null,
          strategy: r.agent_strategy,
          followUps: r.follow_ups,
          citations: r.citations,
        }]);
      })
      .catch((e) => setMessages((m) => [...m, { role: 'assistant', text: `Error: ${e.message}` }]))
      .finally(() => setBusy(false));
  };

  const setHitlValues = (msgIdx: number, values: Record<string, string>) => {
    setMessages((m) => m.map((msg, i) => (i === msgIdx && msg.hitl ? { ...msg, hitl: { ...msg.hitl, values } } : msg)));
  };

  const respondToHitl = (msgIdx: number, actionId: string, actionTitle: string) => {
    const msg = messages[msgIdx];
    if (!msg.hitl || busy) return;
    const { requestId, values } = msg.hitl;
    setMessages((m) => m.map((x, i) => (i === msgIdx && x.hitl ? { ...x, hitl: { ...x.hitl, resolved: true } } : x)));
    setMessages((m) => [...m, { role: 'user', text: `[${actionTitle}]` }]);
    setBusy(true);
    api.respondToHitl(agent.id, requestId, actionId, values)
      .then((r) => {
        if (r.status === 'paused_for_human_input' && r.hitl_request_id) {
          setMessages((m) => [...m, {
            role: 'assistant', text: '',
            hitl: {
              requestId: r.hitl_request_id!, renderedContent: r.hitl_rendered_content || '',
              fields: r.hitl_fields || [], actions: r.hitl_actions || [],
              deliveryStatus: r.hitl_delivery_status, values: {},
            },
          }]);
          return;
        }
        const structuredText = r.structured_output && Object.keys(r.structured_output).length > 0
          ? Object.entries(r.structured_output).map(([k, v]) => `${k}: ${v}`).join('\n') : null;
        setMessages((m) => [...m, {
          role: 'assistant',
          text: r.output || structuredText || r.note || (r.status === 'success' ? '(no output)' : `This app hit an error (${r.status}) — real failure, not a placeholder.`),
        }]);
      })
      .catch((e) => setMessages((m) => [...m, { role: 'assistant', text: `Error: ${e.message}` }]))
      .finally(() => setBusy(false));
  };

  const startingQuestions = opener?.enabled ? (opener.opening_questions || []) : [];
  const showStarters = startingQuestions.length > 0 && messages.every((m) => m.role === 'assistant');

  return (
    <div className="chat-runner">
      <div className="chat-messages">
        {messages.length === 0 && <div className="empty-cfg" style={{ padding: 40 }}>Say something to get started.</div>}
        {messages.map((m, i) => (
          <div key={i} className={`chat-bubble ${m.role}`}>
            {m.hitl ? (
              <HitlForm hitl={m.hitl} busy={busy} onValuesChange={(v) => setHitlValues(i, v)} onRespond={(actionId, actionTitle) => respondToHitl(i, actionId, actionTitle)} />
            ) : (
              <div className="chat-bubble-text">{m.text}</div>
            )}
            {m.citations && m.citations.length > 0 && (
              <div className="chat-citations">
                <div className="small muted" style={{ fontWeight: 700, marginBottom: 4 }}>Sources</div>
                {m.citations.map((c, ci) => <div key={ci} className="chat-citation">{c}</div>)}
              </div>
            )}
            {m.steps && m.steps.length > 0 && (
              <div className="chat-reasoning">
                <button className="btn sm ghost" onClick={() => setExpandedSteps(expandedSteps === i ? null : i)}>
                  <Icon name="i-cpu" /> {expandedSteps === i ? 'Hide' : 'Show'} reasoning ({m.strategy})
                </button>
                {expandedSteps === i && (
                  <div className="chat-steps">
                    {m.steps.map((s, si) => (
                      <div key={si} className="chat-step"><b>{s.phase}</b><div className="small muted">{s.output}</div></div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {m.followUps && m.followUps.length > 0 && (
              <div className="chat-followups">
                {m.followUps.map((f, fi) => (
                  <button key={fi} className="chat-followup-chip" disabled={busy} onClick={() => send(f)}>{f}</button>
                ))}
              </div>
            )}
          </div>
        ))}
        {showStarters && (
          <div className="chat-starters">
            {startingQuestions.map((q, qi) => (
              <button key={qi} className="chat-followup-chip" disabled={busy} onClick={() => send(q)}>{q}</button>
            ))}
          </div>
        )}
        <div ref={endRef} />
      </div>
      <div className="chat-input-row">
        <input
          placeholder={(agent?.features?.webapp_settings?.input_placeholder || '').trim() || 'Type a message…'} value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          disabled={busy}
        />
        <button className="btn" onClick={() => send()} disabled={busy || !input.trim()}>
          {busy ? 'Thinking…' : <><Icon name="i-send" /> Send</>}
        </button>
      </div>
      {/* Replaying the transcript makes starting over impossible without
          this: the model would keep being handed turns from a
          conversation the person considers finished. Only offered once
          there is something to clear. */}
    </div>
  );
}

interface WorkflowRow {
  node_id: string;
  node_type: string;
  status: 'running' | 'ok' | 'skipped' | 'ucs_denied' | 'harness_fail' | 'error' | 'paused';
}

/** Real live per-node progress — rows are built entirely from real
 * node_start/node_done SSE events as the backend's harness traversal
 * actually reaches each node (see api.ts's streamAgentRun), so a row
 * only ever appears once that node has genuinely started, and only
 * flips to done/error once it genuinely has — never a canned replay
 * animated to look live. */
function WorkflowProcessPanel({ events, running, cancelled }: { events: StreamEvent[]; running: boolean; cancelled: boolean }) {
  const rows: WorkflowRow[] = [];
  const indexById = new Map<string, number>();
  for (const e of events) {
    if (e.type === 'node_start' && e.node_id) {
      indexById.set(e.node_id, rows.length);
      rows.push({ node_id: e.node_id, node_type: e.node_type || 'unknown', status: 'running' });
    } else if (e.type === 'node_done' && e.node_id) {
      const idx = indexById.get(e.node_id);
      if (idx !== undefined) rows[idx].status = (e.status as WorkflowRow['status']) || 'ok';
    }
  }
  if (rows.length === 0) return null;

  return (
    <div className="card pad workflow-process-panel">
      <div className="row-split" style={{ marginBottom: 10 }}>
        <b className="small" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {running ? <span className="spinner-sm" /> : <Icon name="i-check" style={{ color: 'var(--green)' }} />}
          Workflow Process
        </b>
        {cancelled && <span className="chip amber">Stopped</span>}
      </div>
      {rows.map((r) => {
        const meta = NODE_TYPES[r.node_type] || { label: r.node_type.toUpperCase(), icon: 'i-cog', dot: 'var(--slate)' };
        return (
          <div key={r.node_id} className="workflow-process-row">
            <span className="workflow-process-icon" style={{ background: meta.dot }}><Icon name={meta.icon} /></span>
            <span className="small" style={{ flex: 1, fontWeight: 700 }}>{meta.label}{meta.sub ? <span className="small muted" style={{ fontWeight: 400 }}> · {meta.sub}</span> : ''}</span>
            {r.status === 'running' && <span className="small" style={{ color: 'var(--blue)', display: 'flex', alignItems: 'center', gap: 6 }}>Running <span className="spinner-sm" /></span>}
            {r.status === 'ok' && <Icon name="i-check" style={{ color: 'var(--green)' }} />}
            {r.status === 'skipped' && <span className="small muted">skipped</span>}
            {r.status === 'paused' && <span className="chip amber">paused</span>}
            {(r.status === 'error' || r.status === 'harness_fail' || r.status === 'ucs_denied') && <Icon name="i-x" style={{ color: 'var(--red)' }} />}
          </div>
        );
      })}
    </div>
  );
}

function JsonOutputPanel({ value }: { value: any }) {
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(value, null, 2);
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => {});
  };
  return (
    <div className="card pad json-output-panel">
      <div className="row-split" style={{ marginBottom: 10 }}>
        <b className="small" style={{ textTransform: 'uppercase', letterSpacing: '.03em' }}>JSON Output</b>
        <button className="btn sm ghost" onClick={copy}><Icon name="i-copy" /> {copied ? 'Copied' : 'Copy'}</button>
      </div>
      {/* Capped and scrolled like any other long value: wrapping it
          fixed the sideways scroll but let it grow to 5,500px, which
          just moved the problem from one axis to the other. */}
      <ScrollValue className="codeblock" maxHeight={320}>{text}</ScrollValue>
    </div>
  );
}

// Mirrors the server's own cap in routers/execution.py's batch_run, so
// the UI refuses an oversized selection before uploading any of it.
const BATCH_ROW_CAP = 200;

export function FormRunner({ agent, appType, busy, setBusy, invokingUser = 'user:end-user', preview }:
  { agent: Agent; appType: string; busy: boolean; setBusy: (b: boolean) => void; invokingUser?: string; preview?: boolean }) {
  const triggerNode = agent.flow?.nodes?.find((n) => n.type === 'trigger');
  const fields: InputField[] = (triggerNode?.config?.fields || []).filter((f: InputField) => !f.hidden_prefilled);
  const hasRealFields = fields.length > 0;
  const canBatch = appType === 'workflow' && hasRealFields;

  const [input, setInput] = useState('');
  const [fieldValues, setFieldValues] = useState<Record<string, any>>({});
  const [result, setResult] = useState<{
    output: string | null; status: string; fail_class: string | null;
    structured_output?: Record<string, string> | null; run_id?: string | null;
    // Carried so a failure can name the step that failed and repeat its
    // message, instead of only the fail class.
    spans?: { node?: string; status?: string; error?: string }[] | null;
    // A run that stopped at a HITL node is WAITING, not broken. Without
    // these the form runner showed "Failed (paused_for_human_input)"
    // and offered no way to answer.
    hitl_request_id?: string | null;
    hitl_rendered_content?: string | null;
    hitl_fields?: { name: string; label?: string; required?: boolean }[] | null;
    hitl_actions?: { id: string; title: string; style?: string }[] | null;
  } | null>(null);
  const [formHitlValues, setFormHitlValues] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const flashMsg = (m: string) => { setFlash(m); setTimeout(() => setFlash(null), 2500); };

  // Real live workflow-run state — populated entirely from real
  // node_start/node_done SSE events, see WorkflowProcessPanel above.
  const [workflowEvents, setWorkflowEvents] = useState<StreamEvent[]>([]);
  const [workflowCancelled, setWorkflowCancelled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const isWorkflowLive = appType === 'workflow';

  const [mode, setMode] = useState<'single' | 'batch'>('single');
  const [batchRows, setBatchRows] = useState<Record<string, string>[]>([]);
  const [batchFileName, setBatchFileName] = useState<string | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchResult, setBatchResult] = useState<BatchRunResult | null>(null);
  const effectiveMode = canBatch ? mode : 'single';
  const fieldNames = fields.map((f) => f.name);

  // A file field cannot travel in a CSV cell. The batch tab offered a
  // CSV template regardless, so a workflow whose only input is a file
  // sent you in a circle: the template asked for `document`, and there
  // was nothing you could type into it that the Doc Extractor could
  // fetch. Batching by file is its own input mode, not a CSV column.
  const isFileField = (f: InputField) => f.type === 'single_file' || f.type === 'file_list';
  const fileFields = fields.filter(isFileField);
  // One `single_file` field and nothing else is the case where "each
  // file is one row" is unambiguous, so that is where the picker
  // replaces the CSV entirely. A `file_list` field wants an array per
  // row, and a file field mixed with text fields still needs the CSV to
  // carry the text — both keep the CSV path plus the URL helper below.
  const batchByFile = fileFields.length === 1 && fields.length === 1
    && fileFields[0].type === 'single_file';
  const fileColumns = batchByFile ? [] : fileFields.map((f) => f.name);
  const [batchLabels, setBatchLabels] = useState<string[]>([]);
  const [batchUploading, setBatchUploading] = useState(false);
  const [urlHelper, setUrlHelper] = useState<UploadedFileRef | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);

  // Each picked file is uploaded, and its download URL becomes one row.
  // This is the step the CSV path has no way to perform.
  //
  // Selections ACCUMULATE. A file picker can only ever return one
  // dialog's worth of files, and most file dialogs cannot select across
  // folders, so replacing the list on every change made it impossible
  // to assemble a batch from more than one place — each pick silently
  // discarded the last. Files are added, and removed explicitly below.
  const onBatchFiles = (list: File[]) => {
    if (list.length === 0) return;
    setBatchError(null);
    setBatchResult(null);
    setBatchFileName(null);
    const total = batchRows.length + list.length;
    if (total > BATCH_ROW_CAP) {
      setBatchError(
        `That would make ${total} files — a batch is capped at ${BATCH_ROW_CAP} rows. `
        + `${batchRows.length} already added, so you can add ${BATCH_ROW_CAP - batchRows.length} more.`,
      );
      return;
    }
    setBatchUploading(true);
    Promise.all(list.map((f) => api.uploadFile(f)))
      .then((uploaded) => {
        setBatchRows((prev) => [...prev, ...uploaded.map((u) => ({ [fileFields[0].name]: u.url }))]);
        setBatchLabels((prev) => [...prev, ...list.map((f) => f.name)]);
      })
      .catch((e) => setBatchError(`Upload failed — ${e.message}`))
      .finally(() => setBatchUploading(false));
  };

  const removeBatchFile = (index: number) => {
    setBatchResult(null);
    setBatchRows((prev) => prev.filter((_, i) => i !== index));
    setBatchLabels((prev) => prev.filter((_, i) => i !== index));
  };

  const clearBatchFiles = () => {
    setBatchResult(null);
    setBatchError(null);
    setBatchRows([]);
    setBatchLabels([]);
  };

  const onUrlHelperFile = (picked: File | null) => {
    if (!picked) return;
    setUrlHelper(null);
    setUrlCopied(false);
    api.uploadFile(picked)
      .then(setUrlHelper)
      .catch((e) => setBatchError(`Upload failed — ${e.message}`));
  };

  const setField = (name: string, value: any) => setFieldValues((v) => ({ ...v, [name]: value }));

  // Real per-field upload state — a file field's value only becomes a
  // real, fetchable {filename, url, ...} reference (or an array of
  // them for file_list) once the real upload genuinely completes; see
  // api.ts's uploadFile and routers/uploads.py.
  const [uploadingFields, setUploadingFields] = useState<Record<string, boolean>>({});
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>({});
  const handleFileSelect = (fieldName: string, multiple: boolean, files: FileList | null) => {
    const list = Array.from(files || []);
    if (list.length === 0) return;
    setUploadingFields((u) => ({ ...u, [fieldName]: true }));
    setUploadErrors((e) => ({ ...e, [fieldName]: '' }));
    Promise.all(list.map((f) => api.uploadFile(f)))
      .then((uploaded) => setField(fieldName, multiple ? uploaded : uploaded[0]))
      .catch((e) => setUploadErrors((prev) => ({ ...prev, [fieldName]: e.message })))
      .finally(() => setUploadingFields((u) => ({ ...u, [fieldName]: false })));
  };

  const missingRequired = hasRealFields
    ? fields.filter((f) => f.required && (fieldValues[f.name] === undefined || fieldValues[f.name] === ''))
    : [];
  const canRun = hasRealFields ? missingRequired.length === 0 : !!input.trim();

  const run = () => {
    if (!canRun || busy) return;
    setBusy(true);
    setResult(null);
    setSaved(false);
    const allInputs: Record<string, any> = { ...fieldValues };
    (triggerNode?.config?.fields || []).filter((f: InputField) => f.hidden_prefilled).forEach((f: InputField) => { allInputs[f.name] = f.default; });
    const summaryInput = hasRealFields
      ? Object.entries(allInputs).map(([k, v]) => `${k}: ${v}`).join('\n')
      : input.trim();

    if (isWorkflowLive) {
      setWorkflowEvents([]);
      setWorkflowCancelled(false);
      const controller = new AbortController();
      abortRef.current = controller;
      api.streamAgentRun(agent.id, summaryInput, invokingUser, hasRealFields ? allInputs : undefined,
        (evt) => {
          setWorkflowEvents((prev) => [...prev, evt]);
          if (evt.type === 'complete') {
            // run_id carried through so Save can cite the real run: the
            // backend then derives what it stores from that Run and its
            // Trace instead of trusting what this client reports.
            setResult({
              output: evt.output ?? null, status: evt.status || 'success',
              fail_class: evt.fail_class ?? null, structured_output: evt.structured_output,
              run_id: evt.run_id ?? null,
              spans: (evt as { spans?: { node?: string; status?: string; error?: string }[] }).spans,
              // A streamed run can end paused just as a synchronous one
              // can, so the same details have to come through here.
              hitl_request_id: (evt as { hitl_request_id?: string }).hitl_request_id ?? null,
              hitl_rendered_content: (evt as { hitl_rendered_content?: string }).hitl_rendered_content ?? null,
              hitl_fields: (evt as { hitl_fields?: never }).hitl_fields ?? null,
              hitl_actions: (evt as { hitl_actions?: never }).hitl_actions ?? null,
            });
          } else if (evt.type === 'error') {
            setResult({ output: null, status: 'failed', fail_class: evt.message });
          } else if (evt.type === 'cancelled') {
            setWorkflowCancelled(true);
          }
        },
        controller.signal, preview)
        .catch((e) => { if (!controller.signal.aborted) setResult({ output: null, status: 'failed', fail_class: e.message }); })
        .finally(() => { setBusy(false); abortRef.current = null; });
      return;
    }

    api.runAgent(agent.id, summaryInput, invokingUser, hasRealFields ? allInputs : undefined, preview)
      .then((r) => setResult({
        output: r.output, status: r.status, fail_class: r.fail_class,
        structured_output: r.structured_output, spans: r.spans,
        hitl_request_id: (r as { hitl_request_id?: string }).hitl_request_id ?? null,
        hitl_rendered_content: (r as { hitl_rendered_content?: string }).hitl_rendered_content ?? null,
        hitl_fields: (r as { hitl_fields?: never }).hitl_fields ?? null,
        hitl_actions: (r as { hitl_actions?: never }).hitl_actions ?? null,
      }))
      .catch((e) => setResult({ output: null, status: 'failed', fail_class: e.message }))
      .finally(() => setBusy(false));
  };

  const stopRun = () => {
    // A real abort of the browser's own connection to /run/stream — the
    // backend detects the disconnect and genuinely stops the traversal
    // at the next node boundary (see run_agent_stream's docstring); it
    // isn't purely cosmetic on this end either, but a node already
    // mid-flight (an LLM/HTTP call already sent) still completes there.
    abortRef.current?.abort();
    setWorkflowCancelled(true);
    setBusy(false);
  };

  const downloadTemplate = () => downloadCsv(`${agent.name.replace(/[^a-z0-9]+/gi, '_')}_batch_template.csv`, fieldNames, []);

  const onBatchFile = (file: File | null) => {
    if (!file) return;
    setBatchFileName(file.name);
    setBatchError(null);
    setBatchResult(null);
    file.text().then((text) => {
      const { headers, rows } = parseCsv(text);
      const missingCols = fieldNames.filter((n) => !headers.includes(n));
      if (missingCols.length > 0) {
        setBatchError(`This CSV is missing column(s): ${missingCols.join(', ')} — download the real template above to get the exact columns this workflow needs.`);
        setBatchRows([]);
      } else if (rows.length === 0) {
        setBatchError('No data rows found in this CSV.');
        setBatchRows([]);
      } else {
        setBatchRows(rows);
      }
    }).catch((e) => setBatchError(`Could not read this file: ${e.message}`));
  };

  const runBatch = () => {
    if (batchRows.length === 0 || busy) return;
    const payloadRows = batchRows.map((r) => Object.fromEntries(fieldNames.map((n) => [n, r[n] || ''])));
    setBusy(true);
    setBatchResult(null);
    setBatchError(null);
    api.batchRun(agent.id, payloadRows, invokingUser)
      .then(setBatchResult)
      .catch((e) => setBatchError(e.message))
      .finally(() => setBusy(false));
  };

  const downloadResults = () => {
    if (!batchResult) return;
    const outputCols = [...new Set(batchResult.results.flatMap((r) => (r.structured_output ? Object.keys(r.structured_output) : ['output'])))];
    const headers = [...fieldNames, 'status', ...outputCols, 'error'];
    const rows = batchResult.results.map((r) => ({ ...r.inputs, status: r.status, ...(r.structured_output || { output: r.output || '' }), error: formatRowError(r.error) }));
    downloadCsv(`${agent.name.replace(/[^a-z0-9]+/gi, '_')}_batch_results.csv`, headers, rows);
  };

  const copyResult = () => {
    if (!result) return;
    const text = result.structured_output
      ? Object.entries(result.structured_output).map(([k, v]) => `${k}: ${v}`).join('\n')
      : (result.output || '');
    navigator.clipboard.writeText(text).then(
      () => flashMsg('Copied to clipboard'),
      () => flashMsg('Could not copy — your browser blocked clipboard access'),
    );
  };

  const saveResult = () => {
    if (!result) return;
    const savedInput = hasRealFields ? Object.entries(fieldValues).map(([k, v]) => `${k}: ${v}`).join('\n') : input.trim();
    // The user's own copy: a JSON file named after the agent, saved by the
    // browser to their machine (usually Downloads). Written first and
    // independently, so a failed server save still leaves them the file.
    const savedAt = new Date();
    const stamp = savedAt.toISOString().slice(0, 19).replace(/[:T]/g, '-');
    downloadJson(`${agent.name.replace(/[^a-z0-9]+/gi, '_').replace(/^_+|_+$/g, '')}_result_${stamp}.json`, {
      agent: { id: agent.id, name: agent.name, version: agent.version },
      run_id: result.run_id ?? null,
      status: result.status,
      saved_at: savedAt.toISOString(),
      input: hasRealFields ? fieldValues : input.trim(),
      output: result.output,
      structured_output: result.structured_output ?? null,
    });
    api.saveResult(agent.id, {
      invoking_user: invokingUser, run_id: result.run_id,
      // Sent only so a run without a run_id (a stream that never
      // reported one) can still be saved manually. When run_id is
      // present the backend ignores both and uses the trusted records.
      input: savedInput, output: result.output, structured_output: result.structured_output,
    }).then(() => { setSaved(true); flashMsg('Result saved — JSON file downloaded'); })
      .catch((e) => { setSaved(true); flashMsg(`JSON file downloaded; saving to the server failed — ${e.message}`); });
  };

  // One condition for both the second column and the two-column grid,
  // so the layout cannot reserve a track that nothing renders into.
  const showProcessPanel = isWorkflowLive && effectiveMode === 'single' && (workflowEvents.length > 0 || busy);

  return (
    <div className={`form-runner${showProcessPanel ? ' form-runner-split' : ''}`}>
      <div>
        {canBatch && (
          <div className="modechip" style={{ marginBottom: 14 }}>
            <button className={effectiveMode === 'single' ? 'on' : ''} onClick={() => setMode('single')}>Single Execution</button>
            <button className={effectiveMode === 'batch' ? 'on' : ''} onClick={() => setMode('batch')}>Run Batch</button>
          </div>
        )}
        {effectiveMode === 'batch' && canBatch ? (
          <div className="batch-runner">
            {batchByFile ? (
              <>
                <p className="small muted" style={{ marginTop: 0 }}>
                  This workflow's only input is a file, so there is no CSV to fill in —
                  pick as many files as you want and <b>each one becomes a real, independent run</b>,
                  executed in parallel.
                </p>
                <div className="field" style={{ marginTop: 12 }}>
                  <label>Add files to run ({fileFields[0].name})</label>
                  <input type="file" multiple disabled={batchUploading}
                    onChange={(e) => {
                      // Copy the FileList before clearing the input: the
                      // list is live, so resetting value first would hand
                      // onBatchFiles an empty selection.
                      const list = Array.from(e.target.files || []);
                      // Without this reset, picking the SAME file again
                      // fires no change event at all, because the input's
                      // value has not changed.
                      e.target.value = '';
                      onBatchFiles(list);
                    }} />
                  <p className="small muted" style={{ marginTop: 6, marginBottom: 0 }}>
                    Pick as many as you like, as many times as you like — each selection is
                    added to the list, up to {BATCH_ROW_CAP} files.
                  </p>
                </div>
                {batchUploading && <p className="small muted">Uploading…</p>}
                {batchRows.length > 0 && (
                  <div className="card pad" style={{ marginTop: 12 }}>
                    <div className="row-split" style={{ marginBottom: 8 }}>
                      <b className="small">
                        {batchRows.length} file{batchRows.length === 1 ? '' : 's'} queued
                      </b>
                      <button className="icon-action" title="Clear all" aria-label="Clear all"
                        onClick={clearBatchFiles} disabled={busy}>
                        <Icon name="i-x" />
                      </button>
                    </div>
                    <ul className="batch-file-list">
                      {batchLabels.map((name, i) => (
                        <li key={`${name}-${i}`}>
                          <span className="mono small">{i + 1}.</span>
                          <span className="batch-file-name">{name}</span>
                          <button className="icon-action danger" title={`Remove ${name}`}
                            aria-label={`Remove ${name}`}
                            onClick={() => removeBatchFile(i)} disabled={busy}><Icon name="i-trash" /></button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <>
                <p className="small muted" style={{ marginTop: 0 }}>Each CSV row becomes a real, independent run of this workflow — genuinely executed in parallel, not simulated.</p>
                <button className="btn sm ghost" onClick={downloadTemplate}><Icon name="i-download" /> Download CSV template</button>
                {fileColumns.length > 0 && (
                  <div className="banner" style={{ marginTop: 10 }}>
                    <div>
                      <b>{fileColumns.join(', ')}</b> {fileColumns.length === 1 ? 'is a file field' : 'are file fields'},
                      so {fileColumns.length === 1 ? 'that column needs' : 'those columns need'} a fetchable
                      URL — a filename on its own is not something this workflow can open.
                      Upload a file here to get its URL, then paste it into the CSV.
                      <div className="field" style={{ marginTop: 8, marginBottom: 0 }}>
                        <input type="file" onChange={(e) => onUrlHelperFile(e.target.files?.[0] || null)} />
                      </div>
                      {urlHelper && (
                        <div className="row-split" style={{ marginTop: 8, gap: 8 }}>
                          <span className="mono small" style={{ overflowWrap: 'anywhere' }}>{urlHelper.url}</span>
                          <button className="btn sm ghost" style={{ flex: 'none' }}
                            onClick={() => navigator.clipboard.writeText(urlHelper.url).then(
                              () => { setUrlCopied(true); setTimeout(() => setUrlCopied(false), 1500); },
                              () => {})}>
                            <Icon name="i-copy" /> {urlCopied ? 'Copied' : 'Copy'}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )}
                <div className="field" style={{ marginTop: 12 }}>
                  <label>Upload completed CSV</label>
                  <input type="file" accept=".csv" onChange={(e) => onBatchFile(e.target.files?.[0] || null)} />
                </div>
                {batchFileName && !batchError && batchRows.length > 0 && (
                  <p className="small muted">{batchFileName}: {batchRows.length} row(s) ready to run.</p>
                )}
              </>
            )}
            {batchError && <div className="banner warn" style={{ marginTop: 8 }}>{batchError}</div>}
            <button className="btn" style={{ marginTop: 8 }} onClick={runBatch} disabled={busy || batchRows.length === 0}>
              <Icon name="i-play" /> {busy ? 'Running batch…' : `Run batch (${batchRows.length} row${batchRows.length === 1 ? '' : 's'})`}
            </button>
            {batchResult && (
              <div className="card pad" style={{ marginTop: 20 }}>
                <div className="row-split" style={{ marginBottom: 10 }}>
                  <div className="small">
                    <b>{batchResult.total}</b> total · <span style={{ color: 'var(--green)' }}>{batchResult.succeeded} succeeded</span>
                    {batchResult.failed > 0 && <> · <span style={{ color: 'var(--red)' }}>{batchResult.failed} failed</span></>}
                    {batchResult.paused > 0 && <> · <span style={{ color: 'var(--amber)' }}>{batchResult.paused} paused for human input</span></>}
                  </div>
                  <button className="btn sm ghost" onClick={downloadResults}><Icon name="i-download" /> Download results CSV</button>
                </div>
                {/* A row's output can be an entire extracted document —
                    tens of thousands of characters with long unbroken
                    runs. Rendered raw, one row pushed the table wider
                    than the pane and buried every other row below a
                    screen of text. Each cell is capped and scrolls
                    inside itself, with the scrollbar hidden and a fade
                    standing in for it, exactly as a single run's
                    structured output already does. */}
                <table className="batch-results-table fixed">
                  <thead><tr><th>#</th><th>Input</th><th>Status</th><th>Output</th></tr></thead>
                  <tbody>
                    {batchResult.results.map((row) => {
                      const label = batchLabels[row.row_index]
                        || Object.values(row.inputs || {}).map(String).join(' · ');
                      return (
                        <tr key={row.row_index}>
                          <td className="mono small">{row.row_index + 1}</td>
                          <td className="small batch-cell-input" title={label}>{label || '—'}</td>
                          <td><span className={`chip ${row.status === 'success' ? 'green' : row.status === 'paused_for_human_input' ? 'amber' : 'red'}`}>
                            {row.status === 'success' ? 'success' : row.status === 'paused_for_human_input' ? 'paused' : 'failed'}
                          </span></td>
                          <td className="small">
                            <ScrollValue maxHeight={180}>
                              {formatRowError(row.error) || (row.structured_output ? Object.entries(row.structured_output).map(([k, v]) => `${k}: ${v}`).join('\n') : row.output) || '—'}
                            </ScrollValue>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <>
            {hasRealFields ? (
              fields.map((f) => (
                <div className="field" key={f.id}>
                  <label>{f.name}{f.required && <span style={{ color: 'var(--red)' }}> *</span>}</label>
                  {f.type === 'paragraph' && <textarea rows={4} value={fieldValues[f.name] || ''} onChange={(e) => setField(f.name, e.target.value)} />}
                  {f.type === 'short_text' && <input value={fieldValues[f.name] || ''} onChange={(e) => setField(f.name, e.target.value)} />}
                  {f.type === 'number' && <input type="number" value={fieldValues[f.name] ?? ''} onChange={(e) => setField(f.name, e.target.value)} />}
                  {f.type === 'checkbox' && (
                    <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
                      <span className={`check-box${fieldValues[f.name] ? ' on' : ''}`} onClick={() => setField(f.name, !fieldValues[f.name])} style={{ cursor: 'pointer' }}>
                        {fieldValues[f.name] && <Icon name="i-check" />}
                      </span>
                    </div>
                  )}
                  {f.type === 'select' && (
                    <SearchSelect ariaLabel={f.name} noun="option" placeholder="Select…"
                      value={fieldValues[f.name] || ''}
                      options={(f.options || []).map((o) => ({ value: o, label: o }))}
                      onChange={(v) => setField(f.name, v)} />
                  )}
                  {(f.type === 'single_file' || f.type === 'file_list') && (
                    <div>
                      <input type="file" multiple={f.type === 'file_list'} disabled={!!uploadingFields[f.name]}
                        onChange={(e) => handleFileSelect(f.name, f.type === 'file_list', e.target.files)} />
                      {uploadingFields[f.name] && <span className="small muted"> Uploading…</span>}
                      {uploadErrors[f.name] && <div className="small" style={{ color: 'var(--red)' }}>{uploadErrors[f.name]}</div>}
                      {!uploadingFields[f.name] && fieldValues[f.name] && (
                        <div className="small muted" style={{ marginTop: 4 }}>
                          {f.type === 'file_list'
                            ? (fieldValues[f.name] as { filename: string }[]).map((u) => u.filename).join(', ')
                            : (fieldValues[f.name] as { filename: string }).filename}
                          {' '}uploaded
                        </div>
                      )}
                    </div>
                  )}
                  {f.type === 'json_code' && <textarea rows={4} className="mono" placeholder="{}" value={fieldValues[f.name] || ''} onChange={(e) => setField(f.name, e.target.value)} />}
                </div>
              ))
            ) : (
              <div className="field">
                <label>{appType === 'workflow' ? 'Input' : 'Prompt'}</label>
                <textarea rows={5} placeholder={(agent?.features?.webapp_settings?.input_placeholder || '').trim() || (appType === 'workflow' ? 'Provide the input for this workflow…' : 'Describe what you want generated…')} value={input} onChange={(e) => setInput(e.target.value)} />
              </div>
            )}
            <div className="row-split" style={{ justifyContent: 'flex-start', gap: 10 }}>
              {isWorkflowLive && busy ? (
                <button className="btn ghost" onClick={stopRun}><Icon name="i-x" /> Stop Run</button>
              ) : (
                <button className="btn" onClick={run} disabled={busy || !canRun}>
                  <Icon name="i-play" /> {busy ? 'Running…' : appType === 'workflow' ? (isWorkflowLive ? 'Execute' : 'Run workflow') : 'Generate'}
                </button>
              )}
            </div>
            {result && (
              <div className="card pad" style={{ marginTop: 20 }}>
                {result.status === 'success' ? (
                  <>
                    {result.structured_output && Object.keys(result.structured_output).length > 0 ? (
                      <div className="structured-output">
                        {Object.entries(result.structured_output).map(([k, v]) => (
                          <div className="structured-output-row" key={k}>
                            <div className="small muted" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5 }}>{k}</div>
                            <ScrollValue>{v}</ScrollValue>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div style={{ whiteSpace: 'pre-wrap' }}>{result.output}</div>
                    )}
                    <div className="row-split" style={{ marginTop: 16, justifyContent: 'flex-start', gap: 8, alignItems: 'center' }}>
                      <button className="btn sm ghost" onClick={copyResult}><Icon name="i-copy" /> Copy</button>
                      <button className="btn sm ghost" onClick={saveResult} disabled={saved}>
                        <Icon name="i-check" /> {saved ? 'Saved' : 'Save'}
                      </button>
                      {flash && <span className="small muted">{flash}</span>}
                    </div>
                  </>
                ) : result.status === 'paused_for_human_input' ? (
                  // Waiting on a person is not a failure. It used to
                  // render as "Failed (paused_for_human_input)" with no
                  // way to answer, so the run could only be completed
                  // from somewhere else.
                  <div className="hitl-form">
                    <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
                      <span className="chip amber">awaiting approval</span>
                      <span className="small muted">This run is paused until someone responds.</span>
                    </div>
                    {result.hitl_rendered_content
                      ? <div className="chat-bubble-text" style={{ whiteSpace: 'pre-wrap', marginTop: 10 }}>
                          {result.hitl_rendered_content}
                        </div>
                      : <p className="small muted" style={{ marginTop: 10 }}>
                          This approval step has no message configured — set
                          <b> Form content</b> on the Human Input node so the person
                          deciding can see what they are approving.
                        </p>}
                    {(result.hitl_fields || []).map((f) => (
                      <div className="field" key={f.name}>
                        <label>{f.label || f.name}{f.required ? ' *' : ''}</label>
                        <input value={formHitlValues[f.name] || ''}
                          onChange={(e) => setFormHitlValues((v) => ({ ...v, [f.name]: e.target.value }))} />
                      </div>
                    ))}
                    <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8, marginTop: 10 }}>
                      {(result.hitl_actions || []).map((a) => (
                        <button key={a.id} className={`btn sm${a.style === 'primary' ? '' : ' ghost'}`}
                          disabled={busy}
                          onClick={() => {
                            if (!result.hitl_request_id) return;
                            setBusy(true);
                            api.respondToHitl(agent.id, result.hitl_request_id, a.id, formHitlValues)
                              .then((r) => setResult({
                                output: r.output ?? null, status: r.status,
                                fail_class: r.fail_class ?? null,
                                structured_output: r.structured_output,
                                spans: r.spans,
                                hitl_request_id: r.hitl_request_id ?? null,
                                hitl_rendered_content: r.hitl_rendered_content ?? null,
                                hitl_fields: r.hitl_fields ?? null,
                                hitl_actions: r.hitl_actions ?? null,
                              }))
                              .catch((e) => setResult({ output: null, status: 'failed', fail_class: e.message }))
                              .finally(() => setBusy(false));
                          }}>
                          {a.title}
                        </button>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="small" style={{ color: 'var(--red)' }}>
                    {isWorkflowLive && workflowCancelled
                      ? 'Run stopped before completing.'
                      : (() => {
                          // The node that failed usually says exactly what
                          // was wrong; "failed: orchestration" says only
                          // that something did. Showing the class alone
                          // sent people to the trace to read a message the
                          // run already had.
                          const failed = (result.spans || []).find((sp) => sp.status === 'error' && sp.error);
                          return (
                            <>
                              <b>{failed ? `${failed.node} step failed` : `Failed (${result.status})`}</b>
                              {failed?.error && (
                                <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>{failed.error}</div>
                              )}
                              <div className="muted" style={{ marginTop: 4 }}>
                                {result.fail_class ? `fail class: ${result.fail_class}` : ''}
                              </div>
                            </>
                          );
                        })()}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
      {showProcessPanel && (
        <div>
          <WorkflowProcessPanel events={workflowEvents} running={busy} cancelled={workflowCancelled} />
          {!busy && result && result.status === 'success' && (
            <JsonOutputPanel value={result.structured_output && Object.keys(result.structured_output).length > 0 ? result.structured_output : { output: result.output }} />
          )}
        </div>
      )}
    </div>
  );
}
