import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import AgentConfigurePanel, {
  classifyFile, fileProblem, skillProblem, parseDotenv, modelProblem, fmtSize,
} from '../components/AgentConfigurePanel';
import { api } from '../api';
import type { Agent, AgentFileRef, AgentSkillRef, AgentToolEntry, LlmModelOption } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

const agent = { id: 'a1', name: 'Me', plane: 'App' } as Agent;
const mdl = (o: Partial<LlmModelOption>): LlmModelOption => ({
  name: 'M', provider: 'P', api_model_id: 'x', builtin: true, available: true, inferred: false, types: ['LLM'], note: null,
  supports_chat: true, supports_tool_calling: true, supports_vision: false, ...o,
} as LlmModelOption);
const models: LlmModelOption[] = [
  mdl({ name: 'Claude Sonnet 5' }),
  mdl({ name: 'Old Model', available: false, note: 'retired' }),
  mdl({ name: 'Embedder', types: ['Embedding'], supports_chat: false, supports_tool_calling: false }),
  mdl({ name: 'No Tools', supports_tool_calling: false }),
];
const catalog: AgentToolEntry[] = [
  { value: 'tool:Slack:Post message', label: 'Post message', group: 'Slack', source_type: 'tool', callable: true, reason: null },
  { value: 'mcp:github', label: 'GitHub MCP', group: 'MCP servers', source_type: 'mcp', callable: false, reason: 'MCP servers are not callable from an Agent node yet.' },
  { value: 'workflow:a2', label: 'Refund flow', group: 'Workflows', source_type: 'workflow', callable: false, reason: 'Workflows are not callable yet.' },
];
const skill = (o: Partial<AgentSkillRef> = {}): AgentSkillRef => ({ id: 'sk-1', name: 'refund-policy', description: 'd', size_bytes: 10, file_count: 1, created_at: '', ...o });
const file = (o: Partial<AgentFileRef> = {}): AgentFileRef => ({ id: 'af-1', filename: 'p.txt', mime: 'text/plain', kind: 'document', size_bytes: 3, status: 'ready', status_detail: '', has_text: true, created_at: '', ...o });

function mount(initial: Record<string, any> = {}) {
  let cfg = { ...initial };
  const set = vi.fn((patch: Record<string, any>) => { cfg = { ...cfg, ...patch }; view.rerender(el()); });
  const setCfg = vi.fn((c: Record<string, any>) => { cfg = c; view.rerender(el()); });
  const toast = vi.fn();
  const onAgentUpdate = vi.fn();
  const el = () => (
    <AgentConfigurePanel agent={agent} nodeId="n1" cfg={cfg} set={set} setCfg={setCfg} toast={toast} onAgentUpdate={onAgentUpdate}
      modelSelect={<div>model-select</div>} llmModels={models}
      strategyLabels={[{ value: 'react', label: 'ReAct' }, { value: 'function_calling', label: 'Function calling' }]}
      strategyInfo={{ react: 'r', function_calling: 'f' }} />
  );
  const view = render(el());
  return { set, setCfg, toast, onAgentUpdate, getCfg: () => cfg };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAgentSkills).mockResolvedValue([]);
  vi.mocked(api.listAgentFiles).mockResolvedValue([]);
  vi.mocked(api.getAgentToolCatalog).mockResolvedValue({ tools: catalog });
  vi.mocked(api.listNodeSecrets).mockResolvedValue([]);
});

describe('helpers', () => {
  it('classifies files and enforces per-type limits', () => {
    expect(classifyFile('a.PDF')).toBe('document');
    expect(classifyFile('a.docx')).toBe('document');
    expect(classifyFile('a.png')).toBe('image');
    expect(classifyFile('a.mp3')).toBe('audio');
    expect(classifyFile('a.mp4')).toBe('video');
    expect(classifyFile('a.exe')).toBeNull();
    const MB = 1024 * 1024;
    expect(fileProblem({ name: 'a.pdf', size: 15 * MB })).toBeNull();
    expect(fileProblem({ name: 'a.pdf', size: 15 * MB + 1 })).toContain('15 MB');
    expect(fileProblem({ name: 'a.png', size: 10 * MB + 1 })).toContain('10 MB');
    expect(fileProblem({ name: 'a.mp3', size: 50 * MB + 1 })).toContain('50 MB');
    expect(fileProblem({ name: 'a.mp4', size: 100 * MB })).toBeNull();
    expect(fileProblem({ name: 'a.mp4', size: 100 * MB + 1 })).toContain('100 MB');
    expect(fileProblem({ name: 'a.exe', size: 1 })).toContain('not a supported');
    expect(fileProblem({ name: 'a.txt', size: 0 })).toContain('empty');
  });
  it('refuses office formats the agent cannot read, with a way forward', () => {
    for (const n of ['a.xlsx', 'a.pptx', 'a.doc', 'a.xls', 'a.ppt', 'a.epub', 'a.rtf']) {
      const p = fileProblem({ name: n, size: 10 });
      expect(p).toContain("can't be read");
      expect(p).toContain('PDF, DOCX');
    }
  });
  it('validates skill packages', () => {
    expect(skillProblem({ name: 'x.zip', size: 10 })).toBeNull();
    expect(skillProblem({ name: 'x.SKILL', size: 10 })).toBeNull();
    expect(skillProblem({ name: 'x.tar', size: 10 })).toContain('.zip or .skill');
    expect(skillProblem({ name: 'x.zip', size: 50 * 1024 * 1024 + 1 })).toContain('50 MB');
  });
  it('parses .env text', () => {
    const rows = parseDotenv('# c\nA=1\nexport B="two words"\nC=\'x\'\nD=v # tail\nbad line\n');
    expect(rows).toEqual([{ key: 'A', value: '1' }, { key: 'B', value: 'two words' }, { key: 'C', value: 'x' }, { key: 'D', value: 'v' }]);
  });
  it('explains why a model cannot run the node', () => {
    const none = { toolCalling: false };
    expect(modelProblem('Claude Sonnet 5', models, none)).toBeNull();
    expect(modelProblem('Old Model', models, none)).toBe('retired');
    expect(modelProblem('Embedder', models, none)).toContain('chat model');
    expect(modelProblem('Not in catalog', models, none)).toContain('catalog');
    expect(modelProblem('Anything', [], none)).toBeNull();
    // tool calling only matters when the node will use tools
    expect(modelProblem('No Tools', models, none)).toBeNull();
    expect(modelProblem('No Tools', models, { toolCalling: true })).toContain('tool calling');
    expect(fmtSize(2048)).toBe('2 KB');
  });
});

describe('Agent node Configure panel', () => {
  it('shows the six sections in order', () => {
    mount();
    const titles = Array.from(document.querySelectorAll('.ac-title, .ac-adv-toggle')).map((e) => e.textContent?.trim().replace(/\s+/g, ' '));
    expect(titles.slice(0, 5)).toEqual(['Model', 'Prompt', 'Skills', 'Files', 'Tools']);
    expect(document.body.textContent).toContain('Advanced settings');
  });

  it('reads the legacy instructions field into the Prompt and writes prompt', () => {
    const { set } = mount({ instructions: 'Old persona' });
    const ta = screen.getByLabelText('Prompt') as HTMLTextAreaElement;
    expect(ta.value).toBe('Old persona');
    fireEvent.change(ta, { target: { value: 'New persona' } });
    fireEvent.blur(ta);
    expect(set).toHaveBeenCalledWith({ prompt: 'New persona' });
  });

  it('shows the Incompatible badge for an unusable model only', () => {
    mount({ model: 'Old Model' });
    expect(screen.getByText('Incompatible')).toBeTruthy();
    cleanup();
    mount({ model: 'Claude Sonnet 5' });
    expect(screen.queryByText('Incompatible')).toBeNull();
  });

  it('help text opens on click', () => {
    mount();
    fireEvent.click(screen.getAllByLabelText('What is this?')[0]);
    expect(screen.getByRole('note').textContent).toContain('tool calling');
  });

  it('uploads a skill from the Add menu and records its id', async () => {
    vi.mocked(api.uploadAgentSkill).mockResolvedValue({ id: 'sk-1', name: 'refund-policy', description: 'd', size_bytes: 2048, file_count: 2, created_at: '' });
    const { set } = mount();
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);
    expect(screen.getByText('EMBEDDED')).toBeTruthy();
    const file = new File(['x'], 's.zip', { type: 'application/zip' });
    fireEvent.change(screen.getByTestId('skill-input'), { target: { files: [file] } });
    await waitFor(() => expect(set).toHaveBeenCalledWith({ skills: ['sk-1'] }));
    expect(await screen.findByText('refund-policy')).toBeTruthy();
  });

  it('rejects a wrong skill file before uploading', async () => {
    const { toast } = mount();
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);
    fireEvent.change(screen.getByTestId('skill-input'), { target: { files: [new File(['x'], 's.tar')] } });
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(api.uploadAgentSkill).not.toHaveBeenCalled();
  });

  it('Remove on a skill only detaches it from the node; the library copy is untouched', async () => {
    vi.mocked(api.listAgentSkills).mockResolvedValue([skill()]);
    const { set } = mount({ skills: ['sk-1'] });
    fireEvent.click(await screen.findByLabelText('Remove skill refund-policy from this node'));
    await waitFor(() => expect(set).toHaveBeenCalledWith({ skills: [] }));
    expect(api.deleteAgentSkill).not.toHaveBeenCalled();
  });

  it('a detached skill is offered again from the library, and can be attached or deleted there', async () => {
    vi.mocked(api.listAgentSkills).mockResolvedValue([skill()]);
    vi.mocked(api.deleteAgentSkill).mockResolvedValue({ ok: true, outcome: 'deleted' });
    const { set } = mount({ skills: [] });
    await waitFor(() => expect(api.listAgentSkills).toHaveBeenCalled());
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);
    fireEvent.click(await screen.findByText('refund-policy'));
    expect(set).toHaveBeenCalledWith({ skills: ['sk-1'] });
    // delete is an explicit, separate action, on a skill that is not attached
    cleanup();
    mount({ skills: [] });
    await waitFor(() => expect(api.listAgentSkills).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);
    fireEvent.click(await screen.findByLabelText('Delete skill refund-policy permanently'));
    await waitFor(() => expect(api.deleteAgentSkill).toHaveBeenCalledWith('a1', 'sk-1'));
  });

  it('surfaces the 409 when deleting a library skill that a draft node still uses', async () => {
    vi.mocked(api.listAgentSkills).mockResolvedValue([skill()]);
    vi.mocked(api.deleteAgentSkill).mockRejectedValue(new Error('Used by draft node n2; detach it first.'));
    const { toast } = mount({ skills: [] });
    await waitFor(() => expect(api.listAgentSkills).toHaveBeenCalled());
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);
    fireEvent.click(await screen.findByLabelText('Delete skill refund-policy permanently'));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Used by draft node n2; detach it first.'));
    expect(screen.queryByText('refund-policy')).toBeTruthy(); // still listed
  });

  it('says every attached skill is included in every run', async () => {
    vi.mocked(api.listAgentSkills).mockResolvedValue([skill(), skill({ id: 'sk-2', name: 'tone' })]);
    mount({ skills: ['sk-1', 'sk-2'] });
    expect(await screen.findByText(/All 2 skills are included in every run/)).toBeTruthy();
  });

  it('opens the Upload file modal, blocks oversize files and uploads valid ones', async () => {
    vi.mocked(api.uploadAgentFile).mockResolvedValue(file());
    const { set } = mount();
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[1]);
    const modal = screen.getByText('Upload file').closest('.modal') as HTMLElement;
    expect(modal.textContent).toContain('documents 15 MB');
    expect(modal.textContent).toContain('video 100 MB');
    expect(modal.textContent).not.toContain('50 MB limit for agent-created');
    const upload = within(modal).getByRole('button', { name: 'Upload' }) as HTMLButtonElement;
    expect(upload.disabled).toBe(true);

    const big = new File(['x'], 'big.mp4'); Object.defineProperty(big, 'size', { value: 101 * 1024 * 1024 });
    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [big] } });
    expect(modal.textContent).toContain('over the 100 MB limit');
    expect((within(modal).getByRole('button', { name: 'Upload' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(modal).getByLabelText('Remove big.mp4'));

    const xl = new File(['x'], 'sheet.xlsx');
    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [xl] } });
    expect(modal.textContent).toContain("can't be read");
    fireEvent.click(within(modal).getByLabelText('Remove sheet.xlsx'));

    fireEvent.change(screen.getByTestId('file-input'), { target: { files: [new File(['abc'], 'p.txt')] } });
    fireEvent.click(within(modal).getByRole('button', { name: 'Upload' }));
    await waitFor(() => expect(set).toHaveBeenCalledWith({ files: ['af-1'] }));
    expect(screen.queryByText('Upload file')).toBeNull();
  });

  it('marks stored-only files as not readable by the agent, and Remove only detaches', async () => {
    vi.mocked(api.listAgentFiles).mockResolvedValue([
      file(), file({ id: 'af-2', filename: 'clip.mp4', kind: 'video', status: 'stored_only', status_detail: 'Video is stored but not sent to the model.' }),
    ]);
    const { set } = mount({ files: ['af-1', 'af-2'] });
    expect(await screen.findByText('clip.mp4')).toBeTruthy();
    expect(screen.getAllByText('Not readable by the agent')).toHaveLength(1);
    fireEvent.click(screen.getByLabelText('Remove file clip.mp4 from this node'));
    expect(set).toHaveBeenCalledWith({ files: ['af-1'] });
    expect(api.deleteAgentFile).not.toHaveBeenCalled();
  });

  it('Cancel closes the upload modal without uploading', () => {
    mount();
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[1]);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Upload file')).toBeNull();
    expect(api.uploadAgentFile).not.toHaveBeenCalled();
  });

  it('tool picker lists non-callable entries disabled with a reason, filters, and adds a callable one', async () => {
    const { set } = mount({ strategy: 'react' });
    fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[2]);
    const pop = screen.getByRole('dialog', { name: 'Add tool' });
    expect(await within(pop).findByText('Post message')).toBeTruthy();
    const mcp = within(pop).getByText('GitHub MCP').closest('button') as HTMLButtonElement;
    expect(mcp.disabled).toBe(true);
    expect(mcp.title).toContain('not callable');
    expect(within(pop).getAllByText('Not callable yet').length).toBeGreaterThan(0);
    fireEvent.click(mcp);
    expect(set).not.toHaveBeenCalled();

    fireEvent.click(within(pop).getByRole('tab', { name: 'MCP' }));
    expect(within(pop).queryByText('Post message')).toBeNull();
    fireEvent.click(within(pop).getByRole('tab', { name: 'All' }));
    fireEvent.change(within(pop).getByLabelText('Search tools'), { target: { value: 'slack' } });
    expect(within(pop).queryByText('GitHub MCP')).toBeNull();
    fireEvent.click(within(pop).getByText('Post message'));
    expect(set).toHaveBeenCalledWith({ tools: ['tool:Slack:Post message'], strategy: 'function_calling' });
  });

  it('removes a tool', async () => {
    const { set } = mount({ tools: ['tool:Slack:Post message'], strategy: 'function_calling' });
    fireEvent.click(await screen.findByLabelText('Remove tool Post message'));
    expect(set).toHaveBeenCalledWith({ tools: [] });
  });

  it('warns that tools are inactive under ReAct and offers to switch strategy', async () => {
    const { set } = mount({ tools: ['tool:Slack:Post message'], strategy: 'react' });
    expect(await screen.findByText(/ignored by the ReAct strategy/)).toBeTruthy();
    expect(screen.getByText('Inactive')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Use Function calling' }));
    expect(set).toHaveBeenCalledWith({ strategy: 'function_calling' });
    expect(screen.queryByText(/ignored by the/)).toBeNull();
  });

  it('flags a selected tool that is no longer callable', async () => {
    mount({ tools: ['mcp:github'], strategy: 'function_calling' });
    expect(await screen.findByText("Can't run")).toBeTruthy();
  });

  it('Incompatible badge explains the missing tool-calling capability only when tools are used', async () => {
    mount({ model: 'No Tools', tools: ['tool:Slack:Post message'], strategy: 'function_calling' });
    const badge = await screen.findByText('Incompatible');
    expect(badge.getAttribute('title')).toContain('tool calling');
    cleanup();
    mount({ model: 'No Tools', strategy: 'react' });
    expect(screen.queryByText('Incompatible')).toBeNull();
  });

  it('Advanced settings is collapsed, expands, and keeps the old controls', () => {
    mount({ strategy: 'react', max_iterations: 4, memory_enabled: true });
    expect(screen.queryByLabelText('Max iterations')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    expect((screen.getByLabelText('Max iterations') as HTMLInputElement).value).toBe('4');
    expect(screen.getByRole('switch', { name: 'Memory' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('button', { name: 'Agentic strategy' })).toBeTruthy();
  });

  // The server returns the saved agent; the panel hands it to the builder and re-reads its own config from it.
  const savedAgent = (env: { key: string }[], extra: Record<string, any> = {}) =>
    ({ id: 'a1', draft_revision: 9, flow: { nodes: [{ id: 'n1', type: 'agent', config: { env, ...extra } }], edges: [] } }) as unknown as Agent;

  it('Env is saved through one transactional call; values never enter the node config', async () => {
    vi.mocked(api.putNodeEnv).mockResolvedValue({ entries: [{ key: 'REGION', configured: true }], draft_revision: 9, agent: savedAgent([{ key: 'REGION' }]) });
    const { getCfg, onAgentUpdate } = mount({});
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Add env value' }));
    fireEvent.change(screen.getByLabelText('Env key 1'), { target: { value: 'REGION' } });
    fireEvent.change(screen.getByLabelText('Env value 1'), { target: { value: 'eu-secret' } });
    expect((screen.getByLabelText('Env value 1') as HTMLInputElement).type).toBe('password');
    fireEvent.blur(screen.getByLabelText('Env value 1'));
    await waitFor(() => expect(api.putNodeEnv).toHaveBeenCalledTimes(1));
    expect(api.putNodeEnv).toHaveBeenCalledWith('a1', 'n1', [{ key: 'REGION', value: 'eu-secret' }], undefined);
    await waitFor(() => expect(onAgentUpdate).toHaveBeenCalled());
    await waitFor(() => expect(getCfg().env).toEqual([{ key: 'REGION' }]));
    expect(JSON.stringify(getCfg())).not.toContain('eu-secret');
    const v = (await screen.findByLabelText('Env value 1')) as HTMLInputElement;
    expect(v.value).toBe('');
    expect(v.placeholder).toContain('saved');
  });

  it('sends the whole key list, and only rows with a new value carry one', async () => {
    vi.mocked(api.listNodeSecrets).mockResolvedValue([{ key: 'OLD', configured: true }]);
    vi.mocked(api.putNodeEnv).mockResolvedValue({ entries: [], draft_revision: 9, agent: savedAgent([{ key: 'OLD' }, { key: 'NEW' }]) });
    mount({ env: [{ key: 'OLD' }] });
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    await waitFor(() => expect((screen.getByLabelText('Env key 1') as HTMLInputElement).readOnly).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Add env value' }));
    fireEvent.change(screen.getByLabelText('Env key 2'), { target: { value: 'NEW' } });
    fireEvent.change(screen.getByLabelText('Env value 2'), { target: { value: 'v' } });
    fireEvent.blur(screen.getByLabelText('Env value 2'));
    await waitFor(() => expect(api.putNodeEnv).toHaveBeenCalled());
    expect(vi.mocked(api.putNodeEnv).mock.calls[0][2]).toEqual([{ key: 'OLD' }, { key: 'NEW', value: 'v' }]);
  });

  it('a conflict (409) is shown and nothing is adopted', async () => {
    vi.mocked(api.putNodeEnv).mockRejectedValue(new Error('This agent changed since you loaded it'));
    const { toast, onAgentUpdate } = mount({});
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Add env value' }));
    fireEvent.change(screen.getByLabelText('Env key 1'), { target: { value: 'K' } });
    fireEvent.change(screen.getByLabelText('Env value 1'), { target: { value: 'v' } });
    fireEvent.blur(screen.getByLabelText('Env value 1'));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('This agent changed since you loaded it'));
    expect(onAgentUpdate).not.toHaveBeenCalled();
  });

  it('configured env rows show a masked placeholder, lock the key, and deleting a row saves the shorter list', async () => {
    vi.mocked(api.listNodeSecrets).mockResolvedValue([{ key: 'TOKEN', configured: true }]);
    vi.mocked(api.putNodeEnv).mockResolvedValue({ entries: [], draft_revision: 9, agent: savedAgent([]) });
    const { getCfg } = mount({ env: [{ key: 'TOKEN' }] });
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    const key = (await screen.findByLabelText('Env key 1')) as HTMLInputElement;
    await waitFor(() => expect(key.readOnly).toBe(true));
    expect((screen.getByLabelText('Env value 1') as HTMLInputElement).placeholder).toContain('••••');
    fireEvent.click(screen.getByLabelText('Delete env TOKEN'));
    await waitFor(() => expect(api.putNodeEnv).toHaveBeenCalledWith('a1', 'n1', [], undefined));
    await waitFor(() => expect(getCfg().env).toEqual([]));
  });

  it('flags duplicate env keys and does not save them', () => {
    mount({ env: [{ key: 'A' }, { key: 'A' }] });
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    expect(document.body.textContent).toContain('Duplicate keys: A');
  });

  it('Import .env sends every pair in a single call', async () => {
    vi.mocked(api.putNodeEnv).mockResolvedValue({ entries: [], draft_revision: 9, agent: savedAgent([{ key: 'A' }, { key: 'B' }]) });
    const { getCfg } = mount({});
    fireEvent.click(screen.getByRole('button', { name: /Advanced settings/ }));
    const f = new File(['A=new\nB=2\n'], '.env');
    (f as any).text = async () => 'A=new\nB=2\n';
    fireEvent.change(screen.getByTestId('env-import'), { target: { files: [f] } });
    await waitFor(() => expect(api.putNodeEnv).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.putNodeEnv).mock.calls[0][2]).toEqual([{ key: 'A', value: 'new' }, { key: 'B', value: '2' }]);
    await waitFor(() => expect(getCfg().env).toEqual([{ key: 'A' }, { key: 'B' }]));
    expect(JSON.stringify(getCfg())).not.toContain('"new"');
  });

  // ── template requirements and file policy ───────────────────────────────
  const REQ = { requirement_id: 'req-abc', name: 'customer-policy.pdf', filename: 'customer-policy.pdf', kind: 'document', accepted_types: ['pdf', 'docx', 'txt'], required: true };

  it('a required template file can be uploaded and is bound to its requirement', async () => {
    vi.mocked(api.uploadAgentFile).mockResolvedValue(file({ id: 'af-9', filename: 'mine.txt' }));
    vi.mocked(api.bindFileRequirement).mockResolvedValue({ files: ['af-9'], files_required: [], draft_revision: 9,
      agent: savedAgent([], { files: ['af-9'] }) });
    const { getCfg, onAgentUpdate } = mount({ files_required: [REQ] });
    expect(document.body.textContent).toContain('needs a file from you');
    expect(document.body.textContent).toContain('Accepts pdf, docx, txt');
    fireEvent.change(screen.getByTestId('req-input-req-abc'), { target: { files: [new File(['policy'], 'mine.txt')] } });
    await waitFor(() => expect(api.bindFileRequirement).toHaveBeenCalledWith('a1', 'n1', 'req-abc', 'af-9', undefined));
    await waitFor(() => expect(onAgentUpdate).toHaveBeenCalled());
    await waitFor(() => expect(getCfg().files).toEqual(['af-9']));
    expect(getCfg().files_required).toBeUndefined();
  });

  it('refuses a file of the wrong type for a requirement before uploading', async () => {
    const { toast } = mount({ files_required: [REQ] });
    fireEvent.change(screen.getByTestId('req-input-req-abc'), { target: { files: [new File(['x'], 'pic.png')] } });
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(String(toast.mock.calls[0][0])).toContain("isn't an accepted type");
    expect(api.uploadAgentFile).not.toHaveBeenCalled();
  });

  it('if binding fails after the upload, says where the file is', async () => {
    vi.mocked(api.uploadAgentFile).mockResolvedValue(file({ id: 'af-9', filename: 'mine.txt' }));
    vi.mocked(api.bindFileRequirement).mockRejectedValue(new Error('changed since you loaded it'));
    const { toast } = mount({ files_required: [REQ] });
    fireEvent.change(screen.getByTestId('req-input-req-abc'), { target: { files: [new File(['p'], 'mine.txt')] } });
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(String(toast.mock.calls[0][0])).toContain('uploaded to the library');
  });

  it('a requirement from an older package without an id explains how to fix it instead of offering upload', () => {
    mount({ files_required: [{ filename: 'old.pdf', kind: 'document' }] });
    expect(document.body.textContent).toContain('save this template again');
    expect(document.querySelector('[data-testid^="req-input-"]')).toBeNull();
  });

  it('each file has a template setting (default: require upload) that can be changed', async () => {
    vi.mocked(api.listAgentFiles).mockResolvedValue([file()]);
    vi.mocked(api.setFileTemplatePolicy).mockResolvedValue(file({ template_policy: 'embed' }));
    mount({ files: ['af-1'] });
    const sel = (await screen.findByLabelText('Template setting for p.txt')) as HTMLSelectElement;
    expect(sel.value).toBe('require');
    fireEvent.change(sel, { target: { value: 'embed' } });
    await waitFor(() => expect(api.setFileTemplatePolicy).toHaveBeenCalledWith('a1', 'af-1', 'embed'));
    await waitFor(() => expect((screen.getByLabelText('Template setting for p.txt') as HTMLSelectElement).value).toBe('embed'));
  });

  it('marks unreadable files as needing replacement and retired-but-restored assets as such', async () => {
    vi.mocked(api.listAgentFiles).mockResolvedValue([file({ id: 'af-1', filename: 'scan.pdf', status: 'failed', status_detail: 'no text' }), file({ id: 'af-2', filename: 'old.txt', retired: true })]);
    vi.mocked(api.listAgentSkills).mockResolvedValue([skill({ retired: true })]);
    mount({ files: ['af-1', 'af-2'], skills: ['sk-1'] });
    expect(await screen.findByText("Can't be read: replace it")).toBeTruthy();
    expect(screen.getAllByText('Restored from version')).toHaveLength(2);
  });
});
