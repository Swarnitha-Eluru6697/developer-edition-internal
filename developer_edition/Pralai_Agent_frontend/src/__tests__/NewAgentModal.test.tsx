import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import NewAgentModal from '../modals/NewAgentModal';
import { api } from '../api';
import type { Agent, Template } from '../types';
import { openSelect } from './pickOption';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();

afterEach(() => cleanup());

function makeTemplate(overrides?: Partial<Template>): Template {
  return { id: 'tpl-blank', key: 'blank', name: 'Blank canvas', type: 'App', desc: '', first_party: true, flow: { nodes: [], edges: [] }, source: 'platform', origin: 'release', status: 'published', version: '1.0.0', tags: [], requirements: { node_types: [], knowledge_bases: [], catalog_objects: [], tools: [], models: [], env_vars: [], review: [] }, release_managed: true, hidden: false, draft_revision: 1, published_revision: 1, current_version_id: 'tplv-blank', has_unpublished_changes: false, created_at: '2026-01-01T00:00:00', updated_at: '2026-01-01T00:00:00', ...overrides } as Template;
}

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a99', name: 'New Flow', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderModal() {
  return render(
    <MemoryRouter>
      <AppProvider><NewAgentModal /></AppProvider>
    </MemoryRouter>
  );
}

describe('NewAgentModal — Data Agent option removed, App type is the only real decision', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listTemplates).mockResolvedValue([
      makeTemplate({ name: 'Blank canvas', type: 'App' }),
      makeTemplate({ id: 'tpl-conversational', key: 'conversational', name: 'Conversational', type: 'App' }),
      makeTemplate({ id: 'tpl-analysis', key: 'analysis', name: 'Analysis Agent', type: 'Data' }),
    ]);
  });

  it('there is no Data Agent vs App Agent choice at all — App type is shown immediately', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText(/App type/)).toBeInTheDocument());
    expect(screen.queryByText('Data Agent')).not.toBeInTheDocument();
    expect(screen.queryByText('App Agent')).not.toBeInTheDocument();
    expect(screen.getByText('Workflow')).toBeInTheDocument();
    expect(screen.getByText('Chatbot')).toBeInTheDocument();
  });

  it('the modal is honest that Data Agents come from elsewhere, not built here', async () => {
    renderModal();
    await waitFor(() => {
      expect(screen.getByText(/provisioned by a separate service/)).toBeInTheDocument();
    });
  });

  it('"Start from" only ever offers real App templates, never a Data template', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText('Start from')).toBeInTheDocument());
    openSelect(screen.getByRole('button', { name: 'Start from' }));
    expect(await screen.findByRole('option', { name: /Blank canvas/i })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Conversational/i })).toBeInTheDocument();
    expect(screen.queryByText('Analysis Agent')).not.toBeInTheDocument();
  });

  it('creating an agent never sends a type field at all — the backend is trusted to always create App', async () => {
    vi.mocked(api.createAgent).mockResolvedValue(makeAgent());
    renderModal();
    await waitFor(() => expect(screen.getByText('Agent')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Agent'));
    fireEvent.change(screen.getByPlaceholderText('e.g. Renewal Risk Analysis Agent'), { target: { value: 'My New Agent' } });
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => {
      const call = vi.mocked(api.createAgent).mock.calls[0][0];
      expect(call).not.toHaveProperty('type');
      expect(call.app_type).toBe('agent');
      expect(call.name).toBe('My New Agent');
    });
  });

  it('creating without choosing a template sends no template_id, so the canvas opens empty', async () => {
    // The regression. `templates.find(...) || templates[0]` meant an
    // untouched "Start from" silently applied whichever template was
    // first, so a new agent arrived with that template's whole flow —
    // retrieval node, knowledge base and all — instead of just its
    // trigger. The select had no empty option either, so it displayed a
    // template that had never been chosen.
    vi.mocked(api.createAgent).mockResolvedValue(makeAgent());
    renderModal();
    await waitFor(() => expect(screen.getByText('Start from')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => {
      const call = vi.mocked(api.createAgent).mock.calls[0][0];
      expect(call.template_id).toBeUndefined();
      expect(call.subtype).toBe('Custom');
    });
  });

  it('choosing a template really does send it', async () => {
    vi.mocked(api.createAgent).mockResolvedValue(makeAgent());
    renderModal();
    await waitFor(() => expect(screen.getByText('Start from')).toBeInTheDocument());
    const startFrom = screen.getByRole('button', { name: 'Start from' });
    expect(startFrom).toHaveTextContent(/Empty canvas/);
    openSelect(startFrom);
    fireEvent.click(await screen.findByRole('option', { name: /Conversational/ }));
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => {
      const call = vi.mocked(api.createAgent).mock.calls[0][0];
      expect(call.template_id).toBe('tpl-conversational');
      expect(call.subtype).toBe('Conversational');
    });
  });

  it('selecting a specific app type and creating navigates into the real playground for the new agent', async () => {
    vi.mocked(api.createAgent).mockResolvedValue(makeAgent({ id: 'a123', app_type: 'workflow' }));
    renderModal();
    await waitFor(() => expect(screen.getByText('Workflow')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Workflow'));
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a123');
    });
  });
});
