import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import ToastHost from '../components/ToastHost';
import Templates from '../pages/Templates';
import TemplateDetailsModal from '../modals/TemplateDetailsModal';
import { api } from '../api';
import type { Agent, SaveTemplateResult, Template } from '../types';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});
afterEach(() => cleanup());

const FLOW = { nodes: [{ id: 't1', type: 'trigger', x: 0, y: 0, sub: '', config: {} }, { id: 't2', type: 'respond', x: 1, y: 0, sub: '', config: {} }], edges: [] };
const REQS = { node_types: ['trigger', 'respond'], knowledge_bases: [], catalog_objects: [], tools: [], models: [], env_vars: [], review: [] };
function tpl(overrides: Partial<Template> = {}): Template {
  return {
    id: 'tpl-x', key: 'x', name: 'X', type: 'App', app_type: 'workflow', desc: 'desc', first_party: true, flow: FLOW,
    source: 'platform', origin: 'release', status: 'published', version: '1.0.0', category: 'Service', vertical: null, tags: [], requirements: REQS,
    owner: 'release', release_managed: true, hidden: false, draft_revision: 1, published_revision: 1,
    current_version_id: 'tplv-x', published_at: '2026-01-01T00:00:00', published_by: 'release', has_unpublished_changes: false,
    created_at: '2026-01-01T00:00:00', updated_at: '2026-01-01T00:00:00', ...overrides,
  } as Template;
}
const PLATFORM = tpl({ id: 'tpl-platform', key: 'platform', name: 'Support Bot' });
const PRIVATE = tpl({ id: 'tpl-private', key: 'private', name: 'My Triage', source: 'private', origin: 'private', first_party: false, owner: 'priya', release_managed: false });
const DRAFT = tpl({ id: 'tpl-draft', key: 'draft', name: 'Draft Triage', source: 'private', origin: 'private', first_party: false, owner: 'priya', status: 'draft', version: '0.0.0', current_version_id: null, published_revision: null, has_unpublished_changes: false, release_managed: false });
const CHANGED = tpl({ id: 'tpl-changed', key: 'changed', name: 'Changed Triage', source: 'private', origin: 'private', first_party: false, owner: 'priya', release_managed: false, draft_revision: 2, published_revision: 1, has_unpublished_changes: true });
function agent(overrides: Partial<Agent> = {}): Agent {
  return { id: 'a1', name: 'Lead Scorer', type: 'App', subtype: 'Workflow', plane: 'App', owner: 'you', status: 'draft', version: 'v1',
    health_score: null, health_status: '—', tags: [], description: 'Scores leads.', updated_at: '2026-01-01T00:00:00', model: 'Claude Sonnet 5', mode: 'agentic',
    runs_7d: 0, cost_7d: '$0.00', first_party: false, metrics: {}, flow: FLOW, features: {}, draft_revision: 1, ...overrides } as Agent;
}
function saved(t: Template = DRAFT): SaveTemplateResult { return { template: t, redactions: [], issues: [] }; }
function renderPage() { return render(<MemoryRouter><AppProvider><ToastHost /><Templates /></AppProvider></MemoryRouter>); }

describe('Template management lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    grantPermissions();
    vi.mocked(api.listTemplates).mockResolvedValue([PLATFORM, PRIVATE]);
    vi.mocked(api.listManagedTemplates).mockResolvedValue([PLATFORM, DRAFT, CHANGED]);
  });

  it('published gallery contains Platform and Private published templates', async () => {
    renderPage(); await screen.findByText('Support Bot');
    expect(screen.getByText('My Triage')).toBeInTheDocument();
    expect(screen.getAllByText('Platform provided').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Private').length).toBeGreaterThan(0);
  });

  it('Manage templates exposes drafts and unpublished changes without making release packages editable', async () => {
    renderPage(); await screen.findByText('Support Bot');
    fireEvent.click(screen.getByRole('button', { name: 'Manage templates' }));
    await screen.findByText('Draft Triage');
    expect(screen.getByText('Unpublished changes')).toBeInTheDocument();
    const platformCard = screen.getByText('Support Bot').closest('.card') as HTMLElement;
    expect(within(platformCard).getByText('Release managed', { selector: '.chip' })).toBeInTheDocument();
    expect(within(platformCard).getByRole('button', { name: /Release managed · read only/i })).toBeDisabled();
  });

  it('publishes a draft using its optimistic-lock revision', async () => {
    vi.mocked(api.publishTemplate).mockResolvedValue({
      template: { ...DRAFT, status: 'published', version: '1.0.0', current_version_id: 'tplv-new', published_revision: 1 },
      version: { id: 'tplv-new', template_id: DRAFT.id, version: '1.0.0', name: DRAFT.name, type: 'App', app_type: 'workflow', desc: DRAFT.desc, first_party: false, flow: FLOW, category: 'Service', vertical: null, tags: [], requirements: REQS, published_at: '2026-01-01T00:00:00' },
      issues: [],
    });
    renderPage(); await screen.findByText('Support Bot');
    fireEvent.click(screen.getByRole('button', { name: 'Manage templates' }));
    await screen.findByText('Draft Triage');
    const card = screen.getByText('Draft Triage').closest('.card') as HTMLElement;
    fireEvent.click(within(card).getByText('Publish template'));
    await waitFor(() => expect(api.publishTemplate).toHaveBeenCalledWith('tpl-draft', 1));
  });

  it('archives editable templates instead of hard deleting them', async () => {
    vi.mocked(api.deleteTemplate).mockResolvedValue({ deleted: DRAFT.id, archived: true });
    renderPage(); await screen.findByText('Support Bot');
    fireEvent.click(screen.getByRole('button', { name: 'Manage templates' }));
    await screen.findByText('Draft Triage');
    const card = screen.getByText('Draft Triage').closest('.card') as HTMLElement;
    fireEvent.click(within(card).getByText('Archive'));
    fireEvent.click(screen.getByRole('button', { name: 'Archive template' }));
    await waitFor(() => expect(api.deleteTemplate).toHaveBeenCalledWith('tpl-draft'));
  });
});

describe('TemplateDetailsModal save and publish', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    grantPermissions();
    vi.mocked(api.listAgents).mockResolvedValue([agent()]);
  });

  it('Save draft captures an Agent as a Private draft by default', async () => {
    vi.mocked(api.saveAsTemplate).mockResolvedValue(saved());
    const onClose = vi.fn();
    render(<MemoryRouter><AppProvider><TemplateDetailsModal target={{ mode: 'save', agent: agent() }} onClose={onClose} /></AppProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(api.saveAsTemplate).toHaveBeenCalledWith(expect.objectContaining({ agent_id: 'a1', name: 'Lead Scorer', source: 'private' })));
    expect(api.publishTemplate).not.toHaveBeenCalled();
  });

  it('Save & publish saves first, then publishes exactly that returned draft revision', async () => {
    vi.mocked(api.saveAsTemplate).mockResolvedValue(saved());
    vi.mocked(api.publishTemplate).mockResolvedValue({
      template: { ...DRAFT, status: 'published', version: '1.0.0', current_version_id: 'tplv-new' },
      version: { id: 'tplv-new', template_id: DRAFT.id, version: '1.0.0', name: DRAFT.name, type: 'App', app_type: 'workflow', desc: DRAFT.desc, first_party: false, flow: FLOW, category: 'Service', vertical: null, tags: [], requirements: REQS, published_at: '2026-01-01T00:00:00' }, issues: [],
    });
    render(<MemoryRouter><AppProvider><TemplateDetailsModal target={{ mode: 'save', agent: agent() }} onClose={vi.fn()} /></AppProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: 'Save & publish' }));
    await waitFor(() => expect(api.publishTemplate).toHaveBeenCalledWith('tpl-draft', DRAFT.draft_revision));
  });

  it('a user with agent:templates:manage-platform can author an Organization-managed template', async () => {
    vi.mocked(api.saveAsTemplate).mockResolvedValue(saved({ ...DRAFT, source: 'platform', origin: 'organization', first_party: true }));
    render(<MemoryRouter><AppProvider><TemplateDetailsModal target={{ mode: 'save', agent: agent() }} onClose={vi.fn()} /></AppProvider></MemoryRouter>);
    await screen.findByRole('option', { name: /Organization-managed/ });
    fireEvent.change(screen.getByLabelText('Visibility'), { target: { value: 'platform' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(api.saveAsTemplate).toHaveBeenCalledWith(expect.objectContaining({ source: 'platform' })));
  });

  it('without agent:templates:manage-platform only Private can be chosen', async () => {
    grantPermissions(['agent:templates:view', 'agent:studio:edit']);
    render(<MemoryRouter><AppProvider><TemplateDetailsModal target={{ mode: 'save', agent: agent() }} onClose={vi.fn()} /></AppProvider></MemoryRouter>);
    await screen.findByText(/need the permission to manage platform templates/);
    expect(screen.queryByRole('option', { name: /Organization-managed/ })).toBeNull();
    expect(screen.getByRole('option', { name: /Private/ })).toBeInTheDocument();
  });

  it('editing uses If-Match revision and can explicitly recapture the flow from an Agent', async () => {
    vi.mocked(api.updateTemplate).mockResolvedValue(saved(CHANGED));
    render(<MemoryRouter><AppProvider><TemplateDetailsModal target={{ mode: 'edit', template: DRAFT }} onClose={vi.fn()} /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText(/Replace draft flow/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/Replace draft flow/), { target: { value: 'a1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(api.updateTemplate).toHaveBeenCalledWith('tpl-draft', 1, expect.objectContaining({ refresh_from_agent_id: 'a1' })));
  });
});
