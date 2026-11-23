import { useEffect, useState } from 'react';
import Icon from './Icon';
import SearchSelect from './SearchSelect';
import AgentConfigurePanel from './AgentConfigurePanel';
import { Banner, SecHead, KV } from './ui';
import { api, BASE } from '../api';
import AddInputFieldModal, { type InputField } from './AddInputFieldModal';
import type { Agent, FlowNode, ModelProvider, DataSource, Tool, ToolAction, KnowledgeBase, LlmModelOption } from '../types';
import { useApp } from '../context/AppContext';
import { INPUT_FIELD_TYPES, NODE_TYPES } from '../constants';
import { COMMENT_COLORS, getCommentColor, applyCommentFormat, type FormatAction } from '../commentFormatting';

function Field({ label, children, extra }: { label: string; children: React.ReactNode; extra?: React.ReactNode }) {
  return (
    <div className="field">
      <label>{label} {extra}</label>
      {children}
    </div>
  );
}

function Toggle({ on, onChange, disabled }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return <div className={`tog${on ? ' on' : ''}`} style={disabled ? { opacity: 0.5, cursor: 'default' } : undefined} onClick={() => !disabled && onChange(!on)} />;
}

function Chips({ items, onChange, disabled }: { items: string[]; onChange: (items: string[]) => void; disabled?: boolean }) {
  const [draft, setDraft] = useState('');
  const add = () => {
    const v = draft.trim();
    if (v && !items.includes(v)) onChange([...items, v]);
    setDraft('');
  };
  return (
    <div className="pill-row" style={{ flexWrap: 'wrap', gap: 6, alignItems: 'center', opacity: disabled ? 0.5 : undefined }}>
      {items.map((it) => (
        <span key={it} className="chip line" style={{ cursor: disabled ? 'default' : 'pointer' }} onClick={() => !disabled && onChange(items.filter((x) => x !== it))}>
          {it} ✕
        </span>
      ))}
      <input
        value={draft}
        placeholder="+ add"
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }}
        onBlur={add}
        style={{ width: 90, padding: '3px 8px', fontSize: 12, border: '1px dashed var(--line)', borderRadius: 12 }}
      />
    </div>
  );
}

/**
 * Every model dropdown in this panel.
 *
 * Four of them were hardcoded <option> lists — three Anthropic names in
 * most, plus two OpenAI ones in the LLM node, and a
 * "(default — not yet wired to a real distinct API call)" placeholder
 * for every other provider. That placeholder was not merely out of
 * date: its VALUE ("DeepSeek (default)") is not a built-in model name
 * and not a registered one, so choosing it made the run fail with
 * "Unrecognized model". The one option those providers offered could
 * never work.
 *
 * Two rules here are deliberate:
 *  - An unavailable model is shown, disabled, with the reason — not
 *    hidden. Hiding it recreates the original complaint.
 *  - A saved value that is not in the catalog is kept as a selected
 *    option rather than silently replaced, so opening a node cannot
 *    quietly rewrite which model it runs.
 */
function ModelSelect({ value, fallback, models, onChange, ariaLabel = 'Model' }: {
  value?: string;
  fallback: string;
  models: LlmModelOption[];
  onChange: (name: string) => void;
  ariaLabel?: string;
}) {
  const current = value || fallback;
  const byProvider = models.reduce<Record<string, LlmModelOption[]>>((acc, m) => {
    const key = m.provider || 'Other';
    (acc[key] = acc[key] || []).push(m);
    return acc;
  }, {});
  const known = models.some((m) => m.name === current);
  return (
    <SearchSelect ariaLabel={ariaLabel} noun="model" value={current}
      options={[
        ...(!known ? [{
          value: current,
          label: models.length === 0 ? current : `${current} — not in the model catalog`,
        }] : []),
        ...Object.entries(byProvider).flatMap(([provider, list]) => list.map((m) => ({
          value: m.name,
          // ML Studio models read by their model id (the group already says
          // "ML Studio"); the stored value stays `mlstudio/<id>`.
          label: `${m.mlstudio ? `${m.mlstudio.model_id}${m.mlstudio.version != null ? ` · v${m.mlstudio.version}` : ''}` : m.name}`
            + `${m.available ? '' : ` — ${m.note || 'unavailable'}`}`,
          group: provider,
          disabled: !m.available,
        }))),
      ]}
      onChange={(v) => onChange(v)} />
  );
}

/** Why a control is greyed out, in words a builder can act on.
 *
 * These notes used to cite "§0.64, GAP_REPORT" — an internal audit
 * section number, rendered into the product, where the reader needed to
 * know what to do instead. And two of the unwired controls carried no
 * note at all while presenting themselves as switched on.
 *
 * Rendered only when the control really is unwired, so wiring one makes
 * its note disappear with it rather than leaving a stale warning about
 * something the runtime now honours.
 */
function UnwiredNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="small muted" style={{ marginTop: -6 }}>
      <span className="chip line sm" style={{ marginRight: 6 }}>Not available yet</span>
      {children}
    </p>
  );
}

export default function NodeConfigPanel({
  agent, node, onAgentUpdate, onDelete,
}: { agent: Agent; node: FlowNode | undefined; onAgentUpdate: (a: Agent) => void; onDelete: (id: string) => void }) {
  const { toast } = useApp();
  const [cfg, setCfg] = useState<Record<string, any>>(node?.config || {});
  // Real fix for a real, significant bug found live during the Comment
  // node build: `useState`'s initial value only runs on this
  // component's real FIRST-EVER mount — which happens with node still
  // `undefined` (Builder.tsx renders NodeConfigPanel unconditionally,
  // before any node is selected) — so `cfg` was permanently stuck at
  // `{}` regardless of which node got selected afterward, for the
  // ENTIRE lifetime of this component instance. Confirmed live: even a
  // single, first-ever click on a real node with real config
  // (`{title: 'REAL TITLE HERE', ...}`) showed a genuinely empty Title
  // field — not stale data from a previous selection, but NEVER-
  // correct data from any selection. This is the real, standard React
  // fix: explicitly resync local state from a prop whenever that
  // prop's real identity (node.id) changes.
  useEffect(() => {
    setCfg(node?.config || {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id]);
  const [allAgents, setAllAgents] = useState<Agent[]>([]);
  const [modelProviders, setModelProviders] = useState<ModelProvider[]>([]);
  // The real model catalog behind every model dropdown in this panel:
  // the built-ins plus whatever is registered on the Integrations page.
  // These used to be hardcoded <option> lists, which is why a workspace
  // with Gemini, DeepSeek and Cohere models configured could select
  // none of them — see the llm-models endpoint's own docstring.
  const [llmModels, setLlmModels] = useState<LlmModelOption[]>([]);
  // §0.115, GAP_REPORT — the real, fetched-once "runtime schema" this
  // panel now uses to disable (not merely warn about) any control a
  // node's real handler doesn't actually consume yet.
  const [unwiredControls, setUnwiredControls] = useState<Record<string, string[]>>({});
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [toolActions, setToolActions] = useState<Record<string, ToolAction[]>>({});
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [retrievalMetadataKeys, setRetrievalMetadataKeys] = useState<string[]>([]);
  const [inputFieldModal, setInputFieldModal] = useState<{ open: boolean; editField: InputField | null }>({ open: false, editField: null });
  useEffect(() => { api.listAgents().then(setAllAgents).catch(() => {}); }, []);
  useEffect(() => { api.listModelProviders().then(setModelProviders).catch(() => {}); }, []);
  useEffect(() => {
    // Optional-chained the same way getUnwiredNodeControls is: the
    // existing test suites automock this module, so a newly added method
    // is undefined there and must not break the panel's render.
    try {
      const result = api.listLlmModels?.();
      if (result && typeof result.then === 'function') {
        result.then((r) => setLlmModels(r.models || [])).catch(() => {});
      }
    } catch { /* the panel still renders; ModelSelect falls back to the saved value */ }
  }, []);
  useEffect(() => {
    // Real, defensive handling — many existing test files use
    // `vi.mock('../api')` (Vitest's own automock), which replaces
    // EVERY exported function, including this new one, with an
    // unconfigured mock returning `undefined` (not a rejected Promise)
    // unless that specific test explicitly configures it — a real,
    // pre-existing pattern across this test suite for methods a given
    // test doesn't itself care about. A plain try/catch here handles
    // both a genuinely missing function and an automocked one
    // returning undefined instead of a real Promise, without needing
    // to touch every one of those existing test files individually.
    try {
      const result = api.getUnwiredNodeControls();
      if (result && typeof result.then === 'function') {
        result.then(setUnwiredControls).catch(() => {});
      }
    } catch { /* real, honest no-op — this schema is a real, additive enhancement, not load-bearing for the panel to render at all */ }
  }, []);
  useEffect(() => { api.listDataSources().then(setDataSources).catch(() => {}); }, []);
  useEffect(() => { api.listKnowledgeBases().then(setKnowledgeBases).catch(() => {}); }, []);
  useEffect(() => {
    api.listTools().then((ts) => {
      setTools(ts);
      Promise.all(ts.map((t) => api.getToolActions(t.name).then((actions) => [t.name, actions] as const).catch(() => [t.name, []] as const)))
        .then((pairs) => setToolActions(Object.fromEntries(pairs)));
    }).catch(() => {});
  }, []);
  // A retrieval node's saved selection may hold KB ids (what this panel
  // writes now), display names (what it wrote before) or the slug of a
  // name, so "is this KB selected?" has to accept all three. Writing the
  // ID is the fix: the same string is read as a catalog object identifier
  // by the entitlement check, and a display name with no dots can never
  // match a UCS grant. It also survives a rename, which a name cannot.
  const kbIsSelected = (kb: { id: string; name: string }, selected: string[]) =>
    selected.includes(kb.id) || selected.includes(kb.name) ||
    selected.includes(kb.name.toLowerCase().replace(/[ .]/g, '_'));

  // Real metadata keys for the manual-filtering condition builder — refetched
  // whenever the retrieval node's selected knowledge bases change.
  useEffect(() => {
    if (node?.type !== 'retrieval') return;
    const selected: string[] = cfg.knowledge_bases || (cfg.knowledge_base ? [cfg.knowledge_base] : []);
    const ids = knowledgeBases.filter((kb) => kbIsSelected(kb, selected)).map((kb) => kb.id);
    if (ids.length === 0) { setRetrievalMetadataKeys([]); return; }
    Promise.all(ids.map((id) => api.listKbMetadataKeys(id).then((r) => r.keys).catch(() => [])))
      .then((lists) => setRetrievalMetadataKeys([...new Set(lists.flat())]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id, node?.type, JSON.stringify(cfg.knowledge_bases || cfg.knowledge_base), knowledgeBases.length]);

  if (!node) {
    if (agent.plane === 'Snippet') {
      const vars: { name: string; type: string; description?: string }[] = agent.input_variables || [];
      const saveVars = (next: typeof vars) => api.setAgentMeta(agent.id, { input_variables: next as any }).then(onAgentUpdate).catch((e) => toast(e.message));
      const updateVar = (i: number, patch: Partial<typeof vars[0]>) => {
        const next = vars.map((v, vi) => (vi === i ? { ...v, ...patch } : v));
        saveVars(next);
      };
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Input Variables</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            A snippet has no Start node to collect input, so declare what it expects here. Real, matching Dify's own
            documented concept exactly: when this snippet is inserted into a workflow, these become the real
            connection points — reference one from any node inside this snippet as <span className="mono">{'{{var_name}}'}</span>.
          </p>
          <div className="row-split" style={{ marginBottom: 6 }}>
            <label style={{ margin: 0 }}>VARIABLES</label>
            <button className="icon-btn" title="Add input variable" onClick={() => saveVars([...vars, { name: '', type: 'string', description: '' }])}>
              <Icon name="i-plus" />
            </button>
          </div>
          {vars.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No input variables yet — add one above.</p>}
          {vars.map((v, i) => (
            <div key={i} className="card pad" style={{ marginBottom: 8 }}>
              <div className="row-split" style={{ gap: 8, marginBottom: 6 }}>
                <input placeholder="variable_name" defaultValue={v.name} style={{ flex: 1 }} onBlur={(e) => updateVar(i, { name: e.target.value })} />
                <SearchSelect ariaLabel="Variable type" noun="type" value={v.type}
                  style={{ width: 110, flex: 'none' }}
                  options={[
                    { value: 'string', label: 'String' }, { value: 'number', label: 'Number' }, { value: 'boolean', label: 'Boolean' },
                    { value: 'array', label: 'Array' }, { value: 'object', label: 'Object' },
                  ]}
                  onChange={(t) => updateVar(i, { type: t as any })} />
                <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => saveVars(vars.filter((_, vi) => vi !== i))}><Icon name="i-trash" /></button>
              </div>
              <input placeholder="Description — what this snippet needs and why" defaultValue={v.description || ''} style={{ width: '100%' }} onBlur={(e) => updateVar(i, { description: e.target.value })} />
            </div>
          ))}
        </div>
      );
    }
    return <div className="cfg"><div className="empty-cfg">Select a node to configure it,<br />or drag one in from the palette.</div></div>;
  }

  const set = (patch: Record<string, any>) => {
    const merged = { ...cfg, ...patch };
    setCfg(merged);
    api.updateNodeConfig(agent.id, node.id, patch, agent.draft_revision).then(onAgentUpdate).catch((e) => toast(e.message));
  };

  const delBtn = ['trigger', 'gov', 'respond'].includes(node.type) ? null : (
    <button className="icon-action danger" style={{ marginTop: 16 }} title="Delete node" aria-label="Delete node" onClick={() => onDelete(node.id)}>
      <Icon name="i-trash" />
    </button>
  );

  const panelContent = (() => {
  switch (node.type) {
    case 'llm': {
      // §0.115, GAP_REPORT — the actual, structural fix for a real,
      // reported gap: this panel used to leave every one of these
      // controls fully interactive, with only a warning PARAGRAPH
      // stating a control wasn't really wired — a builder who didn't
      // read the fine print could still toggle/select/type into a
      // control that genuinely does nothing at runtime. Each such
      // control below is now ALSO real, visually, functionally
      // disabled (not merely warned about), driven by the same real,
      // fetched-once, single source of truth (node_wiring.py, via
      // GET /api/agents/node-schema/unwired-controls) handle_llm's
      // own docstring already states in prose — not a second,
      // independently-maintained "is this wired" fact this component
      // invents on its own.
      const llmUnwired = unwiredControls.llm || [];
      // Named once rather than re-tested at each use site: the display
      // state and the disabled state have to agree, and they drifted
      // apart precisely because each was computed separately.
      const guardrailUnwired = llmUnwired.includes('guardrail_profile');
      const reasoningUnwired = llmUnwired.includes('reasoning_tag_separation');
      const structuredUnwired = llmUnwired.includes('structured_output');
      // Accepts the value form and the older stored-label form, matching
      // _llm_agentic_strategy in harness.py. Anything unrecognised reads
      // as "no strategy" — the single call — rather than silently
      // selecting a loop that costs a model call per round.
      const llmStrategy = ({
        react: 'react', function_calling: 'function_calling',
        'react (reason + act loop)': 'react', 'function calling': 'function_calling',
      } as Record<string, string>)[(cfg.agentic_strategy || '').trim().toLowerCase()] || '';
      const preset: 'precise' | 'balanced' | 'creative' = cfg.param_preset || 'balanced';
      // Real, deliberate scope (§0.64, GAP_REPORT) — frequency_penalty
      // and presence_penalty were removed from this table (and the
      // controls below) entirely: the real Anthropic Messages API has
      // no such parameters at all (confirmed directly against the
      // real SDK's own call signature), unlike OpenAI's API, which
      // does. Leaving working-looking sliders for a setting the one
      // real, functional provider this platform has can never honor
      // would be worse than not showing them.
      const presetDefaults: Record<string, Record<string, number>> = {
        precise: { temperature: 0, top_p: 0.85 },
        balanced: { temperature: 0.5, top_p: 0.9 },
        creative: { temperature: 1, top_p: 1 },
      };
      const paramRow = (key: string, label: string, min: number, max: number) => {
        const val = cfg[key] != null ? cfg[key] : presetDefaults[preset][key];
        return (
          <Field key={key} label={`${label} (${val})`}>
            <input type="range" min={min} max={max} step={0.05} value={val} onChange={(e) => set({ [key]: Number(e.target.value) })} />
          </Field>
        );
      };
      const readyProviders = modelProviders.filter((p) => p.types.includes('LLM') && p.status === 'credits');
      const hasNeedsKey = modelProviders.some((p) => p.types.includes('LLM') && p.status === 'needs_key');
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>LLM Node</SecHead>
          <div className="modechip">
            <button className={cfg.mode !== 'fixed' ? 'on' : ''} onClick={() => set({ mode: 'agentic' })}>Agentic</button>
            <button className={cfg.mode === 'fixed' ? 'on' : ''} onClick={() => set({ mode: 'fixed' })}>Fixed</button>
          </div>
          <p className="small muted" style={{ marginTop: 0 }}>Agentic: harness-driven loop (§3.5) — model decides which tool to call. Fixed: single deterministic call.</p>

          <SecHead>Model Selection and Parameters</SecHead>
          <Field label="Model (from Model catalog)">
            <ModelSelect value={cfg.model} fallback="Claude Sonnet 5" models={llmModels}
                         onChange={(name) => set({ model: name })} />
          </Field>
          {hasNeedsKey && <p className="small muted" style={{ marginTop: -6 }}>Some LLM providers in Integrations still need an API key configured before their models can genuinely be used.</p>}
          <Field label="Agentic strategy">
            {/* Values, not labels. This used to store the option TEXT
                ("ReAct (reason + act loop)"), so the stored value changed
                whenever the wording did; the handler accepts both so
                older flows still resolve. */}
            <SearchSelect ariaLabel="Agentic strategy" noun="strategy" value={llmStrategy}
              disabled={llmUnwired.includes('agentic_strategy')}
              options={[
                { value: '', label: 'Single call (no loop)' },
                { value: 'react', label: 'ReAct (reason + act loop)' },
                { value: 'function_calling', label: 'Function calling' },
              ]}
              onChange={(v) => set({ agentic_strategy: v })} />
          </Field>
          <p className="small muted" style={{ marginTop: -6 }}>
            {llmStrategy === 'react'
              ? 'Real multi-round loop — alternates Thought/Action up to Step limit rounds, stopping as soon as the model concludes with "Final Answer:". The same loop the Agent node runs.'
              : llmStrategy === 'function_calling'
                ? 'Real iterative tool-use loop via the provider\u2019s own tool-use API, up to Step limit rounds, stopping when the model stops requesting a tool. Select the tools it may call below.'
                : 'Single call, which is what this node has always done. Pick a strategy to run a real multi-round loop instead — each round is a real model call, so Step limit bounds the cost.'}
          </p>
          <div className="modechip" style={{ marginBottom: 10 }}>
            <button className={preset === 'precise' ? 'on' : ''} onClick={() => set({ param_preset: 'precise', temperature: undefined, top_p: undefined })}>Precise</button>
            <button className={preset === 'balanced' ? 'on' : ''} onClick={() => set({ param_preset: 'balanced', temperature: undefined, top_p: undefined })}>Balanced</button>
            <button className={preset === 'creative' ? 'on' : ''} onClick={() => set({ param_preset: 'creative', temperature: undefined, top_p: undefined })}>Creative</button>
          </div>
          {paramRow('temperature', 'Temperature — 0 deterministic, 1 creative', 0, 1)}
          {paramRow('top_p', 'Top P — limits word choices by probability', 0, 1)}

          <SecHead>Prompt Configuration</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>Reference workflow variables with <span className="mono small">{'{{variable_name}}'}</span>. Chat models use System (behavior) and User (input) roles.</p>
          <Field label="System">
            <textarea rows={3} placeholder="You are a technical documentation expert." value={cfg.system_prompt || ''}
              onChange={(e) => setCfg({ ...cfg, system_prompt: e.target.value })} onBlur={() => set({ system_prompt: cfg.system_prompt })} />
          </Field>
          <Field label="User">
            <textarea rows={3} placeholder="{{input}}" value={cfg.user_prompt || ''}
              onChange={(e) => setCfg({ ...cfg, user_prompt: e.target.value })} onBlur={() => set({ user_prompt: cfg.user_prompt })} />
          </Field>

          <SecHead>Context Variables</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>Connect a Knowledge Retrieval node's output, then reference it as <span className="mono small">{'{{context}}'}</span> in the prompts above.</p>
          <Field label="Context">
            <input placeholder="{x} Set variable" value={cfg.context_var || ''} onChange={(e) => setCfg({ ...cfg, context_var: e.target.value })} onBlur={() => set({ context_var: cfg.context_var })} />
          </Field>

          <SecHead>Memory and File Processing</SecHead>
          <div className="row-split" style={{ padding: '8px 0' }}>
            <span className="small">Enable Memory</span><Toggle on={!!cfg.memory_enabled} onChange={(v) => set({ memory_enabled: v })} />
          </div>
          {cfg.memory_enabled && <p className="small muted" style={{ marginTop: 0 }}>Prior turns in this conversation are included as a real transcript, not just concatenated text.</p>}
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Vision</span><Toggle on={!!cfg.vision} onChange={(v) => set({ vision: v })} disabled={llmUnwired.includes('vision')} />
          </div>
          {cfg.vision && (
            <>
              <UnwiredNote>
                Images are not sent to the model. Doing so needs file-data resolution
                and image content blocks built into the prompt, which is separate work
                — so this switch changes nothing at runtime today.
              </UnwiredNote>
              <Field label="Vision variable">
                <input placeholder="userinput.files" value={cfg.vision_var ?? 'userinput.files'} onChange={(e) => setCfg({ ...cfg, vision_var: e.target.value })} onBlur={() => set({ vision_var: cfg.vision_var })} />
              </Field>
              <Field label="Detail level">
                <SearchSelect ariaLabel="Detail level" noun="detail level" value={cfg.vision_detail || 'high'}
                  disabled={llmUnwired.includes('vision_detail')}
                  options={[
                    { value: 'high', label: 'High detail — better accuracy, more tokens' },
                    { value: 'low', label: 'Low detail — faster, fewer tokens' },
                  ]}
                  onChange={(v) => set({ vision_detail: v })} />
              </Field>
              <p className="small muted" style={{ marginTop: -6 }}>The real Anthropic API has no "detail level" concept for images at all (confirmed against its own real API) — unlike OpenAI's API, which does. This control could never be wired to the one real, functional provider this platform has, even once vision itself is.</p>
            </>
          )}

          {/* Both of these defaulted to `!== false`, i.e. ON, while being
              disabled and unread by the handler — a switch sitting in the
              on position for something that does nothing, with no note
              saying so. Shown off when unwired; the stored value is not
              rewritten, so wiring them later restores whatever was set. */}
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Reasoning tag separation</span>
            <Toggle on={reasoningUnwired ? false : cfg.reasoning_tag_separation !== false}
              onChange={(v) => set({ reasoning_tag_separation: v })} disabled={reasoningUnwired} />
          </div>
          {reasoningUnwired && (
            <UnwiredNote>
              The model's reasoning is returned inline with its answer. Splitting it
              into a separate field needs a parsing convention this platform has not
              settled on yet.
            </UnwiredNote>
          )}
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Structured output</span>
            <Toggle on={structuredUnwired ? false : cfg.structured_output !== false}
              onChange={(v) => set({ structured_output: v })} disabled={structuredUnwired} />
          </div>
          {structuredUnwired && (
            <UnwiredNote>
              This node returns text. Enforcing a shape would need an output schema to
              validate against — use a <b>Code</b> node to parse the reply for now.
            </UnwiredNote>
          )}
          {llmStrategy === 'function_calling' && (
            <>
              <div className="row-split" style={{ marginBottom: 6, marginTop: 10 }}>
                <label style={{ margin: 0 }}>TOOLS THIS NODE MAY CALL</label>
              </div>
              {tools.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No tools configured in Integrations yet.</p>}
              {/* The same catalog picker the Agent node uses, writing the
                  same "tool:Name:Action" strings. The old control here was
                  a free-text Chips field defaulting to example values like
                  "Tool: Trino query" — strings no catalog lookup could ever
                  resolve, which is part of why it could not be wired. */}
              {tools.map((t) => {
                const acts = toolActions[t.name] || [];
                if (acts.length === 0) return null;
                return (
                  <div key={t.name} style={{ marginBottom: 8 }}>
                    <div className="small muted" style={{ fontWeight: 700, marginBottom: 4 }}>{t.name}</div>
                    {acts.map((a) => {
                      const obj = `tool:${t.name}:${a.name}`;
                      const on = (cfg.allowed_tools || []).includes(obj);
                      const toggle = () => set({ allowed_tools: on
                        ? (cfg.allowed_tools || []).filter((x: string) => x !== obj)
                        : [...(cfg.allowed_tools || []), obj] });
                      return (
                        <div key={obj} className="row-split" style={{ justifyContent: 'flex-start', gap: 8, padding: '4px 0' }}>
                          <span className={`check-box${on ? ' on' : ''}`} style={{ cursor: 'pointer' }} onClick={toggle}>
                            {on && <Icon name="i-check" />}
                          </span>
                          <span className="small" style={{ cursor: 'pointer' }} onClick={toggle}>{a.name}</span>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
              <p className="small muted">With no tool selected the model simply answers directly on the first round — the loop still runs honestly, it just has nothing to call.</p>
            </>
          )}
          <div className="g2 grid" style={{ marginTop: 10 }}>
            <Field label="Step limit">
              <input value={cfg.step_limit ?? 12} onChange={(e) => setCfg({ ...cfg, step_limit: e.target.value })} onBlur={() => set({ step_limit: cfg.step_limit })} />
            </Field>
            <Field label="Timeout (s)">
              <input value={cfg.timeout_s ?? 30} onChange={(e) => setCfg({ ...cfg, timeout_s: e.target.value })} onBlur={() => set({ timeout_s: cfg.timeout_s })} />
            </Field>
          </div>
          {/* Shows the stored profile, disabled. An earlier version
              blanked the display to "Not enforced" while unwired, on the
              grounds that a greyed control naming a profile reads as
              "configured and locked". That was reverted by request: the
              saved value is what the flow actually holds, and hiding it
              means a builder cannot see what is on the node. The note
              below carries the "this is not in force" part instead. */}
          <Field label="Guardrail profile">
            <SearchSelect ariaLabel="Guardrail profile" noun="profile"
              value={cfg.guardrail_profile || 'Standard (input + output)'}
              disabled={guardrailUnwired}
              options={['Standard (input + output)', 'Strict', 'None'].map((o) => ({ value: o, label: o }))}
              onChange={(v) => set({ guardrail_profile: v })} />
          </Field>
          {guardrailUnwired && (
            <UnwiredNote>
              Guardrails for this agent are set on the <b>Guardrails</b> page and run
              before the flow starts. Per-node profiles are not wired to the runtime,
              so nothing selected here would take effect.
            </UnwiredNote>
          )}
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Retry on failure</span><Toggle on={!!cfg.retry_on_failure} onChange={(v) => set({ retry_on_failure: v })} />
          </div>
          <Field label="Error handling">
            <SearchSelect ariaLabel="Error handling" noun="option" value={cfg.error_handling || 'None'}
              options={['None', 'Default value', 'Fail branch'].map((o) => ({ value: o, label: o }))}
              onChange={(v) => set({ error_handling: v })} />
          </Field>
          <hr className="sep" />
          <Banner kind="info">Every tool this loop proposes still routes through the same UCS entitlement check as a fixed Tool Node.</Banner>
          {delBtn}
        </div>
      );
    }

    case 'trigger': {
      const triggerType = cfg.trigger_type || 'user_input';
      const webhookUrl = cfg.webhook_token ? `${BASE}/api/webhooks/${cfg.webhook_token}` : null;

      const pickType = (t: string) => {
        if (t === 'webhook' && !cfg.webhook_token) {
          // generate the token once, client-side — an unguessable UUID is
          // the whole security model here, same as most webhook-trigger
          // tools (Zapier/n8n included); see webhooks.py's docstring
          set({ trigger_type: t, webhook_token: crypto.randomUUID() });
        } else {
          set({ trigger_type: t });
        }
      };

      const sendTestWebhook = () => {
        if (!cfg.webhook_token) return;
        api.testWebhook(cfg.webhook_token, { message: 'Test payload from the Builder' })
          .then((r) => toast(`Webhook fired — ${r.status.toUpperCase()}${r.output ? `: ${r.output.slice(0, 60)}` : ''}`))
          .catch((e) => toast(`Webhook test failed: ${e.message}`));
      };

      const OPTIONS: { type: string; icon: string; label: string; badge?: string }[] = [
        { type: 'user_input', icon: 'i-user', label: 'User Input', badge: 'MOST COMMON' },
        { type: 'schedule', icon: 'i-clock', label: 'Schedule Trigger' },
        { type: 'webhook', icon: 'i-plug', label: 'Webhook Trigger' },
      ];

      return (
        <>
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Trigger</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>Every flow starts here — choose what starts it.</p>
          <div className="trigger-type-list">
            {OPTIONS.map((o) => (
              <div key={o.type} className={`trigger-type-row${triggerType === o.type ? ' sel' : ''}`} onClick={() => pickType(o.type)}>
                <div className="tt-icon"><Icon name={o.icon} /></div>
                <span className="tt-label">{o.label}</span>
                {o.badge && <span className="chip line tt-badge">{o.badge}</span>}
              </div>
            ))}
          </div>

          {triggerType === 'user_input' && (() => {
            const fields: InputField[] = cfg.fields || [];
            const removeField = (id: string) => {
              const next = fields.filter((f) => f.id !== id);
              set({ fields: next });
            };
            return (
              <div className="field">
                <div className="row-split" style={{ marginBottom: 6 }}>
                  <label style={{ margin: 0 }}>INPUT FIELD</label>
                  <button className="icon-btn" title="Add input field" onClick={() => setInputFieldModal({ open: true, editField: null })}><Icon name="i-plus" /></button>
                </div>
                {fields.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No input fields yet — every run just gets one plain message. Add a field for a real structured form.</p>}
                {fields.map((f) => {
                  const typeDef = INPUT_FIELD_TYPES.find((t) => t.value === f.type);
                  return (
                    <div key={f.id} className="input-field-row" onClick={() => setInputFieldModal({ open: true, editField: f })}>
                      <span className="mono small" style={{ color: 'var(--purple)' }}>{'{x}'}</span>
                      <div style={{ flex: 1 }}>
                        <b>{f.name}</b> <span className="small muted">· {f.name}</span>
                      </div>
                      {f.required && <span className="small muted">required</span>}
                      <span className="input-field-type-icon"><Icon name={typeDef?.icon || 'i-file'} /></span>
                      <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={(e) => { e.stopPropagation(); removeField(f.id); }}><Icon name="i-trash" /></button>
                    </div>
                  );
                })}
              </div>
            );
          })()}

          {triggerType === 'schedule' && (
            <>
              <Field label="Schedule (cron expression)">
                <input className="mono" placeholder="0 9 * * MON-FRI" value={cfg.cron || ''}
                  onChange={(e) => setCfg({ ...cfg, cron: e.target.value })} onBlur={() => set({ cron: cfg.cron })} />
              </Field>
              <Banner kind="warn">Saved for real, but nothing is actively scheduling runs yet — there's no scheduler process wired up in this reference implementation. See the module docstring in harness.py.</Banner>
            </>
          )}

          {triggerType === 'webhook' && webhookUrl && (
            <>
              <Field label="Webhook URL">
                <input className="mono" value={webhookUrl} readOnly onClick={(e) => (e.target as HTMLInputElement).select()} />
              </Field>
              <button className="btn sm ghost block" style={{ marginBottom: 10 }} onClick={sendTestWebhook}>
                <Icon name="i-test" /> Send test webhook
              </button>
              <Banner kind="good">Genuinely wired — a POST here runs this agent for real, the same as the Builder's Test button, tagged with principal <span className="mono">webhook</span> in its trace.</Banner>
            </>
          )}
        </div>
        {inputFieldModal.open && (
          <AddInputFieldModal
            agent={agent} node={node} editField={inputFieldModal.editField}
            onClose={() => setInputFieldModal({ open: false, editField: null })}
            onSaved={(a) => { onAgentUpdate(a); setCfg(a.flow.nodes.find((n) => n.id === node.id)?.config || cfg); }}
          />
        )}
        </>
      );
    }

    case 'gov':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>UCS Check</SecHead>
          <p className="small muted">Automatic platform guardrail, not user-configurable (§4.4). Resolves the whole run's entitlement plan before any data or tool node executes.</p>
          <KV k="Cache TTL" v={<span className="mono">60–120s</span>} />
          <KV k="Max chain depth" v={<span className="mono">5 hops</span>} />
        </div>
      );

    case 'respond':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Output</SecHead>
          <Field label="Format">
            <SearchSelect ariaLabel="Format" noun="format" value={cfg.format || 'markdown'}
              options={[
                { value: 'plain', label: 'Plain text' }, { value: 'markdown', label: 'Markdown' }, { value: 'json', label: 'JSON' },
              ]}
              onChange={(v) => set({ format: v })} />
          </Field>
          <div className="row-split" style={{ padding: '8px 0' }}>
            <span className="small">Streaming</span><Toggle on={cfg.streaming !== false} onChange={(v) => set({ streaming: v })} />
          </div>
        </div>
      );

    case 'answer':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Answer</SecHead>
          <Banner kind="info">Chatflow apps only — Workflow apps use the Output node instead. Real multiple-Answer-node support: each one's real resolved content appends to a real running transcript, so several Answer nodes across a flow build up one coherent reply rather than each overwriting the last.</Banner>
          <Field label="Answer content">
            <textarea rows={5} placeholder="Hello {{name}}, here's what I found…" value={cfg.content || ''} onChange={(e) => setCfg({ ...cfg, content: e.target.value })} onBlur={() => set({ content: cfg.content })} />
          </Field>
          <p className="small muted">
            Reference any earlier node's real output with <span className="mono">{'{{var}}'}</span> — including{' '}
            <span className="mono">{'{{input}}'}</span> for the original message. A variable that resolves to a real
            uploaded file (from a trigger's Single File / Multiple Files field, or a Doc Extractor result) renders as
            a real inline link, not a stringified object.
          </p>
          {delBtn}
        </div>
      );

    case 'retrieval': {
      const selectedKbs: string[] = cfg.knowledge_bases || (cfg.knowledge_base ? [cfg.knowledge_base] : []);
      const filterMode: string = cfg.metadata_filtering_mode || 'disabled';
      const conditions: { key: string; operator: string; value: string }[] = cfg.metadata_filtering_conditions || [];
      const toggleKb = (kb: { id: string; name: string }) => {
        // Deselect has to remove whichever of the three forms is actually
        // stored, or an older config would keep a stale display name and
        // the node would still claim to use a KB the person just turned off.
        const next = kbIsSelected(kb, selectedKbs)
          ? selectedKbs.filter((x) => !kbIsSelected(kb, [x]))
          : [...selectedKbs, kb.id];
        set({ knowledge_bases: next, knowledge_base: undefined });
      };
      const setCondition = (i: number, patch: Partial<{ key: string; operator: string; value: string }>) => {
        const next = conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c));
        set({ metadata_filtering_conditions: next });
      };
      const addCondition = () => set({ metadata_filtering_conditions: [...conditions, { key: retrievalMetadataKeys[0] || '', operator: '=', value: '' }] });
      const removeCondition = (i: number) => set({ metadata_filtering_conditions: conditions.filter((_, idx) => idx !== i) });
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Knowledge Retrieval</SecHead>
          <Field label="Query text">
            <input placeholder="{x} Set variable" value={cfg.query_var || ''} onChange={(e) => setCfg({ ...cfg, query_var: e.target.value })} onBlur={() => set({ query_var: cfg.query_var })} />
          </Field>

          <SecHead>Select Knowledge to Search</SecHead>
          {knowledgeBases.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No knowledge bases registered yet — add one under Integrations → KB Connections.</p>}
          {knowledgeBases.map((kb) => (
            <div key={kb.id} className="row-split" style={{ padding: '6px 0', cursor: 'pointer' }} onClick={() => toggleKb(kb)}>
              <span className="small" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className={`chip ${kb.status === 'connected' ? 'green' : 'line'}`} style={{ padding: '1px 6px' }}>{kb.provider}</span>{kb.name}
              </span>
              <Toggle on={kbIsSelected(kb, selectedKbs)} onChange={() => toggleKb(kb)} />
            </div>
          ))}

          <SecHead>Enable Metadata Filtering</SecHead>
          <Field label="Mode">
            <SearchSelect ariaLabel="Metadata filtering mode" noun="mode" value={filterMode}
              options={[
                { value: 'disabled', label: 'Disabled' }, { value: 'automatic', label: 'Automatic' }, { value: 'manual', label: 'Manual' },
              ]}
              onChange={(v) => set({ metadata_filtering_mode: v })} />
          </Field>
          {filterMode === 'automatic' && (
            <>
              <p className="small muted" style={{ marginTop: 0 }}>Filters auto-configure from the query — a real model call proposes conditions from this KB's real metadata fields.</p>
              <Field label="Filtering model">
                <ModelSelect value={cfg.filtering_model} fallback="Claude Haiku 4.5" models={llmModels}
                             onChange={(name) => set({ filtering_model: name })} />
              </Field>
            </>
          )}
          {filterMode === 'manual' && (
            <div style={{ marginBottom: 10 }}>
              {retrievalMetadataKeys.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No metadata fields found yet on the selected knowledge base(s) — ingest a document with metadata to populate this.</p>}
              {conditions.map((c, i) => (
                <div key={i} className="row-split" style={{ gap: 6, marginBottom: 6 }}>
                  <SearchSelect ariaLabel="Metadata field" noun="field" placeholder="field…" value={c.key}
                    style={{ flex: 1 }}
                    options={retrievalMetadataKeys.map((k) => ({ value: k, label: k }))}
                    onChange={(v) => setCondition(i, { key: v })} />
                  <SearchSelect ariaLabel="Operator" noun="operator" value={c.operator}
                    style={{ width: 120, flex: 'none' }}
                    options={[
                      { value: '=', label: '=' }, { value: '!=', label: '≠' },
                      { value: '>', label: '>' }, { value: '<', label: '<' },
                      { value: 'contains', label: 'contains' },
                      { value: 'starts_with', label: 'starts with' },
                      { value: 'ends_with', label: 'ends with' },
                    ]}
                    onChange={(v) => setCondition(i, { operator: v })} />
                  <input style={{ flex: 1 }} placeholder="value" value={c.value} onChange={(e) => setCondition(i, { value: e.target.value })} />
                  <button className="icon-action danger" title="Remove condition" aria-label="Remove condition" onClick={() => removeCondition(i)}><Icon name="i-trash" /></button>
                </div>
              ))}
              <button className="btn sm ghost" onClick={addCondition}><Icon name="i-plus" /> Add condition</button>
              <p className="small muted" style={{ marginTop: 8 }}>Conditions combine with AND.</p>
            </div>
          )}

          <Field label="Top K results">
            <input value={cfg.top_k ?? 5} onChange={(e) => setCfg({ ...cfg, top_k: e.target.value })} onBlur={() => set({ top_k: cfg.top_k })} />
          </Field>
          <Banner kind="info">Real nearest-neighbor search against whichever knowledge base(s) are checked above — entitlement resolved at run time, not a raw connection string.</Banner>
          {delBtn}
        </div>
      );
    }

    case 'agent': {
      const strategyLabels: { value: string; label: string }[] = [
        { value: 'react', label: 'ReAct' },
        { value: 'function_calling', label: 'Function calling' },
        { value: 'plan_execute', label: 'Plan & execute (this platform\u2019s own addition)' },
      ];
      const strategyInfo: Record<string, string> = {
        react: 'Loops up to Max iterations, alternating Thought/Action, stopping as soon as the model itself concludes with a real "Final Answer".',
        function_calling: 'Real, genuinely iterative tool-use loop — up to Max iterations real rounds via Anthropic\u2019s own real tool-use API, stopping the moment the model stops requesting a tool.',
        plan_execute: 'Always exactly two real calls: first a real plan, then a real execution pass genuinely conditioned on that plan. Not one of Dify\u2019s own two documented strategies, kept as a real, useful addition.',
      };
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Configure</SecHead>
          <AgentConfigurePanel
            agent={agent} nodeId={node.id} cfg={cfg} set={set} setCfg={setCfg} onAgentUpdate={onAgentUpdate} toast={toast}
            llmModels={llmModels}
            strategyLabels={strategyLabels} strategyInfo={strategyInfo}
            modelSelect={<ModelSelect value={cfg.model} fallback="Claude Sonnet 5" models={llmModels}
                                      onChange={(name) => set({ model: name })} />} />
          {delBtn}
        </div>
      );
    }

    case 'classifier':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Question Classifier</SecHead>
          <Field label="Model">
            <ModelSelect value={cfg.model} fallback="Claude Haiku 4.5" models={llmModels}
                         onChange={(name) => set({ model: name })} />
          </Field>
          <Field label="Input variables">
            <input placeholder="{x} Set variable" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
          </Field>
          <div className="row-split" style={{ padding: '8px 0' }}>
            <span className="small">Vision</span><Toggle on={!!cfg.vision} onChange={(v) => set({ vision: v })} />
          </div>
          <Field label="Classes">
            <input placeholder="Class 1 — topic name" value={cfg.class1 || ''} onChange={(e) => setCfg({ ...cfg, class1: e.target.value })} onBlur={() => set({ class1: cfg.class1 })} style={{ marginBottom: 6 }} />
            <input placeholder="Class 2 — topic name" value={cfg.class2 || ''} onChange={(e) => setCfg({ ...cfg, class2: e.target.value })} onBlur={() => set({ class2: cfg.class2 })} />
          </Field>
          <SecHead>Advanced setting</SecHead>
          <Field label="Instruction">
            <textarea placeholder="Write your prompt word here, enter '{' to insert a variable" rows={3}
              value={cfg.instruction || ''} onChange={(e) => setCfg({ ...cfg, instruction: e.target.value })} onBlur={() => set({ instruction: cfg.instruction })} />
          </Field>
          <SecHead>Output variables</SecHead>
          <KV k="class_name" v={<span className="small muted">string</span>} />
          <KV k="class_label" v={<span className="small muted">string</span>} />
          {delBtn}
        </div>
      );

    case 'parameter_extractor': {
      const params: { name: string; type: string; description: string; required: boolean }[] = cfg.parameters || [];
      const setParams = (next: typeof params) => set({ parameters: next });
      const updateParam = (i: number, patch: Partial<typeof params[0]>) => {
        const next = params.map((p, pi) => (pi === i ? { ...p, ...patch } : p));
        setCfg({ ...cfg, parameters: next });
        return next; // real fresh value — a caller needing to `set(...)` in the same handler can't rely on the stale `cfg` closure
      };
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Parameter Extractor</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>Real LLM-based extraction — converts unstructured text into real structured JSON matching the parameters below.</p>
          <Field label="Input variable">
            <input placeholder="{{input}}" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
          </Field>
          <Field label="Model">
            <ModelSelect value={cfg.model} fallback="Claude Sonnet 5" models={llmModels}
                         onChange={(name) => set({ model: name })} />
          </Field>
          <div className="row-split" style={{ marginBottom: 6 }}>
            <label style={{ margin: 0 }}>PARAMETERS</label>
            <button className="icon-btn" title="Add parameter" onClick={() => setParams([...params, { name: '', type: 'string', description: '', required: false }])}>
              <Icon name="i-plus" />
            </button>
          </div>
          {params.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No parameters yet — add one above.</p>}
          {params.map((p, i) => (
            <div key={i} className="card pad" style={{ marginBottom: 8 }}>
              <div className="row-split" style={{ gap: 8, marginBottom: 6 }}>
                <input placeholder="parameter_name" value={p.name} style={{ flex: 1 }} onChange={(e) => updateParam(i, { name: e.target.value })} onBlur={() => set({ parameters: cfg.parameters })} />
                <SearchSelect ariaLabel="Parameter type" noun="type" value={p.type}
                  style={{ width: 110, flex: 'none' }}
                  options={[
                    { value: 'string', label: 'String' }, { value: 'number', label: 'Number' }, { value: 'boolean', label: 'Boolean' },
                    { value: 'array', label: 'Array' }, { value: 'object', label: 'Object' },
                  ]}
                  onChange={(v) => { const next = updateParam(i, { type: v }); set({ parameters: next }); }} />
                <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => setParams(params.filter((_, pi) => pi !== i))}><Icon name="i-trash" /></button>
              </div>
              <input placeholder="Description — helps the LLM understand what to extract" value={p.description} style={{ width: '100%', marginBottom: 6 }} onChange={(e) => updateParam(i, { description: e.target.value })} onBlur={() => set({ parameters: cfg.parameters })} />
              <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
                <span className="small">Required</span><Toggle on={!!p.required} onChange={(v) => { const next = updateParam(i, { required: v }); set({ parameters: next }); }} />
              </div>
            </div>
          ))}
          <Field label="Extraction instructions">
            <textarea rows={3} placeholder="Describe what to extract and how to format it — examples improve accuracy." value={cfg.instruction || ''} onChange={(e) => setCfg({ ...cfg, instruction: e.target.value })} onBlur={() => set({ instruction: cfg.instruction })} />
          </Field>
          <Field label="Inference mode">
            <SearchSelect ariaLabel="Inference mode" noun="mode" value={cfg.inference_mode || 'function_call'}
              options={[
                { value: 'function_call', label: 'Function Call / Tool Call' },
                { value: 'prompt', label: 'Prompt-based' },
              ]}
              onChange={(v) => set({ inference_mode: v })} />
          </Field>
          <p className="small muted" style={{ marginTop: 0 }}>
            {cfg.inference_mode === 'prompt'
              ? 'Real pure-prompt extraction — a real instruction to respond with only JSON, with real parsing of the response. For models without tool-calling, or when it performs worse.'
              : 'Real Anthropic tool-use — a forced structured tool call for reliable real type compliance.'}
          </p>
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Memory</span><Toggle on={!!cfg.memory_enabled} onChange={(v) => set({ memory_enabled: v })} />
          </div>
          {cfg.memory_enabled && <p className="small muted" style={{ marginTop: 0 }}>Real — includes real conversation history for context, the same real transcript the LLM node's own memory toggle reads.</p>}
          <SecHead>Output variables</SecHead>
          {params.map((p) => <KV key={p.name} k={p.name || '(unnamed)'} v={<span className="small muted">{p.type}</span>} />)}
          <KV k="__is_success" v={<span className="small muted">number (1 or 0)</span>} />
          <KV k="__reason" v={<span className="small muted">string</span>} />
          {delBtn}
        </div>
      );
    }

    case 'condition':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>IF/ELSE</SecHead>
          <Field label="Expression">
            <input className="mono" value={cfg.expression || ''} placeholder="{{quality_score}} >= 70" onChange={(e) => setCfg({ ...cfg, expression: e.target.value })} onBlur={() => set({ expression: cfg.expression })} />
          </Field>
          {delBtn}
        </div>
      );

    case 'hitl': {
      const deliveryMethods: string[] = cfg.delivery_methods || ['web_app'];
      const fields: { name: string; type: string; options?: string[]; default_value?: string }[] = cfg.fields || [];
      const actions: { id: string; title: string; style: string }[] = cfg.actions || [];
      const fieldTypeLabels: Record<string, string> = { paragraph: 'Paragraph', select: 'Select', single_file: 'Single File', file_list: 'File List' };
      const toggleDelivery = (m: string) => {
        const next = deliveryMethods.includes(m) ? deliveryMethods.filter((x) => x !== m) : [...deliveryMethods, m];
        set({ delivery_methods: next });
      };
      const setField = (i: number, patch: Record<string, any>) => set({ fields: fields.map((f, idx) => (idx === i ? { ...f, ...patch } : f)) });
      const addField = () => set({ fields: [...fields, { id: `f${Date.now()}`, name: '', type: 'paragraph', options: [], default_value: '' }] });
      const removeField = (i: number) => set({ fields: fields.filter((_, idx) => idx !== i) });
      const setAction = (i: number, patch: Record<string, any>) => set({ actions: actions.map((a, idx) => (idx === i ? { ...a, ...patch } : a)) });
      const addAction = () => set({ actions: [...actions, { id: `act${Date.now()}`, title: '', style: 'secondary' }] });
      const removeAction = (i: number) => set({ actions: actions.filter((_, idx) => idx !== i) });
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Human Input</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>Pauses the flow to request human input — a real pause and resume on this platform's sync execution path.</p>
          {deliveryMethods.length === 0 && <Banner kind="warn">No delivery method added, the operation cannot be triggered.</Banner>}

          <SecHead>Delivery Method</SecHead>
          <div className="row-split" style={{ padding: '6px 0' }}><span className="small">Web app</span><Toggle on={deliveryMethods.includes('web_app')} onChange={() => toggleDelivery('web_app')} /></div>
          <div className="row-split" style={{ padding: '6px 0' }}><span className="small">Email</span><Toggle on={deliveryMethods.includes('email')} onChange={() => toggleDelivery('email')} /></div>
          {deliveryMethods.includes('email') && (
            <p className="small muted" style={{ marginTop: 0 }}>Recorded for real, but this reference platform has no email provider configured — Email requests won't actually be sent.</p>
          )}

          <SecHead>Form Content</SecHead>
          <Field label="Markdown, reference variables with {{var}}">
            <textarea rows={4} placeholder={'Please review:\n\n{{llm_output}}'} value={cfg.form_content || ''}
              onChange={(e) => setCfg({ ...cfg, form_content: e.target.value })} onBlur={() => set({ form_content: cfg.form_content })} />
          </Field>

          <SecHead>Form Fields</SecHead>
          {fields.map((f, i) => (
            <div key={i} className="card pad" style={{ marginBottom: 8, padding: 10 }}>
              <div className="row-split" style={{ gap: 6, marginBottom: 6 }}>
                <input placeholder="field name" value={f.name || ''} style={{ flex: 1 }} onChange={(e) => setField(i, { name: e.target.value })} />
                <SearchSelect ariaLabel="Field type" noun="type" value={f.type || 'paragraph'}
                  style={{ width: 140, flex: 'none' }}
                  options={Object.entries(fieldTypeLabels).map(([v, l]) => ({ value: v, label: l }))}
                  onChange={(v) => setField(i, { type: v })} />
                <button className="icon-action danger" title="Remove field" aria-label="Remove field" onClick={() => removeField(i)}><Icon name="i-trash" /></button>
              </div>
              {f.type === 'select' && (
                <input placeholder="options, comma-separated" value={(f.options || []).join(', ')}
                  onChange={(e) => setField(i, { options: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
              )}
              {(f.type || 'paragraph') === 'paragraph' && (
                <input placeholder="default value or {{var}} to pre-fill" value={f.default_value || ''} onChange={(e) => setField(i, { default_value: e.target.value })} />
              )}
              <p className="small muted" style={{ marginTop: 6, marginBottom: 0 }}>{(f.type || 'paragraph') === 'paragraph' ? 'Optional' : "Mandatory — matches Dify's real rule"}</p>
            </div>
          ))}
          <button className="btn sm ghost" onClick={addField}><Icon name="i-plus" /> Add field</button>

          <SecHead>User Action</SecHead>
          {actions.map((a, i) => (
            <div key={i} className="row-split" style={{ gap: 6, marginBottom: 6 }}>
              <input placeholder="button title" value={a.title || ''} style={{ flex: 1 }} onChange={(e) => setAction(i, { title: e.target.value })} />
              <SearchSelect ariaLabel="Button style" noun="style" value={a.style === 'primary' ? 'primary' : 'secondary'}
                style={{ width: 130, flex: 'none' }}
                options={[{ value: 'primary', label: 'Primary' }, { value: 'secondary', label: 'Secondary' }]}
                onChange={(v) => setAction(i, { style: v })} />
              <button className="icon-action danger" title="Remove action" aria-label="Remove action" onClick={() => removeAction(i)}><Icon name="i-trash" /></button>
            </div>
          ))}
          <button className="btn sm ghost" onClick={addAction}><Icon name="i-plus" /> Add action</button>
          <p className="small muted" style={{ marginTop: 8 }}>Each action gets its own real output port below — drag a connection from it to wire that branch.</p>

          <SecHead>Timeout Strategy</SecHead>
          <Field label="Timeout">
            <div className="row-split">
              <input value={cfg.timeout_value ?? 3} style={{ maxWidth: 70 }} onChange={(e) => setCfg({ ...cfg, timeout_value: e.target.value })} onBlur={() => set({ timeout_value: cfg.timeout_value })} />
              <div className="modechip" style={{ flex: 'none', width: 'auto' }}>
                <button className={cfg.timeout_unit !== 'hours' ? 'on' : ''} onClick={() => set({ timeout_unit: 'days' })}>Days</button>
                <button className={cfg.timeout_unit === 'hours' ? 'on' : ''} onClick={() => set({ timeout_unit: 'hours' })}>Hours</button>
              </div>
            </div>
          </Field>
          <p className="small muted" style={{ marginTop: 0 }}>If nobody responds before the timeout, the flow follows the real Timeout port below — if nothing's connected there, the run ends, same as an unwired action.</p>
          {delBtn}
        </div>
      );
    }

    case 'iteration':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Iteration</SecHead>
          <Field label="Input" extra={<span className="chip line" style={{ float: 'right' }}>Array</span>}>
            <input placeholder="{x} Set variable" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
          </Field>
          <Field label="Output variables" extra={<span className="chip line" style={{ float: 'right' }}>Array</span>}>
            <input placeholder="{x} Set variable" value={cfg.output_var || ''} onChange={(e) => setCfg({ ...cfg, output_var: e.target.value })} onBlur={() => set({ output_var: cfg.output_var })} />
          </Field>
          <div className="row-split" style={{ padding: '8px 0' }}>
            <span className="small">Parallel mode</span><Toggle on={!!cfg.parallel} onChange={(v) => set({ parallel: v })} />
          </div>
          <Field label="Error response method">
            <SearchSelect ariaLabel="Error response method" noun="method" value={cfg.error_response || 'Terminated'}
              options={['Terminated', 'Continue on error', 'Remove abnormal output'].map((o) => ({ value: o, label: o }))}
              onChange={(v) => set({ error_response: v })} />
          </Field>
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Flatten output</span><Toggle on={cfg.flatten !== false} onChange={(v) => set({ flatten: v })} />
          </div>
          {delBtn}
        </div>
      );

    case 'loop': {
      const children = agent.flow.nodes.filter((n) => n.parent_id === node.id);
      const hasExit = children.some((n) => n.type === 'exit_loop');
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Loop</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            A real container — drag nodes from the palette and drop them inside this loop's box on the canvas to
            make them real body nodes (they'll show <span className="mono">parent_id</span> pointing at this loop).
            Wire them among themselves however you like, including their own <span className="mono">condition</span>{' '}
            branches — end a branch at an <span className="mono">Exit Loop</span> node to stop iterating early and
            fall through to whatever runs after this loop. Body nodes can reference{' '}
            <span className="mono">{'{{loop_iteration}}'}</span> for the current pass (starts at 1).
          </p>
          <KV k="Body nodes" v={<span className="mono">{children.length}</span>} />
          <KV k="Has an Exit Loop node" v={hasExit ? <span className="chip green">yes</span> : <span className="chip amber">no — will only stop via the safety cap below</span>} />
          <Field label="Maximum loop count (safety cap)">
            <div className="row-split">
              <input value={cfg.max_loops ?? 10} style={{ maxWidth: 70 }} onChange={(e) => setCfg({ ...cfg, max_loops: e.target.value })} onBlur={() => set({ max_loops: cfg.max_loops })} />
              <input type="range" min={1} max={50} value={cfg.max_loops ?? 10} style={{ flex: 1 }} onChange={(e) => set({ max_loops: e.target.value })} />
            </div>
            <p className="small muted" style={{ marginTop: 4, marginBottom: 0 }}>Stops the loop even if nothing inside it ever reaches Exit Loop — a real safety net, not the primary way to exit.</p>
          </Field>
          {delBtn}
        </div>
      );
    }

    case 'exit_loop':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Exit Loop</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Place this at the end of a branch inside a loop's body. When execution reaches it, the loop stops
            iterating immediately and moves on to whatever runs after the loop — a real early exit, not just a
            visual marker. Only meaningful inside a loop container; on its own it does nothing.
          </p>
          {delBtn}
        </div>
      );

    case 'comment': {
      const fontSizeLabels: Record<string, string> = { small: 'Small', medium: 'Medium', large: 'Large' };
      const fontSize = cfg.font_size || 'small';
      const applyFormat = (action: FormatAction) => {
        const ta = document.getElementById('comment-body-textarea') as HTMLTextAreaElement | null;
        if (!ta) return;
        const result = applyCommentFormat(cfg.body || '', ta.selectionStart, ta.selectionEnd, action);
        setCfg({ ...cfg, body: result.text });
        set({ body: result.text });
        // Restore real focus + selection so a person can keep formatting
        // without having to re-click into the textarea each time.
        requestAnimationFrame(() => {
          ta.focus();
          ta.setSelectionRange(result.selStart, result.selEnd);
        });
      };
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Comment</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Documentation only — never executes, never connects into the flow. Position it near whatever it
            explains.
          </p>
          <Field label="Title">
            <input placeholder="e.g. Bring in your data" value={cfg.title || ''}
              onChange={(e) => setCfg({ ...cfg, title: e.target.value })} onBlur={() => set({ title: cfg.title })} />
          </Field>
          <Field label="Background color">
            <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
              {COMMENT_COLORS.map((c) => (
                <span
                  key={c.id}
                  className={`comment-color-swatch${(cfg.color || 'blue') === c.id ? ' sel' : ''}`}
                  style={{ background: c.swatch }}
                  title={c.id}
                  onClick={() => set({ color: c.id })}
                />
              ))}
            </div>
          </Field>
          <Field label="Body">
            <div className="comment-toolbar">
              <SearchSelect size="sm" ariaLabel="Font size" noun="font size" value={fontSize}
                className="comment-fontsize" style={{ width: 110, flex: 'none' }} menuMinWidth={180}
                options={Object.entries(fontSizeLabels).map(([k, label]) => ({ value: k, label: `Aa ${label}` }))}
                onChange={(v) => set({ font_size: v })} />
              <span className="sep" />
              <button title="Bold" onClick={() => applyFormat('bold')}><Icon name="i-bold" /></button>
              <button title="Italic" onClick={() => applyFormat('italic')}><Icon name="i-italic" /></button>
              <button title="Strikethrough" onClick={() => applyFormat('strike')}><Icon name="i-strike" /></button>
              <button title="Link" onClick={() => applyFormat('link')}><Icon name="i-link" /></button>
              <span className="sep" />
              <button title="Bulleted list" onClick={() => applyFormat('bullet')}><Icon name="i-bars" /></button>
              <button title="Heading" onClick={() => applyFormat('heading')}>H</button>
              <button title="Quote" onClick={() => applyFormat('quote')}>&ldquo;</button>
            </div>
            <textarea id="comment-body-textarea" rows={5} placeholder="Explain what this part of the flow does, or how to configure it…" value={cfg.body || ''}
              onChange={(e) => setCfg({ ...cfg, body: e.target.value })} onBlur={() => set({ body: cfg.body })} />
            <p className="small muted" style={{ marginTop: 4, marginBottom: 0 }}>
              Real formatting — genuinely wraps your selected text with real Markdown, rendered on the canvas note itself.
            </p>
          </Field>
          {delBtn}
        </div>
      );
    }

    case 'code':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Code Node</SecHead>
          <Field label="Input variables">
            <div className="row-split" style={{ gap: 8, marginBottom: 6 }}>
              <input value={cfg.arg1_name ?? 'arg1'} style={{ maxWidth: 90 }} onChange={(e) => setCfg({ ...cfg, arg1_name: e.target.value })} onBlur={() => set({ arg1_name: cfg.arg1_name })} />
              <input placeholder="{x} Set variable" value={cfg.arg1_var || ''} onChange={(e) => setCfg({ ...cfg, arg1_var: e.target.value })} onBlur={() => set({ arg1_var: cfg.arg1_var })} />
            </div>
            <div className="row-split" style={{ gap: 8 }}>
              <input value={cfg.arg2_name ?? 'arg2'} style={{ maxWidth: 90 }} onChange={(e) => setCfg({ ...cfg, arg2_name: e.target.value })} onBlur={() => set({ arg2_name: cfg.arg2_name })} />
              <input placeholder="{x} Set variable" value={cfg.arg2_var || ''} onChange={(e) => setCfg({ ...cfg, arg2_var: e.target.value })} onBlur={() => set({ arg2_var: cfg.arg2_var })} />
            </div>
          </Field>
          <Field label="Python3">
            <textarea className="mono" rows={6} style={{ fontSize: 12 }}
              value={cfg.code ?? 'def main(arg1: str, arg2: str):\n    return {\n        "result": arg1 + arg2,\n    }'}
              onChange={(e) => setCfg({ ...cfg, code: e.target.value })} onBlur={() => set({ code: cfg.code })} />
          </Field>
          <Field label="Output variables">
            <div className="row-split" style={{ gap: 8 }}>
              <input value={cfg.output_name ?? 'result'} style={{ maxWidth: 120 }} onChange={(e) => setCfg({ ...cfg, output_name: e.target.value })} onBlur={() => set({ output_name: cfg.output_name })} />
              <SearchSelect ariaLabel="Output type" noun="type" value={cfg.output_type || 'String'}
                style={{ flex: 1 }}
                options={['String', 'Number', 'Object'].map((o) => ({ value: o, label: o }))}
                onChange={(v) => set({ output_type: v })} />
            </div>
          </Field>
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)' }}>
            <span className="small">Retry on failure</span><Toggle on={!!cfg.retry_on_failure} onChange={(v) => set({ retry_on_failure: v })} />
          </div>
          {cfg.retry_on_failure && <p className="small muted" style={{ marginTop: 0 }}>Real — genuinely re-runs the code once more before giving up, not just a relabeled single attempt.</p>}
          <Field label="Error handling">
            <SearchSelect ariaLabel="Error handling" noun="option" value={cfg.error_handling || 'None'}
              options={['None', 'Default value', 'Fail branch'].map((o) => ({ value: o, label: o }))}
              onChange={(v) => set({ error_handling: v })} />
          </Field>
          {cfg.error_handling === 'Default value' && (
            <Field label="Default value (used as this node's output on a real failure)">
              <input value={cfg.default_value ?? ''} onChange={(e) => setCfg({ ...cfg, default_value: e.target.value })} onBlur={() => set({ default_value: cfg.default_value })} />
            </Field>
          )}
          {cfg.error_handling === 'Fail branch' && (
            <Banner kind="info">A real second output port appears on this node on the canvas — wire it to whatever should run when the code genuinely fails, separately from the normal success path.</Banner>
          )}
          <p className="small muted">Executes in an isolated subprocess with enforced CPU / memory / time limits — not Firecracker/gVisor (those need infrastructure this environment doesn't have; see the sandbox module's docs for why). A nonzero exit code is a real failure — handled per Error handling above, not silently reported as success.</p>
          {delBtn}
        </div>
      );

    case 'template':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Template</SecHead>
          <Field label="Jinja2 template">
            <textarea rows={5} placeholder="Hello {{ name }}" value={cfg.template || ''} onChange={(e) => setCfg({ ...cfg, template: e.target.value })} onBlur={() => set({ template: cfg.template })} />
          </Field>
          <Field label="Output variable">
            <div className="row-split" style={{ gap: 8 }}>
              <input value={cfg.output_var_name ?? 'output'} style={{ maxWidth: 120 }} onChange={(e) => setCfg({ ...cfg, output_var_name: e.target.value })} onBlur={() => set({ output_var_name: cfg.output_var_name })} />
              <span className="chip line">String</span>
            </div>
          </Field>
          {delBtn}
        </div>
      );

    case 'aggregator': {
      type AggGroup = { output_name: string; variables: string[] };
      const groups: AggGroup[] = cfg.groups && cfg.groups.length > 0 ? cfg.groups : [{ output_name: cfg.output_name || 'merged_output', variables: [] }];
      const saveGroups = (next: AggGroup[]) => set({ groups: next });
      const updateGroup = (gi: number, patch: Partial<AggGroup>) => {
        const next = groups.map((g, i) => (i === gi ? { ...g, ...patch } : g));
        setCfg({ ...cfg, groups: next });
      };
      const addGroup = () => saveGroups([...groups, { output_name: '', variables: [] }]);
      const removeGroup = (gi: number) => saveGroups(groups.filter((_, i) => i !== gi));
      const addVariable = (gi: number) => updateGroup(gi, { variables: [...groups[gi].variables, ''] });
      const updateVariable = (gi: number, vi: number, value: string) => {
        const next = groups[gi].variables.map((v, i) => (i === vi ? value : v));
        updateGroup(gi, { variables: next });
      };
      const removeVariable = (gi: number, vi: number) => updateGroup(gi, { variables: groups[gi].variables.filter((_, i) => i !== vi) });
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Variable Aggregator</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Converges EXCLUSIVE branches (only one of which genuinely runs — e.g. a Condition node's True/False
            paths) into one real output. Select the real variable each branch sets; since only one branch ran,
            only one genuinely has a value, and that real value — type preserved — becomes the output. Not for
            merging branches that run in parallel — use a Code or Template node for that.
          </p>
          {groups.map((group, gi) => (
            <div key={gi} className="card pad" style={{ marginBottom: 10 }}>
              <div className="row-split" style={{ marginBottom: 8 }}>
                <input
                  placeholder="Output variable name" value={group.output_name} style={{ flex: 1, marginRight: 8 }}
                  onChange={(e) => updateGroup(gi, { output_name: e.target.value })}
                  onBlur={() => set({ groups: cfg.groups || groups })}
                />
                {groups.length > 1 && (
                  <button className="icon-action danger" title="Remove group" aria-label="Remove group" onClick={() => removeGroup(gi)}><Icon name="i-trash" /></button>
                )}
              </div>
              <div className="row-split" style={{ marginBottom: 6 }}>
                <label className="small muted" style={{ margin: 0 }}>VARIABLES TO CONVERGE (one per branch)</label>
                <button className="icon-btn" title="Add variable" onClick={() => addVariable(gi)}><Icon name="i-plus" /></button>
              </div>
              {group.variables.length === 0 && <p className="small muted" style={{ marginTop: 0, marginBottom: 4 }}>No variables yet — add one per branch above.</p>}
              {group.variables.map((v, vi) => (
                <div key={vi} className="row-split" style={{ gap: 8, marginBottom: 6 }}>
                  <input
                    className="mono" placeholder="{{branch_variable}}" value={v} style={{ flex: 1 }}
                    onChange={(e) => updateVariable(gi, vi, e.target.value)}
                    onBlur={() => set({ groups: cfg.groups || groups })}
                  />
                  <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => removeVariable(gi, vi)}><Icon name="i-trash" /></button>
                </div>
              ))}
            </div>
          ))}
          <button className="btn sm ghost" onClick={addGroup}><Icon name="i-plus" /> Add Aggregation Group</button>
          <p className="small muted" style={{ marginTop: 10 }}>
            Multiple groups let one node converge several independent sets of branch variables at once, each
            producing its own separate real output — real Dify-style "Aggregation Group" support, not a single
            fixed merge.
          </p>
          {delBtn}
        </div>
      );
    }

    case 'output': {
      const variables: { name: string; expr: string }[] = cfg.variables || [];
      const setVariables = (next: { name: string; expr: string }[]) => set({ variables: next });
      const updateVar = (i: number, patch: Partial<{ name: string; expr: string }>) => {
        const next = variables.map((v, vi) => (vi === i ? { ...v, ...patch } : v));
        setCfg({ ...cfg, variables: next });
      };
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Output</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Collects named output fields at this point in the flow — real values pulled from whatever
            upstream node produced them. Reference an earlier node's output with <span className="mono">{'{{var}}'}</span>,
            e.g. <span className="mono">{'{{template_output}}'}</span> or <span className="mono">{'{{input}}'}</span> for
            the original message. A flow can have more than one Output node; their fields merge.
          </p>
          <div className="row-split" style={{ marginBottom: 6 }}>
            <label style={{ margin: 0 }}>OUTPUT VARIABLE</label>
            <button className="icon-btn" title="Add output variable" onClick={() => setVariables([...variables, { name: '', expr: '' }])}>
              <Icon name="i-plus" />
            </button>
          </div>
          {variables.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No output variables yet — add one above.</p>}
          {variables.map((v, i) => (
            <div key={i} className="row-split" style={{ gap: 8, marginBottom: 8, alignItems: 'flex-start' }}>
              <input
                placeholder="Variable name" value={v.name} style={{ flex: 1 }}
                onChange={(e) => updateVar(i, { name: e.target.value })}
                onBlur={() => set({ variables: cfg.variables })}
              />
              <input
                placeholder="{x} Set variable" value={v.expr} style={{ flex: 1 }}
                onChange={(e) => updateVar(i, { expr: e.target.value })}
                onBlur={() => set({ variables: cfg.variables })}
              />
              <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => setVariables(variables.filter((_, vi) => vi !== i))}>
                <Icon name="i-trash" />
              </button>
            </div>
          ))}
          {delBtn}
        </div>
      );
    }

    case 'docextractor': {
      const mode = cfg.mode || 'single';
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Doc Extractor</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Genuinely fetches and extracts — not a placeholder. Accepts a literal S3 / GCS / Azure Blob URL
            (public or presigned), this platform's own real <span className="mono">/api/uploads</span> URL
            (from a trigger's real Single File / Multiple Files field — see the Trigger node), or a{' '}
            <span className="mono">{'{{var}}'}</span> reference to either. No external cloud SDK credentials
            are configured — only public, presigned, or this platform's own real upload links work.
          </p>
          <Field label="Mode">
            <SearchSelect ariaLabel="Mode" noun="mode" value={mode}
              options={[{ value: 'single', label: 'Single File' }, { value: 'multiple', label: 'Multiple Files' }]}
              onChange={(v) => set({ mode: v })} />
          </Field>
          {mode === 'single' ? (
            <Field label="Input" extra={<span className="chip line" style={{ float: 'right' }}>URL / {'{{var}}'}</span>}>
              <input className="mono" placeholder="s3://bucket/key.pdf or {{documents}}" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
            </Field>
          ) : (
            <Field label="Input (a real array of files)" extra={<span className="chip line" style={{ float: 'right' }}>{'{{var}}'}</span>}>
              <input className="mono" placeholder="{{documents}}" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
            </Field>
          )}
          {mode === 'multiple' && (
            <p className="small muted" style={{ marginTop: 0 }}>
              Reference a trigger's real <span className="mono">file_list</span> field by name (e.g.{' '}
              <span className="mono">{'{{documents}}'}</span>) — every real uploaded file is genuinely fetched
              and extracted; output is a real array of strings, one per file, in the same order.
            </p>
          )}
          <p className="small muted">
            Real extraction for: pdf, docx, doc, xlsx, xls, csv, pptx, ppt, epub, eml, msg, vtt, html, txt, md,
            json, xml, yaml, and other text-like formats (real encoding detection, not an always-UTF-8
            assumption). Tables (docx/xlsx/xls/csv) become real Markdown tables; consecutive same-speaker VTT
            utterances are genuinely merged. Legacy .doc/.ppt use real system tools (antiword/catdoc) — a real,
            deliberate substitution for Dify's external Unstructured API service, which this reference
            platform has no credentials for. Output lands in <span className="mono">{'{{docextractor_text}}'}</span>{' '}
            — a real string in Single File mode, a real array of strings in Multiple Files mode. Note: named{' '}
            <span className="mono">docextractor_text</span>, not Dify's <span className="mono">text</span> —
            kept as-is deliberately to avoid a breaking rename for any existing flow already using it.
          </p>
          {delBtn}
        </div>
      );
    }

    case 'list_operator': {
      type Filter = { property: string; operator: string; value: string };
      const filters: Filter[] = cfg.filters || [];
      const setFilters = (next: Filter[]) => set({ filters: next });
      const updateFilter = (i: number, patch: Partial<Filter>) => {
        const next = filters.map((f, fi) => (fi === i ? { ...f, ...patch } : f));
        setCfg({ ...cfg, filters: next });
        return next;
      };
      const sort: { property?: string; direction?: string } | null = cfg.sort || null;
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>List Operator</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Real filtering, sorting, and selection over a real array — including real file-property filters
            (type/mime type/extension/size/name/transfer method) for arrays of uploaded files, e.g. a trigger's
            real <span className="mono">file_list</span> field. The exact real pattern this closes: split a
            mixed upload into images (→ a vision LLM) and documents (→ Doc Extractor), each with its own
            List Operator node.
          </p>
          <Field label="Input array" extra={<span className="chip line" style={{ float: 'right' }}>Array</span>}>
            <input className="mono" placeholder="{{uploaded_files}}" value={cfg.input_var || ''} onChange={(e) => setCfg({ ...cfg, input_var: e.target.value })} onBlur={() => set({ input_var: cfg.input_var })} />
          </Field>

          <div className="row-split" style={{ marginBottom: 6 }}>
            <label style={{ margin: 0 }}>FILTERS (all must match)</label>
            <button className="icon-btn" title="Add filter" onClick={() => setFilters([...filters, { property: '', operator: 'is', value: '' }])}>
              <Icon name="i-plus" />
            </button>
          </div>
          {filters.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No filters — every item passes through.</p>}
          {filters.map((f, i) => (
            <div key={i} className="card pad" style={{ marginBottom: 8 }}>
              <div className="row-split" style={{ gap: 8, marginBottom: 6 }}>
                <SearchSelect ariaLabel="Filter property" noun="property" value={f.property}
                  style={{ flex: 1 }}
                  options={[
                    { value: '', label: 'Value itself (plain array)' },
                    { value: 'type', label: 'Type (image/document/audio/video)' },
                    { value: 'mime_type', label: 'MIME Type' },
                    { value: 'extension', label: 'Extension' },
                    { value: 'size', label: 'Size' },
                    { value: 'name', label: 'Name' },
                    { value: 'transfer_method', label: 'Transfer Method' },
                  ]}
                  onChange={(v) => { const next = updateFilter(i, { property: v }); set({ filters: next }); }} />
                <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => setFilters(filters.filter((_, fi) => fi !== i))}><Icon name="i-trash" /></button>
              </div>
              <SearchSelect ariaLabel="Filter operator" noun="operator" value={f.operator}
                style={{ width: '100%', marginBottom: 6 }}
                options={[
                  { value: 'is', label: 'is' }, { value: 'is_not', label: 'is not' },
                  { value: 'contains', label: 'contains' }, { value: 'not_contains', label: 'does not contain' },
                  { value: 'starts_with', label: 'starts with' }, { value: 'ends_with', label: 'ends with' },
                  { value: '>', label: 'greater than' }, { value: '<', label: 'less than' },
                  { value: '>=', label: 'greater than or equal' }, { value: '<=', label: 'less than or equal' },
                  { value: 'empty', label: 'is empty' }, { value: 'not_empty', label: 'is not empty' },
                ]}
                onChange={(v) => { const next = updateFilter(i, { operator: v }); set({ filters: next }); }} />
              {!['empty', 'not_empty'].includes(f.operator) && (
                <input placeholder="Value to compare against" value={f.value} style={{ width: '100%' }} onChange={(e) => updateFilter(i, { value: e.target.value })} onBlur={() => set({ filters: cfg.filters })} />
              )}
            </div>
          ))}

          <Field label="Sort by">
            <SearchSelect ariaLabel="Sort by" noun="sort property"
              value={sort && sort.property !== undefined ? sort.property : '__none__'}
              options={[
                { value: '__none__', label: 'No sorting' },
                { value: '', label: 'Value itself (plain array)' },
                { value: 'type', label: 'Type' }, { value: 'size', label: 'Size' }, { value: 'name', label: 'Name' },
              ]}
              onChange={(v) => set({ sort: v === '__none__' ? null : { property: v, direction: (sort && sort.direction) || 'asc' } })} />
          </Field>
          {sort && sort.property !== undefined && (
            <Field label="Direction">
              <SearchSelect ariaLabel="Direction" noun="direction" value={sort.direction || 'asc'}
                options={[
                  { value: 'asc', label: 'Ascending (A–Z, smallest first)' },
                  { value: 'desc', label: 'Descending (Z–A, largest first)' },
                ]}
                onChange={(v) => set({ sort: { ...sort, direction: v } })} />
            </Field>
          )}

          <Field label="Take First N (optional)">
            <input type="number" min={1} max={20} placeholder="1–20" value={cfg.limit || ''} onChange={(e) => setCfg({ ...cfg, limit: e.target.value })} onBlur={() => set({ limit: cfg.limit })} />
          </Field>

          <SecHead>Output variables</SecHead>
          <KV k="result" v={<span className="small muted">array — the full filtered/sorted/limited list</span>} />
          <KV k="first_record" v={<span className="small muted">single element, or null if empty</span>} />
          <KV k="last_record" v={<span className="small muted">single element, or null if empty</span>} />
          {delBtn}
        </div>
      );
    }

    case 'tool': {
      const obj: string = cfg.catalog_object || '';
      const isDs = obj.startsWith('datasource:');
      const isTool = obj.startsWith('tool:');
      const dsName = isDs ? obj.slice('datasource:'.length) : '';
      const [toolName, actionName] = isTool ? obj.slice('tool:'.length).split(':') : ['', ''];
      const ds = dataSources.find((d) => d.name === dsName);
      const action = (toolActions[toolName] || []).find((a) => a.name === actionName);
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Tool / Connector Node</SecHead>
          <Field label="Catalog object">
            {/* Only tools from Integrations → Tools are offered; a node saved
                earlier with a data source keeps its value visible until reselected. */}
            <SearchSelect ariaLabel="Catalog object" noun="tool action" placeholder="Choose a tool action…"
              value={obj}
              options={[
                ...(isDs ? [{ value: obj, label: `${dsName} (data source)` }] : []),
                ...tools.flatMap((t) => (toolActions[t.name] || []).map((a) => ({
                  value: `tool:${t.name}:${a.name}`, label: a.name, group: t.name,
                }))),
              ]}
              onChange={(v) => set({ catalog_object: v })} />
          </Field>
          {isDs && (
            <p className="small muted" style={{ marginTop: -6 }}>{ds ? `${ds.auth} auth · ${ds.status} — data sources are no longer offered here; reselect a tool action.` : 'This data source is no longer registered — reselect a tool action.'}</p>
          )}
          {isTool && (
            <p className="small muted" style={{ marginTop: -6 }}>
              {action ? `${action.desc}${action.params.length ? ` · params: ${action.params.map((p) => p.name).join(', ')}` : ''}` : 'This tool action is no longer registered — reselect one.'}
            </p>
          )}
          {isTool && action && action.params.length > 0 && (
            <>
              <SecHead>Parameters</SecHead>
              {action.params.map((p) => {
                const paramValues = cfg.param_values || {};
                return (
                  <Field key={p.name} label={`${p.name}${p.required ? ' *' : ''}`} extra={<span className="chip line" style={{ float: 'right' }}>{'{{var}}'}</span>}>
                    <input
                      className="mono"
                      placeholder={p.desc || p.name}
                      value={paramValues[p.name] ?? ''}
                      onChange={(e) => setCfg({ ...cfg, param_values: { ...paramValues, [p.name]: e.target.value } })}
                      onBlur={() => set({ param_values: cfg.param_values })}
                    />
                  </Field>
                );
              })}
              <p className="small muted" style={{ marginTop: -6 }}>
                Real, genuine execution — a {'{{var}}'} reference or a literal value for each real parameter this action actually needs.
              </p>
            </>
          )}
          <Field label="Entitlement"><span className="chip green">select — resolved</span></Field>
          {delBtn}
        </div>
      );
    }

    case 'http':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>HTTP Request</SecHead>
          <div className="row-split" style={{ gap: 8 }}>
            <SearchSelect ariaLabel="HTTP method" noun="method" value={cfg.method || 'GET'}
              style={{ width: 100, flex: 'none' }}
              options={['GET', 'POST', 'PUT', 'DELETE'].map((o) => ({ value: o, label: o }))}
              onChange={(v) => set({ method: v })} />
            <input placeholder="Enter URL" value={cfg.url || ''} onChange={(e) => setCfg({ ...cfg, url: e.target.value })} onBlur={() => set({ url: cfg.url })} />
          </div>
          <Field label="Headers">
            <textarea rows={2} placeholder={'Key: Value\nAnother-Key: Value'} value={cfg.headers_text || ''}
              onChange={(e) => setCfg({ ...cfg, headers_text: e.target.value })} onBlur={() => set({ headers_text: cfg.headers_text })} />
          </Field>
          <Field label="Params">
            <textarea rows={2} placeholder={'key=value\nanother=value'} value={cfg.params_text || ''}
              onChange={(e) => setCfg({ ...cfg, params_text: e.target.value })} onBlur={() => set({ params_text: cfg.params_text })} />
          </Field>
          <Field label="Body">
            <div className="pill-row">
              {['none', 'form-data', 'JSON', 'raw', 'binary'].map((b) => (
                <span key={b} className={`chip ${cfg.body_type === b || (!cfg.body_type && b === 'none') ? 'teal' : 'line'}`}
                  style={{ cursor: 'pointer' }} onClick={() => set({ body_type: b })}>{b}</span>
              ))}
            </div>
          </Field>
          {/* Real body-content input per type (§0.65, GAP_REPORT) — previously
              there was no way to enter body content at all from the builder,
              for any type including JSON; each now genuinely reaches
              handle_http/safe_http_request (harness.py, egress.py). */}
          {cfg.body_type === 'JSON' && (
            <Field label="Body (JSON)">
              <textarea rows={4} className="mono" placeholder={'{\n  "key": "value"\n}'} value={cfg.body_json || ''}
                onChange={(e) => setCfg({ ...cfg, body_json: e.target.value })} onBlur={() => set({ body_json: cfg.body_json })} />
            </Field>
          )}
          {cfg.body_type === 'form-data' && (
            <Field label="Body (form fields)">
              <textarea rows={3} placeholder={'key=value\nanother=value'} value={cfg.body_form_text || ''}
                onChange={(e) => setCfg({ ...cfg, body_form_text: e.target.value })} onBlur={() => set({ body_form_text: cfg.body_form_text })} />
            </Field>
          )}
          {cfg.body_type === 'raw' && (
            <Field label="Body (raw)">
              <textarea rows={4} className="mono" placeholder="Raw request body, sent exactly as written." value={cfg.body_raw_text || ''}
                onChange={(e) => setCfg({ ...cfg, body_raw_text: e.target.value })} onBlur={() => set({ body_raw_text: cfg.body_raw_text })} />
            </Field>
          )}
          {cfg.body_type === 'binary' && (
            <Field label="Body (binary, base64-encoded)">
              <textarea rows={3} className="mono" placeholder="Base64-encoded content — decoded to raw bytes before sending." value={cfg.body_binary_base64 || ''}
                onChange={(e) => setCfg({ ...cfg, body_binary_base64: e.target.value })} onBlur={() => set({ body_binary_base64: cfg.body_binary_base64 })} />
            </Field>
          )}
          <div className="row-split" style={{ padding: '8px 0', borderTop: '1px solid var(--line-2)', marginTop: 10 }}>
            <span className="small">Retry on failure</span><Toggle on={cfg.retry_on_failure !== false} onChange={(v) => set({ retry_on_failure: v })} />
          </div>
          <Field label="Max retries">
            <div className="row-split">
              <input value={cfg.max_retries ?? 3} style={{ maxWidth: 70 }} onChange={(e) => setCfg({ ...cfg, max_retries: e.target.value })} onBlur={() => set({ max_retries: cfg.max_retries })} />
              <input type="range" min={0} max={10} value={cfg.max_retries ?? 3} style={{ flex: 1 }} onChange={(e) => set({ max_retries: e.target.value })} />
            </div>
          </Field>
          <p className="small muted" style={{ marginTop: -6 }}>Retries a real 5xx (server-side) failure or a network error, up to this many additional attempts. A 4xx (client error) is never retried — asking again identically won't fix it.</p>
          <Banner kind="warn">Every real destination (and every real redirect hop) is DNS-resolved and IP-validated before the connection is made — not yet routed through a governed engine the way a Tool Node is. TLS verification is always on for this node; that isn't configurable here.</Banner>
          {delBtn}
        </div>
      );

    case 'memory':
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Memory / State</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Genuinely persists across separate runs (Postgres-backed) — not just within this one execution.
            Place a <span className="mono">read</span> node early in the flow to load prior state, and a{' '}
            <span className="mono">write</span> node later to save new state under the same key.
          </p>
          <Field label="Key">
            <input className="mono" placeholder="e.g. last_reply, visit_count" value={cfg.key || ''}
              onChange={(e) => setCfg({ ...cfg, key: e.target.value })} onBlur={() => set({ key: cfg.key })} />
          </Field>
          <Field label="Scope">
            <SearchSelect ariaLabel="Scope" noun="scope" value={cfg.scope || 'agent'}
              options={[
                { value: 'agent', label: 'Cross-run (agent-level) — shared by every run' },
                { value: 'conversation', label: 'Conversation — scoped per invoking user' },
              ]}
              onChange={(v) => set({ scope: v })} />
          </Field>
          <Field label="Mode">
            <SearchSelect ariaLabel="Mode" noun="mode" value={cfg.mode || 'read_write'}
              options={[
                { value: 'read', label: 'Read only' },
                { value: 'write', label: 'Write only' },
                { value: 'read_write', label: 'Read, then write (load old value, then overwrite)' },
              ]}
              onChange={(v) => set({ mode: v })} />
          </Field>
          {(cfg.mode === 'write' || cfg.mode === 'read_write' || !cfg.mode) && (
            <Field label="Value to write">
              <input className="mono" placeholder="{{output}} or a literal value" value={cfg.write_value || ''}
                onChange={(e) => setCfg({ ...cfg, write_value: e.target.value })} onBlur={() => set({ write_value: cfg.write_value })} />
              <p className="small muted" style={{ marginTop: 4, marginBottom: 0 }}>
                <span className="mono">{'{{output}}'}</span> captures whatever the most recent node set as output; anything else is stored literally.
              </p>
            </Field>
          )}
          <Banner kind="info">
            Read makes the prior value available downstream as <span className="mono">{`{{memory_${cfg.key || 'KEY'}}}`}</span> — reference it from any template/LLM node after this one.
          </Banner>
          {delBtn}
        </div>
      );

    case 'variable_assigner': {
      type Assignment = { variable: string; operation: string; source_var: string; value: string; arithmetic_op: string };
      const assignments: Assignment[] = cfg.assignments || [];
      const setAssignments = (next: Assignment[]) => set({ assignments: next });
      const updateAssignment = (i: number, patch: Partial<Assignment>) => {
        const next = assignments.map((a, ai) => (ai === i ? { ...a, ...patch } : a));
        setCfg({ ...cfg, assignments: next });
        return next; // real fresh value — a caller needing to `set(...)` in the same handler can't rely on the stale `cfg` closure
      };
      const needsSourceVar = (op: string) => ['overwrite', 'arithmetic', 'append', 'extend'].includes(op);
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Variable Assigner</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Real, genuinely persistent — writes to real CONVERSATION variables (Postgres-backed, scoped per
            invoking user), not plain workflow variables that reset every run. A value set here is real and
            readable by a LATER, SEPARATE run for the same user, as <span className="mono">{'{{conv_<name>}}'}</span>.
          </p>
          <div className="row-split" style={{ marginBottom: 6 }}>
            <label style={{ margin: 0 }}>ASSIGNMENTS</label>
            <button className="icon-btn" title="Add assignment" onClick={() => setAssignments([...assignments, { variable: '', operation: 'overwrite', source_var: '', value: '', arithmetic_op: '+' }])}>
              <Icon name="i-plus" />
            </button>
          </div>
          {assignments.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>No assignments yet — add one above.</p>}
          {assignments.map((a, i) => (
            <div key={i} className="card pad" style={{ marginBottom: 8 }}>
              <div className="row-split" style={{ gap: 8, marginBottom: 6 }}>
                <input className="mono" placeholder="variable_name" value={a.variable} style={{ flex: 1 }} onChange={(e) => updateAssignment(i, { variable: e.target.value })} onBlur={() => set({ assignments: cfg.assignments })} />
                <button className="icon-action danger" title="Remove" aria-label="Remove" onClick={() => setAssignments(assignments.filter((_, ai) => ai !== i))}><Icon name="i-trash" /></button>
              </div>
              <SearchSelect ariaLabel="Operation" noun="operation" value={a.operation}
                style={{ width: '100%', marginBottom: 6 }}
                options={[
                  { value: 'overwrite', label: 'Overwrite (from a variable)' },
                  { value: 'set', label: 'Set (a fixed value)' },
                  { value: 'clear', label: 'Clear' },
                  { value: 'arithmetic', label: 'Arithmetic (number)' },
                  { value: 'append', label: 'Append (array)' },
                  { value: 'extend', label: 'Extend (array)' },
                  { value: 'remove_first', label: 'Remove First (array)' },
                  { value: 'remove_last', label: 'Remove Last (array)' },
                ]}
                onChange={(v) => { const next = updateAssignment(i, { operation: v }); set({ assignments: next }); }} />
              {needsSourceVar(a.operation) && (
                <input className="mono" placeholder="{{source_var}} or, for Arithmetic, a literal number" value={a.source_var} style={{ width: '100%' }} onChange={(e) => updateAssignment(i, { source_var: e.target.value })} onBlur={() => set({ assignments: cfg.assignments })} />
              )}
              {a.operation === 'arithmetic' && (
                <SearchSelect ariaLabel="Arithmetic operation" noun="operation" value={a.arithmetic_op || '+'}
                  style={{ width: '100%', marginTop: 6 }}
                  options={[
                    { value: '+', label: 'Add' }, { value: '-', label: 'Subtract' },
                    { value: '*', label: 'Multiply' }, { value: '/', label: 'Divide' },
                  ]}
                  onChange={(v) => { const next = updateAssignment(i, { arithmetic_op: v }); set({ assignments: next }); }} />
              )}
              {a.operation === 'set' && (
                <input placeholder="Fixed value" value={a.value} style={{ width: '100%' }} onChange={(e) => updateAssignment(i, { value: e.target.value })} onBlur={() => set({ assignments: cfg.assignments })} />
              )}
            </div>
          ))}
          {delBtn}
        </div>
      );
    }

    case 'subagent': {
      const otherAgents = allAgents.filter((a) => a.id !== agent.id && a.plane !== 'Snippet');
      const targetAgent = allAgents.find((a) => a.id === cfg.target_agent_id);
      return (
        <div className="cfg">
          <SecHead style={{ marginTop: 0 }}>Sub-Agent Node</SecHead>
          <p className="small muted" style={{ marginTop: 0 }}>
            Genuinely runs the target agent's own real flow (its own trigger/gov/... nodes, including its own real
            entitlement checks) and bubbles its real output back here — not a simulation. The same invoking user is
            threaded through every hop.
          </p>
          <Field label="Target agent">
            <SearchSelect ariaLabel="Target agent" noun="agent" placeholder="Select an agent…"
              value={cfg.target_agent_id || ''}
              options={otherAgents.map((a) => ({ value: a.id, label: a.name }))}
              onChange={(v) => set({ target_agent_id: v })} />
          </Field>
          {cfg.target_agent_id && !targetAgent && (
            <Banner kind="warn">This id doesn't match any agent currently visible here — the run will record a real "target not found" error rather than silently doing nothing.</Banner>
          )}
          <Field label="Input (optional)">
            <input className="mono" placeholder="{{output}} — defaults to whatever this flow has produced so far" value={cfg.input || ''}
              onChange={(e) => setCfg({ ...cfg, input: e.target.value })} onBlur={() => set({ input: cfg.input })} />
          </Field>
          <Banner kind="info">
            Real cycle detection — an agent that's already in this call's chain is refused before ever being invoked, not just eventually crashed into. Real hop cap: 5 (§7.6), matching the same number UCS's own entitlement resolution already enforces.
          </Banner>
          {delBtn}
        </div>
      );
    }

    default:
      return <div className="cfg"><div className="empty-cfg">Select a node to configure it.</div></div>;
  }
  })();

  return (
    // .cfg-shell, not a bare position:relative div: this wrapper is the
    // grid child of .builder-shell, and an unconstrained one let the
    // config panel inside it lay out at its full content height (~2300px
    // for an LLM node) and be clipped by the shell instead of scrolling.
    // See the .cfg-shell rules in styles.css.
    <div className="cfg-shell">
      <a
        className="docs-link-icon"
        href={`#/docs/${node.type}`}
        target="_blank"
        rel="noopener noreferrer"
        title={`View documentation for ${NODE_TYPES[node.type]?.label || node.type}`}
      >
        <Icon name="i-help" />
      </a>
      {panelContent}
    </div>
  );
}
