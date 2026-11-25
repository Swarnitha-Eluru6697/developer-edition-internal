import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Agents from '../pages/Agents';
import ModalHost from '../components/ModalHost';
import ToastHost from '../components/ToastHost';
import EditAgentModal from '../modals/EditAgentModal';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Revenue Analysis Agent', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0,
    cost_7d: '$0.00', first_party: false, tags: [], description: '',
    updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

describe('the Edit action on a Studio card', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([makeAgent()]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
  });

  const renderPage = () => render(
    <MemoryRouter><AppProvider><Agents /><ModalHost /></AppProvider></MemoryRouter>
  );

  it('every card offers an Edit action', async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    expect(container.querySelectorAll('.studio-card-edit')).toHaveLength(1);
  });

  it('is offered on published agents too, not only drafts', async () => {
    // Delete is draft-only because it is destructive. Editing a name is
    // not, and a published agent is the one most likely to have a name
    // worth correcting.
    vi.mocked(api.listAgents).mockResolvedValue([
      makeAgent({ id: 'a1', status: 'draft' }),
      makeAgent({ id: 'a2', name: 'Support Bot', status: 'published' }),
    ]);
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText('Support Bot')).toBeInTheDocument());
    expect(container.querySelectorAll('.studio-card-edit')).toHaveLength(2);
    expect(container.querySelectorAll('.studio-card-delete')).toHaveLength(1);
  });

  it('clicking Edit opens the modal and does not open the Builder', async () => {
    // The card's own onClick navigates to the Builder, so the action
    // button has to stop propagation or Edit would do both.
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());

    fireEvent.click(container.querySelector('.studio-card-edit')!);

    await waitFor(() => expect(screen.getByText('Edit agent')).toBeInTheDocument());
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('clicking the card itself still opens the Builder', async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    fireEvent.click(container.querySelector('.studio-card')!);
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
  });
});

describe('EditAgentModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({
      name: 'Old name', description: 'Old description',
    }));
    vi.mocked(api.setAgentMeta).mockResolvedValue(makeAgent({ name: 'New name' }));
  });

  // ToastHost included on purpose: the blank-name guard's whole job is to
  // TELL the person, so a test tree without it could only check that
  // nothing was sent, not that they were informed.
  const renderModal = () => render(
    <MemoryRouter><AppProvider><EditAgentModal agentId="a1" /><ToastHost /></AppProvider></MemoryRouter>
  );

  it('loads the current name and description rather than starting blank', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByDisplayValue('Old name')).toBeInTheDocument());
    expect(screen.getByDisplayValue('Old description')).toBeInTheDocument();
  });

  it('saves the edited name through the meta endpoint', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByDisplayValue('Old name')).toBeInTheDocument());
    fireEvent.change(screen.getByDisplayValue('Old name'), { target: { value: 'New name' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(api.setAgentMeta).toHaveBeenCalledWith('a1', {
      name: 'New name', description: 'Old description',
    }));
  });

  it('Save is disabled until something actually changes', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByDisplayValue('Old name')).toBeInTheDocument());
    expect(screen.getByText('Save')).toBeDisabled();

    fireEvent.change(screen.getByDisplayValue('Old name'), { target: { value: 'Old name edited' } });
    expect(screen.getByText('Save')).not.toBeDisabled();
  });

  it('refuses to save a blank name instead of sending one', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByDisplayValue('Old name')).toBeInTheDocument());
    fireEvent.change(screen.getByDisplayValue('Old name'), { target: { value: '   ' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(screen.getByText(/needs a name/)).toBeInTheDocument());
    expect(api.setAgentMeta).not.toHaveBeenCalled();
  });

  it('trims whitespace off the name it sends', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByDisplayValue('Old name')).toBeInTheDocument());
    fireEvent.change(screen.getByDisplayValue('Old name'), { target: { value: '  Padded name  ' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(api.setAgentMeta).toHaveBeenCalledWith('a1',
      expect.objectContaining({ name: 'Padded name' })));
  });
});
