import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Templates from '../pages/Templates';
import { api } from '../api';
import type { Agent, Template } from '../types';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();
afterEach(() => cleanup());

const requirements = { node_types: [], knowledge_bases: [], catalog_objects: [], tools: [], models: [], env_vars: [], review: [] };
function makeTemplate(overrides?: Partial<Template>): Template {
  return {
    id: 'tpl-analysis', key: 'analysis', name: 'Analysis Agent', type: 'App', app_type: 'workflow',
    desc: 'Analysis.', first_party: true,
    flow: { nodes: [
      { id: 't1', type: 'trigger', x: 0, y: 0, sub: '', config: {} },
      { id: 't2', type: 'respond', x: 100, y: 0, sub: '', config: {} },
    ], edges: [] },
    source: 'platform', origin: 'release', status: 'published', version: '1.0.0', category: 'Data', vertical: null,
    tags: [], requirements, owner: 'release', release_managed: true, hidden: false,
    draft_revision: 1, published_revision: 1, current_version_id: 'tplv-analysis', published_at: '2026-01-01T00:00:00',
    published_by: 'release', has_unpublished_changes: false, created_at: '2026-01-01T00:00:00', updated_at: '2026-01-01T00:00:00',
    ...overrides,
  } as Template;
}
function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a999', name: 'Instant Analysis Agent', type: 'App', subtype: 'Analysis Agent', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {}, flow: { nodes: [], edges: [] }, draft_revision: 1, ...overrides,
  } as Agent;
}
function renderPage() { return render(<MemoryRouter><AppProvider><Templates /></AppProvider></MemoryRouter>); }

describe('Templates published gallery', () => {
  beforeEach(() => {
    vi.clearAllMocks(); mockNavigate.mockReset();
    vi.mocked(api.listTemplates).mockResolvedValue([makeTemplate()]);
    vi.mocked(api.listManagedTemplates).mockResolvedValue([makeTemplate()]);
  });

  it('shows the real published node sequence', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/trigger → respond/)).toBeInTheDocument());
  });

  it('Customize instantiates the immutable template by stable id', async () => {
    vi.mocked(api.instantiateTemplate).mockResolvedValue({ agent: makeAgent({ id: 'a-custom-123' }), published: false, publish_error: null });
    renderPage(); await screen.findByText('Analysis Agent');
    fireEvent.click(screen.getByText('Customize'));
    await waitFor(() => expect(api.instantiateTemplate).toHaveBeenCalledWith('tpl-analysis', { publish: false }));
    expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a-custom-123');
  });

  it('Create & publish agent preserves the existing one-click agent publication behavior', async () => {
    vi.mocked(api.instantiateTemplate).mockResolvedValue({ agent: makeAgent({ status: 'published' }), published: true, publish_error: null });
    renderPage(); await screen.findByText('Analysis Agent');
    fireEvent.click(screen.getByText(/Create & publish agent/));
    await waitFor(() => expect(api.instantiateTemplate).toHaveBeenCalledWith('tpl-analysis', { publish: true, invoking_user: 'user:developer' }));
    expect(mockNavigate).toHaveBeenCalledWith('/agents');
  });

  it('a blocked one-click Agent publish still opens the Builder', async () => {
    vi.mocked(api.instantiateTemplate).mockResolvedValue({ agent: makeAgent({ id: 'a-blocked' }), published: false, publish_error: 'blocked' });
    renderPage(); await screen.findByText('Analysis Agent');
    fireEvent.click(screen.getByText(/Create & publish agent/));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a-blocked'));
  });

  it('duplicate and export use the published snapshot', async () => {
    vi.mocked(api.duplicateTemplate).mockResolvedValue(makeTemplate({ id: 'tpl-copy', source: 'private', origin: 'private', status: 'draft', current_version_id: null }));
    vi.mocked(api.exportTemplate).mockResolvedValue({ package_version: '1' });
    Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() });
    renderPage(); await screen.findByText('Analysis Agent');
    fireEvent.click(screen.getByText('Duplicate'));
    await waitFor(() => expect(api.duplicateTemplate).toHaveBeenCalledWith('tpl-analysis', { state: 'published' }));
  });
});
