import { useEffect, useRef, useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import SearchSelect from './SearchSelect';
import { Banner, SecHead, KV } from './ui';
import { api } from '../api';
import type { Agent, AgentFileRef, AgentSkillRef, AgentToolEntry, FileRequirement, LlmModelOption, TemplatePolicy } from '../types';
import './agent-configure.css';

const MB = 1024 * 1024;
export const FILE_LIMITS_MB = { document: 15, image: 10, audio: 50, video: 100 } as const;
// Documents the agent can actually read: text-like files, PDF and DOCX.
const DOC_EXT = ['pdf', 'docx', 'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'xml', 'html', 'htm', 'yaml', 'yml', 'log'];
const UNREADABLE_DOC_EXT = ['doc', 'xls', 'xlsx', 'ppt', 'pptx', 'epub', 'rtf'];
const IMG_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
const AUD_EXT = ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'flac', 'amr'];
const VID_EXT = ['mp4', 'mov', 'webm', 'mpeg', 'mpg', 'avi', 'mkv'];
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

export type FileKind = 'document' | 'image' | 'audio' | 'video';

export function classifyFile(name: string): FileKind | null {
  const ext = name.toLowerCase().split('.').pop() || '';
  if (DOC_EXT.includes(ext)) return 'document';
  if (IMG_EXT.includes(ext)) return 'image';
  if (AUD_EXT.includes(ext)) return 'audio';
  if (VID_EXT.includes(ext)) return 'video';
  return null;
}

/** Why a file cannot be uploaded, or null when it can. Checked before sending
 *  so a 100 MB video is not uploaded only to be refused. */
export function fileProblem(file: { name: string; size: number }): string | null {
  const ext = file.name.toLowerCase().split('.').pop() || '';
  if (UNREADABLE_DOC_EXT.includes(ext)) return `"${file.name}": .${ext} files can't be read by the agent yet. Save it as PDF, DOCX, TXT, CSV or Markdown.`;
  const kind = classifyFile(file.name);
  if (!kind) return `"${file.name}" is not a supported file type.`;
  const max = FILE_LIMITS_MB[kind];
  if (file.size > max * MB) return `"${file.name}" is over the ${max} MB limit for ${kind === 'audio' ? 'audio' : kind + 's'}.`;
  if (file.size === 0) return `"${file.name}" is empty.`;
  return null;
}

export function skillProblem(file: { name: string; size: number }): string | null {
  if (!/\.(zip|skill)$/i.test(file.name)) return 'A skill package must be a .zip or .skill file.';
  if (file.size > 50 * MB) return 'A skill package must be 50 MB or smaller.';
  return null;
}

export function fmtSize(n: number): string {
  if (n >= MB) return `${(n / MB).toFixed(n >= 10 * MB ? 0 : 1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** Parse the text of a .env file into KEY/VALUE rows. */
export function parseDotenv(text: string): { key: string; value: string }[] {
  const rows: { key: string; value: string }[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.replace(/^export\s+/, '').match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"') && v.length >= 2) || (v.startsWith("'") && v.endsWith("'") && v.length >= 2)) {
      v = v.slice(1, -1);
    } else {
      v = v.replace(/\s+#.*$/, '');
    }
    rows.push({ key: m[1], value: v });
  }
  return rows;
}

/** Why the selected model can't run this node as configured, or null when it can.
 *  Judged on what the node needs: chat, plus tool calling when it will use tools. */
export function modelProblem(name: string | undefined, models: LlmModelOption[], needs: { toolCalling: boolean }): string | null {
  if (!name || models.length === 0) return null;
  const m = models.find((x) => x.name === name);
  if (!m) return "This model isn't in the model catalog.";
  if (!m.available) return m.note || 'This model is unavailable.';
  if (m.supports_chat === false || (m.types && m.types.length > 0 && !m.types.some((t) => ['llm', 'chat'].includes(String(t).toLowerCase())))) {
    return "This isn't a chat model.";
  }
  if (needs.toolCalling && m.supports_tool_calling === false) return "This model doesn't support tool calling, which this node's tools need.";
  return null;
}

type ToolTab = 'all' | 'tool' | 'mcp' | 'workflow';

function HelpTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="ac-help">
      <button type="button" className="ac-help-btn" aria-label="What is this?" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="i-help" />
      </button>
      {open && <span className="ac-help-pop" role="note">{text}</span>}
    </span>
  );
}

function SectionHead({ title, help, action }: { title: string; help: string; action?: React.ReactNode }) {
  return (
    <div className="ac-head">
      <span className="ac-title">{title}<HelpTip text={help} /></span>
      {action}
    </div>
  );
}

interface EnvRow { key: string; draft: string; saved: boolean }

export const POLICY_LABELS: Record<TemplatePolicy, string> = {
  require: 'Require upload on install',
  embed: 'Embed content',
  exclude: 'Exclude',
};

export default function AgentConfigurePanel({
  agent, nodeId, cfg, set, setCfg, onAgentUpdate, modelSelect, llmModels, toast, strategyLabels, strategyInfo,
}: {
  agent: Agent;
  nodeId: string;
  onAgentUpdate: (a: Agent) => void;
  cfg: Record<string, any>;
  set: (patch: Record<string, any>) => void;
  setCfg: (c: Record<string, any>) => void;
  modelSelect: React.ReactNode;
  llmModels: LlmModelOption[];
  toast: (m: string) => void;
  strategyLabels: { value: string; label: string }[];
  strategyInfo: Record<string, string>;
}) {
  const [skills, setSkills] = useState<AgentSkillRef[]>([]);
  const [files, setFiles] = useState<AgentFileRef[]>([]);
  const [catalog, setCatalog] = useState<AgentToolEntry[]>([]);
  const [configured, setConfigured] = useState<Set<string>>(new Set());
  const [addMenu, setAddMenu] = useState(false);
  const [uploadingSkill, setUploadingSkill] = useState(false);
  const [fileModal, setFileModal] = useState(false);
  const [toolPop, setToolPop] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const skillInput = useRef<HTMLInputElement>(null);

  const selectedSkills: string[] = Array.isArray(cfg.skills) ? cfg.skills : [];
  const selectedFiles: string[] = Array.isArray(cfg.files) ? cfg.files : [];
  const selectedTools: string[] = Array.isArray(cfg.tools) ? cfg.tools : [];
  const envKeys: string[] = (Array.isArray(cfg.env) ? cfg.env : []).map((r: any) => String(r?.key ?? ''));
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);

  useEffect(() => {
    let live = true;
    const guard = <T,>(p: Promise<T> | undefined, fn: (v: T) => void) => { if (p && typeof p.then === 'function') p.then((v) => live && fn(v)).catch(() => {}); };
    try { guard(api.listAgentSkills?.(agent.id), setSkills); } catch { /* panel still renders */ }
    try { guard(api.listAgentFiles?.(agent.id), setFiles); } catch { /* panel still renders */ }
    try { guard(api.getAgentToolCatalog?.(), (r) => setCatalog(r.tools || [])); } catch { /* panel still renders */ }
    try {
      guard(api.listNodeSecrets?.(agent.id, nodeId), (r) => {
        const have = new Set(r.filter((x) => x.configured).map((x) => x.key));
        setConfigured(have);
        setEnvRows((prev) => prev.map((row) => ({ ...row, saved: have.has(row.key) })));
      });
    } catch { /* panel still renders */ }
    return () => { live = false; };
  }, [agent.id, nodeId]);

  // Env rows are local UI state seeded from the node's KEY names. Values are never in `cfg`.
  useEffect(() => {
    // Keep what the person is typing: drafts for keys that still exist, and rows that have no key yet.
    setEnvRows((prev) => [
      ...envKeys.map((k) => ({ key: k, draft: prev.find((p) => p.key === k)?.draft ?? '', saved: configured.has(k) })),
      ...prev.filter((p) => !p.key),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, cfg.env?.length, configured]);

  const prompt: string = cfg.prompt ?? cfg.instructions ?? '';
  const strategy: string = cfg.strategy || 'react';
  const problem = modelProblem(cfg.model || 'Claude Sonnet 5', llmModels, { toolCalling: selectedTools.length > 0 && strategy === 'function_calling' });

  // ── skills ──────────────────────────────────────────────────────────────
  const attachedSkills = selectedSkills.map((id) => skills.find((s) => s.id === id)).filter(Boolean) as AgentSkillRef[];
  const librarySkills = skills.filter((s) => !selectedSkills.includes(s.id));
  const pickSkill = async (file: File | undefined) => {
    setAddMenu(false);
    if (!file) return;
    const p = skillProblem(file);
    if (p) { toast(p); return; }
    setUploadingSkill(true);
    try {
      const s = await api.uploadAgentSkill(agent.id, file);
      setSkills((prev) => [...prev, s]);
      set({ skills: [...selectedSkills, s.id] });
    } catch (e: any) { toast(e.message || 'Skill upload failed'); }
    finally { setUploadingSkill(false); if (skillInput.current) skillInput.current.value = ''; }
  };
  /** Remove = detach from this node only. The skill stays in the agent's library. */
  const detachSkill = (id: string) => set({ skills: selectedSkills.filter((x) => x !== id) });
  const attachSkill = (id: string) => { setAddMenu(false); set({ skills: [...selectedSkills, id] }); };
  const deleteSkill = async (id: string) => {
    try { await api.deleteAgentSkill(agent.id, id); } catch (e: any) { toast(e.message || 'Could not delete the skill'); return; }
    setSkills((prev) => prev.filter((s) => s.id !== id));
  };

  // ── files ───────────────────────────────────────────────────────────────
  const attachedFiles = selectedFiles.map((id) => files.find((f) => f.id === id)).filter(Boolean) as AgentFileRef[];
  const libraryFiles = files.filter((f) => !selectedFiles.includes(f.id));
  const detachFile = (id: string) => set({ files: selectedFiles.filter((x) => x !== id) });
  const attachFile = (id: string) => set({ files: [...selectedFiles, id] });
  const deleteFile = async (id: string) => {
    try { await api.deleteAgentFile(agent.id, id); } catch (e: any) { toast(e.message || 'Could not delete the file'); return; }
    setFiles((prev) => prev.filter((f) => f.id !== id));
  };
  const onFilesUploaded = (added: AgentFileRef[]) => {
    if (!added.length) return;
    setFiles((prev) => [...prev, ...added]);
    set({ files: [...selectedFiles, ...added.map((f) => f.id)] });
  };
  const required: FileRequirement[] = Array.isArray(cfg.files_required) ? cfg.files_required : [];
  const [binding, setBinding] = useState<string | null>(null);
  /** Supply a file a template asked for: upload it, then bind it to the requirement in one draft update. */
  const fulfil = async (req: FileRequirement, picked: File | undefined) => {
    if (!picked || !req.requirement_id) return;
    const ext = picked.name.toLowerCase().split('.').pop() || '';
    const p = fileProblem(picked) || (req.accepted_types?.length && !req.accepted_types.includes(ext)
      ? `"${picked.name}" isn't an accepted type for ${req.name || req.filename} (${req.accepted_types.slice(0, 6).join(', ')}).` : null);
    if (p) { toast(p); return; }
    setBinding(req.requirement_id);
    try {
      const f = await api.uploadAgentFile(agent.id, picked);
      setFiles((prev) => [...prev, f]);
      try {
        const res = await api.bindFileRequirement(agent.id, nodeId, req.requirement_id, f.id, agent.draft_revision);
        adopt(res.agent);
      } catch (e: any) {
        toast(`${e.message} The file was uploaded to the library; attach it from there once the problem is fixed.`);
      }
    } catch (e: any) { toast(e.message || 'Upload failed'); }
    finally { setBinding(null); }
  };
  const changePolicy = async (f: AgentFileRef, policy: TemplatePolicy) => {
    try {
      const updated = await api.setFileTemplatePolicy(agent.id, f.id, policy);
      setFiles((prev) => prev.map((x) => (x.id === f.id ? { ...x, ...updated } : x)));
    } catch (e: any) { toast(e.message || 'Could not change the template setting'); }
  };

  // ── tools ───────────────────────────────────────────────────────────────
  const addTool = (obj: string) => {
    if (selectedTools.includes(obj)) return;
    // Tools are only used by the function-calling strategy.
    set({ tools: [...selectedTools, obj], ...(strategy !== 'function_calling' ? { strategy: 'function_calling' } : {}) });
  };
  const removeTool = (obj: string) => set({ tools: selectedTools.filter((x) => x !== obj) });
  const entryFor = (obj: string) => catalog.find((c) => c.value === obj);
  const toolsInactive = selectedTools.length > 0 && strategy !== 'function_calling';

  // ── env ─────────────────────────────────────────────────────────────────
  // The whole Env is saved in one call: the key list on the draft and the encrypted values
  // commit together, so the panel can never describe keys the server doesn't have (or the reverse).
  /** Take the saved agent from the server into the builder and this panel. */
  const adopt = (a: Agent) => {
    onAgentUpdate(a);
    const n = a.flow?.nodes?.find((x) => x.id === nodeId);
    if (n) setCfg(n.config || {});
  };
  const commitEnv = async (rows: EnvRow[]) => {
    const valid = rows.filter((r) => r.key && ENV_KEY_RE.test(r.key));
    if (new Set(valid.map((r) => r.key)).size !== valid.length) return; // duplicates are flagged inline
    try {
      const res = await api.putNodeEnv(agent.id, nodeId, valid.map((r) => (r.draft ? { key: r.key, value: r.draft } : { key: r.key })), agent.draft_revision);
      setConfigured(new Set(res.entries.filter((e) => e.configured).map((e) => e.key)));
      // Values that were just stored are cleared from the screen (a newer edit to the same box is kept).
      const sent = new Map(valid.filter((r) => r.draft).map((r) => [r.key, r.draft]));
      setEnvRows((prev) => prev.map((r) => (sent.get(r.key) === r.draft && r.draft ? { ...r, draft: '', saved: true } : r)));
      adopt(res.agent);
    } catch (e: any) { toast(e.message || 'Could not save Env'); }
  };
  const saveRow = (rows: EnvRow[]) => {
    // Only a row whose key is valid and which has something new (a value, or a key not yet declared) needs a save.
    const dirty = rows.some((r) => r.key && ENV_KEY_RE.test(r.key) && (r.draft || !envKeys.includes(r.key)));
    if (dirty) commitEnv(rows);
  };
  const deleteRow = (i: number) => {
    const next = envRows.filter((_, j) => j !== i);
    setEnvRows(next);
    if (envRows[i].key) commitEnv(next);
  };
  const importEnv = async (file: File | undefined) => {
    if (!file) return;
    const parsed = parseDotenv(await file.text());
    if (!parsed.length) { toast('No KEY=VALUE lines found in that file.'); return; }
    let rows = envRows.slice();
    for (const p of parsed) {
      const at = rows.findIndex((r) => r.key === p.key);
      rows = at >= 0 ? rows.map((r, j) => (j === at ? { ...r, draft: p.value } : r)) : [...rows, { key: p.key, draft: p.value, saved: false }];
    }
    setEnvRows(rows);
    await commitEnv(rows);
  };
  const dupKeys = new Set(envRows.map((r) => r.key).filter((k, i, a) => k && a.indexOf(k) !== i));

  return (
    <div className="ac">
      {/* Model */}
      <section className="ac-sec">
        <SectionHead title="Model" help="Pick the chat model the agent runs on. Favor recent models that are strong at reasoning and natively support tool calling. Older models often can't use the agent's tools reliably." />
        <div className="ac-model">
          <div className="ac-grow">{modelSelect}</div>
          {problem && <span className="ac-badge-incompat" title={problem}>Incompatible</span>}
        </div>
        {problem && <p className="small muted ac-note">{problem} Choose a different model.</p>}
      </section>

      {/* Prompt */}
      <section className="ac-sec">
        <SectionHead title="Prompt" help="Set the agent's role and goal, how it should approach the work, when to use its tools and files, and what the final output should look like. Reference variables with {{var}} and this node's Env values with {{env.KEY}}." />
        <textarea className="ac-prompt" rows={6} aria-label="Prompt"
          placeholder={'Role and goal: You are a support analyst who resolves refund questions.\nApproach: check policy files first, then use tools.\nOutput: a short answer with the policy cited.'}
          value={prompt}
          onChange={(e) => setCfg({ ...cfg, prompt: e.target.value })}
          onBlur={() => set({ prompt: cfg.prompt ?? prompt })} />
      </section>

      {/* Skills */}
      <section className="ac-sec">
        <SectionHead title="Skills" help="A skill is a procedure the agent follows for one specific task. Add a skill package: a .zip or .skill file with a SKILL.md inside, up to 50 MB. Every skill attached here is included in the agent's instructions on every run."
          action={(
            <span className="ac-menu-wrap">
              <button type="button" className="btn ghost sm" onClick={() => setAddMenu(!addMenu)} disabled={uploadingSkill} aria-haspopup="menu" aria-expanded={addMenu}>
                <Icon name="i-plus" /> {uploadingSkill ? 'Uploading…' : 'Add'}
              </button>
              {addMenu && (
                <div className="ac-menu" role="menu">
                  <button type="button" role="menuitem" className="ac-menu-item" onClick={() => skillInput.current?.click()}>
                    <Icon name="i-upload" /> Upload skill.zip <span className="chip line sm">EMBEDDED</span>
                  </button>
                  {librarySkills.length > 0 && <div className="ac-group">In this agent's library</div>}
                  {librarySkills.map((s) => (
                    <div key={s.id} className="ac-menu-row">
                      <button type="button" role="menuitem" className="ac-menu-item" onClick={() => attachSkill(s.id)} title={s.description}>
                        <Icon name="i-brain" /> {s.name}
                      </button>
                      <button type="button" className="icon-action danger" aria-label={`Delete skill ${s.name} permanently`} onClick={() => deleteSkill(s.id)}><Icon name="i-trash" /></button>
                    </div>
                  ))}
                </div>
              )}
              <input ref={skillInput} type="file" accept=".zip,.skill" hidden data-testid="skill-input" onChange={(e) => pickSkill(e.target.files?.[0])} />
            </span>
          )} />
        {attachedSkills.length === 0
          ? <p className="small muted ac-empty">No skills added.</p>
          : (
            <ul className="ac-list">
              {attachedSkills.map((s) => (
                <li key={s.id} className="ac-row">
                  <Icon name="i-brain" />
                  <div className="ac-row-main"><b>{s.name}</b><span className="small muted">{s.description}</span></div>
                  {s.retired && <span className="chip line sm" title="This skill was retired, but a version you restored uses it">Restored from version</span>}
                  <span className="small muted">{fmtSize(s.size_bytes)}</span>
                  <button type="button" className="icon-action" title="Remove from this node (stays in the library)" aria-label={`Remove skill ${s.name} from this node`} onClick={() => detachSkill(s.id)}><Icon name="i-x" /></button>
                </li>
              ))}
            </ul>
          )}
        {attachedSkills.length > 1 && <p className="small muted ac-note">All {attachedSkills.length} skills are included in every run, so keep unrelated ones off this node.</p>}
      </section>

      {/* Files */}
      <section className="ac-sec">
        <SectionHead title="Files" help="Files are reference documents the agent can use: policies, specs, data. PDF, DOCX and text files are read; images, audio and video are stored but not sent to the model yet. Size limits by type: documents 15 MB, images 10 MB, audio 50 MB, video 100 MB."
          action={<button type="button" className="btn ghost sm" onClick={() => setFileModal(true)}><Icon name="i-plus" /> Add</button>} />
        {required.length > 0 && (
          <div className="ac-required" role="group" aria-label="Files this template needs">
            <Banner kind="warn">This template needs {required.length === 1 ? 'a file' : `${required.length} files`} from you before the agent can be published.</Banner>
            <ul className="ac-list">
              {required.map((r, i) => (
                <li key={r.requirement_id || `${r.filename}-${i}`} className="ac-row">
                  <Icon name="i-file" />
                  <div className="ac-row-main">
                    <b>{r.name || r.filename}</b>
                    <span className="small muted">
                      {r.accepted_types?.length ? `Accepts ${r.accepted_types.slice(0, 6).join(', ')}` : r.kind}
                      {r.requirement_id ? '' : ' · save this template again to enable upload'}
                    </span>
                  </div>
                  {r.requirement_id && (
                    <label className={`btn ghost sm${binding === r.requirement_id ? ' disabled' : ''}`} style={{ cursor: 'pointer' }}>
                      <Icon name="i-upload" /> {binding === r.requirement_id ? 'Uploading…' : 'Upload file'}
                      <input type="file" hidden data-testid={`req-input-${r.requirement_id}`} disabled={binding === r.requirement_id}
                        onChange={(e) => { fulfil(r, e.target.files?.[0]); e.target.value = ''; }} />
                    </label>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {attachedFiles.length === 0
          ? <p className="small muted ac-empty">No files added.</p>
          : (
            <ul className="ac-list">
              {attachedFiles.map((f) => (
                <li key={f.id} className="ac-row">
                  <Icon name="i-file" />
                  <div className="ac-row-main">
                    <b>{f.filename}</b>
                    <span className="small muted">{f.kind}</span>
                  </div>
                  {f.retired && <span className="chip line sm" title="This file was retired, but a version you restored uses it">Restored from version</span>}
                  {f.status === 'failed' && <span className="chip line sm" title={f.status_detail}>Can't be read: replace it</span>}
                  {f.status === 'stored_only' && <span className="chip line sm" title={f.status_detail}>Not readable by the agent</span>}
                  <select className="ac-policy" aria-label={`Template setting for ${f.filename}`} value={f.template_policy || 'require'}
                    title="What Save as template does with this file"
                    onChange={(e) => changePolicy(f, e.target.value as TemplatePolicy)}>
                    {(Object.keys(POLICY_LABELS) as TemplatePolicy[]).map((k) => <option key={k} value={k}>{POLICY_LABELS[k]}</option>)}
                  </select>
                  <span className="small muted">{fmtSize(f.size_bytes)}</span>
                  <button type="button" className="icon-action" title="Remove from this node (stays in the library)" aria-label={`Remove file ${f.filename} from this node`} onClick={() => detachFile(f.id)}><Icon name="i-x" /></button>
                </li>
              ))}
            </ul>
          )}
      </section>

      {/* Tools */}
      <section className="ac-sec">
        <SectionHead title="Tools" help="Give the agent tools so it can act beyond the chat: tools, MCP servers and workflows from your workspace. Only entries marked callable can be added today. Some tools need authentication in Integrations before they work."
          action={<button type="button" className="btn ghost sm" onClick={() => setToolPop(!toolPop)} aria-haspopup="dialog" aria-expanded={toolPop}><Icon name="i-plus" /> Add</button>} />
        {toolPop && <ToolPicker catalog={catalog} selected={selectedTools} onPick={addTool} onClose={() => setToolPop(false)} />}
        {toolsInactive && (
          <Banner kind="warn">
            These tools are ignored by the {strategy === 'plan_execute' ? 'Plan & execute' : 'ReAct'} strategy. {' '}
            <button type="button" className="link" onClick={() => set({ strategy: 'function_calling' })}>Use Function calling</button>
          </Banner>
        )}
        {selectedTools.length === 0
          ? <p className="small muted ac-empty">No tools added.</p>
          : (
            <ul className="ac-list">
              {selectedTools.map((t) => {
                const c = entryFor(t);
                const bad = catalog.length > 0 && (!c || !c.callable);
                return (
                  <li key={t} className={`ac-row${toolsInactive ? ' inactive' : ''}`}>
                    <Icon name={c?.source_type === 'mcp' ? 'i-plug' : c?.source_type === 'workflow' ? 'i-flow' : 'i-cog'} />
                    <div className="ac-row-main"><b>{c?.label || t}</b><span className="small muted">{c?.group || 'No longer in the catalog'}</span></div>
                    {toolsInactive && <span className="chip line sm">Inactive</span>}
                    {bad && <span className="chip line sm" title={c?.reason || 'Not in the catalog'}>Can't run</span>}
                    <button type="button" className="icon-action danger" aria-label={`Remove tool ${c?.label || t}`} onClick={() => removeTool(t)}><Icon name="i-x" /></button>
                  </li>
                );
              })}
            </ul>
          )}
        {selectedTools.length > 0 && strategy === 'function_calling' && (
          <p className="small muted ac-note">The agent decides which tool to call and when, up to Max iterations rounds.</p>
        )}
      </section>

      {/* Advanced settings */}
      <section className="ac-sec">
        <button type="button" className="ac-adv-toggle" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>
          <Icon name="i-chev" className={advanced ? 'ac-chev open' : 'ac-chev'} /> Advanced settings
        </button>
        {advanced && (
          <div className="ac-adv">
            <div className="field">
              <label>Agentic strategy</label>
              <SearchSelect ariaLabel="Agentic strategy" noun="strategy" placeholder="Select agentic strategy"
                value={strategy}
                options={strategyLabels}
                onChange={(v) => set({ strategy: v })} />
              <p className="small muted ac-note">{strategyInfo[strategy]}</p>
            </div>
            <div className="field">
              <label>Query <span className="chip line" style={{ float: 'right' }}>{'{{var}}'}</span></label>
              <input className="mono" placeholder="{{input}} — defaults to the prior node's output if unset" value={cfg.query || ''}
                onChange={(e) => setCfg({ ...cfg, query: e.target.value })} onBlur={() => set({ query: cfg.query })} />
            </div>
            {strategy !== 'plan_execute' && (
              <div className="field">
                <label>Max iterations</label>
                <div className="row-split">
                  <input aria-label="Max iterations" value={cfg.max_iterations ?? cfg.max_steps ?? 3} style={{ maxWidth: 70 }}
                    onChange={(e) => setCfg({ ...cfg, max_iterations: e.target.value })} onBlur={() => set({ max_iterations: cfg.max_iterations })} />
                  <input type="range" min={1} max={15} aria-label="Max iterations slider" value={cfg.max_iterations ?? cfg.max_steps ?? 3} style={{ flex: 1 }}
                    onChange={(e) => set({ max_iterations: e.target.value })} />
                </div>
              </div>
            )}
            <div className="row-split" style={{ padding: '8px 0' }}>
              <span className="small">Memory</span>
              <div className={`tog${cfg.memory_enabled ? ' on' : ''}`} role="switch" aria-checked={!!cfg.memory_enabled} aria-label="Memory"
                onClick={() => set({ memory_enabled: !cfg.memory_enabled })} />
            </div>

            <div className="ac-head" style={{ marginTop: 8 }}>
              <span className="ac-title">Env<HelpTip text="Environment values for this node only, stored encrypted and never shown again after saving. Use {{env.KEY}} in the prompt or query. Templates keep the key names but never the values." /></span>
              <span className="ac-env-actions">
                <label className="btn ghost sm" style={{ cursor: 'pointer' }}>
                  <Icon name="i-download" /> Import .env
                  <input type="file" hidden data-testid="env-import" onChange={(e) => { importEnv(e.target.files?.[0]); e.target.value = ''; }} />
                </label>
                <button type="button" className="btn ghost sm" aria-label="Add env value" onClick={() => setEnvRows([...envRows, { key: '', draft: '', saved: false }])}><Icon name="i-plus" /> Add</button>
              </span>
            </div>
            {envRows.length === 0 ? <p className="small muted ac-empty">No environment values.</p> : (
              <div className="ac-env">
                <div className="ac-env-row ac-env-h"><span>KEY</span><span>VALUE</span><span /></div>
                {envRows.map((r, i) => (
                  <div key={i} className="ac-env-row">
                    <input className="mono" aria-label={`Env key ${i + 1}`} placeholder="KEY" value={r.key} readOnly={r.saved}
                      title={r.saved ? 'Delete this row and add it again to rename the key' : undefined}
                      aria-invalid={dupKeys.has(r.key) || (!!r.key && !ENV_KEY_RE.test(r.key))}
                      onChange={(e) => setEnvRows(envRows.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}
                      onBlur={() => saveRow(envRows)} />
                    <input className="mono" type="password" autoComplete="new-password" aria-label={`Env value ${i + 1}`}
                      placeholder={r.saved ? '•••••••• (saved — type to replace)' : 'VALUE'} value={r.draft}
                      onChange={(e) => setEnvRows(envRows.map((x, j) => (j === i ? { ...x, draft: e.target.value } : x)))}
                      onBlur={() => saveRow(envRows)} />
                    <button type="button" className="icon-action danger" aria-label={`Delete env ${r.key || i + 1}`} onClick={() => deleteRow(i)}><Icon name="i-trash" /></button>
                  </div>
                ))}
                {dupKeys.size > 0 && <p className="small ac-err">Duplicate keys: {[...dupKeys].join(', ')}</p>}
              </div>
            )}
          </div>
        )}
      </section>

      <SecHead>Output variables</SecHead>
      <KV k="agent_final_answer" v={<span className="small muted">string</span>} />
      <KV k="agent_tool_outputs" v={<span className="small muted">array — one entry per tool call</span>} />
      <KV k="agent_reasoning_trace" v={<span className="small muted">array — step-by-step</span>} />
      <KV k="agent_iteration_count" v={<span className="small muted">number</span>} />
      <KV k="agent_success" v={<span className="small muted">boolean</span>} />

      {fileModal && (
        <UploadFileModal agentId={agent.id} library={libraryFiles} onClose={() => setFileModal(false)}
          onUploaded={onFilesUploaded} onAttach={(id) => { attachFile(id); }} onDelete={deleteFile} toast={toast} />
      )}
    </div>
  );
}

function ToolPicker({ catalog, selected, onPick, onClose }: {
  catalog: AgentToolEntry[]; selected: string[]; onPick: (v: string) => void; onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const [tab, setTab] = useState<ToolTab>('all');
  const tabs: { id: ToolTab; label: string }[] = [
    { id: 'all', label: 'All' }, { id: 'tool', label: 'Tools' }, { id: 'mcp', label: 'MCP' }, { id: 'workflow', label: 'Workflows' },
  ];
  const shown = catalog.filter((c) => (tab === 'all' || c.source_type === tab)
    && (`${c.label} ${c.group}`.toLowerCase().includes(q.trim().toLowerCase())));
  const groups = shown.reduce<Record<string, AgentToolEntry[]>>((acc, c) => { (acc[c.group] = acc[c.group] || []).push(c); return acc; }, {});
  return (
    <div className="ac-pop" role="dialog" aria-label="Add tool">
      <div className="ac-pop-search">
        <Icon name="i-search" />
        <input autoFocus placeholder="Search tools" aria-label="Search tools" value={q} onChange={(e) => setQ(e.target.value)} />
        <button type="button" className="icon-action" aria-label="Close tool picker" onClick={onClose}><Icon name="i-x" /></button>
      </div>
      <div className="ac-tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} className={`ac-tab${tab === t.id ? ' on' : ''}`} onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>
      <div className="ac-pop-list">
        {shown.length === 0 && <p className="small muted" style={{ padding: 12 }}>{catalog.length === 0 ? 'Nothing in Integrations yet.' : 'No matches.'}</p>}
        {Object.entries(groups).map(([g, items]) => (
          <div key={g}>
            <div className="ac-group">{g}</div>
            {items.map((c) => {
              const on = selected.includes(c.value);
              return (
                <button key={c.value} type="button" className="ac-pop-item" disabled={on || !c.callable} onClick={() => onPick(c.value)}
                  title={c.callable ? c.desc : c.reason || undefined}>
                  <span>{c.label}</span>
                  {on ? <span className="chip green sm">Added</span> : !c.callable ? <span className="chip line sm">Not callable yet</span> : null}
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function UploadFileModal({ agentId, library, onClose, onUploaded, onAttach, onDelete, toast }: {
  agentId: string; library: AgentFileRef[]; onClose: () => void; onUploaded: (f: AgentFileRef[]) => void;
  onAttach: (id: string) => void; onDelete: (id: string) => void; toast: (m: string) => void;
}) {
  const [picked, setPicked] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const problems = picked.map((f) => fileProblem(f));
  const add = (list: FileList | null) => { if (list) setPicked((p) => [...p, ...Array.from(list)]); };
  const upload = async () => {
    setBusy(true);
    const done: AgentFileRef[] = [];
    const failed: string[] = [];
    for (const f of picked) {
      try { done.push(await api.uploadAgentFile(agentId, f)); } catch (e: any) { failed.push(`${f.name}: ${e.message}`); }
    }
    setBusy(false);
    onUploaded(done);
    if (failed.length) { toast(failed.join('\n')); if (!done.length) return; }
    onClose();
  };
  const ok = picked.length > 0 && problems.every((p) => p === null);
  return (
    <Modal title="Upload file" onClose={onClose}
      footer={(
        <>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" disabled={!ok || busy} onClick={upload}>{busy ? 'Uploading…' : 'Upload'}</button>
        </>
      )}>
      <div className={`ac-drop${over ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); }}>
        <Icon name="i-upload" />
        <p>Drag and drop files here, or <button type="button" className="link" onClick={() => input.current?.click()}>browse</button></p>
        <input ref={input} type="file" multiple hidden data-testid="file-input" onChange={(e) => { add(e.target.files); e.target.value = ''; }} />
      </div>
      <p className="small muted ac-note">Limits: documents {FILE_LIMITS_MB.document} MB, images {FILE_LIMITS_MB.image} MB, audio {FILE_LIMITS_MB.audio} MB, video {FILE_LIMITS_MB.video} MB. The agent reads PDF, DOCX and text files; images, audio and video are stored but not sent to the model yet.</p>
      {picked.length > 0 && (
        <ul className="ac-list">
          {picked.map((f, i) => (
            <li key={i} className="ac-row">
              <Icon name="i-file" />
              <div className="ac-row-main"><b>{f.name}</b>{problems[i] && <span className="small ac-err">{problems[i]}</span>}</div>
              <span className="small muted">{fmtSize(f.size)}</span>
              <button type="button" className="icon-action" aria-label={`Remove ${f.name}`} onClick={() => setPicked(picked.filter((_, j) => j !== i))}><Icon name="i-x" /></button>
            </li>
          ))}
        </ul>
      )}
      {library.length > 0 && (
        <>
          <div className="ac-group" style={{ paddingLeft: 0 }}>Already in this agent's library</div>
          <ul className="ac-list">
            {library.map((f) => (
              <li key={f.id} className="ac-row">
                <Icon name="i-file" />
                <div className="ac-row-main"><b>{f.filename}</b><span className="small muted">{f.kind}</span></div>
                <span className="small muted">{fmtSize(f.size_bytes)}</span>
                <button type="button" className="btn ghost sm" onClick={() => { onAttach(f.id); onClose(); }}>Attach</button>
                <button type="button" className="icon-action danger" aria-label={`Delete ${f.filename} permanently`} onClick={() => onDelete(f.id)}><Icon name="i-trash" /></button>
              </li>
            ))}
          </ul>
        </>
      )}
    </Modal>
  );
}
