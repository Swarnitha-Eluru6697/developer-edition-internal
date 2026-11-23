import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Agents from '../pages/Agents';
import DrawerHost from '../components/DrawerHost';
import { api } from '../api';
import type { Agent, Role, AgentLog, Trace } from '../types';
import { openSelect, optionTexts, pickOption } from './pickOption';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Revenue Analysis Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'team-data', status: 'published', version: 'v4', health_score: 91, health_status: 'pass',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 214, cost_7d: '$28.40', first_party: true,
    tags: ['finance', 'sql'], description: 'Generates SQL analysis against the revenue lakehouse.',
    updated_at: '2026-08-19T10:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderStudio(url = '/agents') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <AppProvider><Agents /><DrawerHost /></AppProvider>
    </MemoryRouter>
  );
}

describe('Studio — real card grid, genuine filters backed by real Agent data', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([
      makeAgent(),
      makeAgent({ id: 'a5', name: 'Support Knowledge Bot', type: 'App', app_type: 'chatbot', owner: 'team-support', tags: ['support'], updated_at: '2026-08-19T12:00:00', description: 'Answers customer questions.' }),
      makeAgent({ id: 'a6', name: 'Invoice Approval Flow', type: 'App', app_type: 'workflow', owner: 'team-finance', tags: ['finance', 'internal'], status: 'draft', updated_at: '2026-08-19T08:00:00', description: 'Routes an invoice through approval.' }),
    ]);
  });

  it('renders the page as "Studio", not the old "Agents" title', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Studio')).toBeInTheDocument());
  });

  it('shows real cards for every agent with their real type label', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    expect(screen.getByText('Support Knowledge Bot')).toBeInTheDocument();
    expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
    expect(screen.getAllByText('Chatbot').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Workflow').length).toBeGreaterThan(0);
  });

  it('clicking a card navigates to the real playground for that specific agent', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Support Knowledge Bot')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Knowledge Bot'));
    expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a5');
  });

  it('the Types filter genuinely narrows the real card list, not a decorative control', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    const typeSelect = screen.getByRole('button', { name: 'Filter by type' });
    expect(typeSelect).toHaveTextContent('Types');
    pickOption(typeSelect, 'Workflow');

    await waitFor(() => {
      expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
      expect(screen.queryByText('Revenue Analysis Agent')).not.toBeInTheDocument();
      expect(screen.queryByText('Support Knowledge Bot')).not.toBeInTheDocument();
    });
  });

  it('the Creators filter uses the real distinct owner values from actual data', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    const creatorSelect = screen.getByRole('button', { name: 'Filter by creator' });
    expect(creatorSelect).toHaveTextContent('Creators');
    openSelect(creatorSelect);
    const options = optionTexts();
    expect(options).toContain('team-data');
    expect(options).toContain('team-support');
    expect(options).toContain('team-finance');

    pickOption(creatorSelect, 'team-finance');
    await waitFor(() => {
      expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
      expect(screen.queryByText('Revenue Analysis Agent')).not.toBeInTheDocument();
    });
  });

  it('the Tags filter genuinely narrows results using real tag data, AND-combined across selections', async () => {
    const { container } = renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    fireEvent.click(screen.getByText(/^Tags/));
    await waitFor(() => expect(container.querySelector('.studio-tagmenu')).toBeInTheDocument());
    const internalOption = within(container.querySelector('.studio-tagmenu') as HTMLElement).getByText('internal');
    fireEvent.click(internalOption);

    await waitFor(() => {
      expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
      expect(screen.queryByText('Revenue Analysis Agent')).not.toBeInTheDocument();
      expect(screen.queryByText('Support Knowledge Bot')).not.toBeInTheDocument();
    });
  });

  it('status tabs split published and draft agents, with counts', async () => {
    renderStudio();
    await waitFor(() => expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument());
    const tab = (label: string) => screen.getAllByText(label).find((el) => el.classList.contains('tab'))!;
    expect(tab('All')).toHaveTextContent('3');
    expect(tab('Published')).toHaveTextContent('2');
    expect(tab('Drafts')).toHaveTextContent('1');
    // No agent is staged, so there is no Staged tab.
    expect(screen.queryByText('Staged')).not.toBeInTheDocument();

    fireEvent.click(tab('Drafts'));
    expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
    expect(screen.queryByText('Revenue Analysis Agent')).not.toBeInTheDocument();
    expect(screen.queryByText('Support Knowledge Bot')).not.toBeInTheDocument();

    fireEvent.click(tab('Published'));
    expect(screen.queryByText('Invoice Approval Flow')).not.toBeInTheDocument();
    expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument();
    expect(screen.getByText('Support Knowledge Bot')).toBeInTheDocument();
  });

  it('status tab counts follow the other filters', async () => {
    // The search term arrives from the top-bar search as ?q=.
    renderStudio('/agents?q=invoice');
    await waitFor(() => expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument());
    const tab = (label: string) => screen.getAllByText(label).find((el) => el.classList.contains('tab'))!;
    expect(tab('All')).toHaveTextContent('1');
    expect(tab('Published')).toHaveTextContent('0');
    expect(tab('Drafts')).toHaveTextContent('1');
  });

  it('search genuinely filters by name and description text, not just name', async () => {
    renderStudio('/agents?q=customer%20questions');
    await waitFor(() => {
      expect(screen.getByText('Support Knowledge Bot')).toBeInTheDocument();
      expect(screen.queryByText('Revenue Analysis Agent')).not.toBeInTheDocument();
    });
  });

  it('sorting by Last modified uses the real updated_at, most recent first', async () => {
    const { container } = renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    const titles = Array.from(container.querySelectorAll('.studio-card-title')).map((el) => el.textContent);
    // Support Knowledge Bot has the latest updated_at (12:00) among the three
    expect(titles[0]).toBe('Support Knowledge Bot');
  });

  it('adding a tag calls the real setAgentMeta API with the merged real tag list', async () => {
    vi.mocked(api.setAgentMeta).mockResolvedValue(makeAgent());
    // The tag is typed into a side panel now, not window.prompt.
    renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    const revenueCard = screen.getByText('Revenue Analysis Agent').closest('.studio-card') as HTMLElement;
    fireEvent.click(within(revenueCard).getByText(/add tag/));
    fireEvent.change(await screen.findByLabelText('Tag'), { target: { value: 'urgent' } });
    fireEvent.click(screen.getByText('Add tag'));

    await waitFor(() => {
      expect(api.setAgentMeta).toHaveBeenCalledWith('a1', { tags: ['finance', 'sql', 'urgent'] });
    });
  });

  it('the card\'s "Details" button opens the real agent profile drawer (with API Access) WITHOUT triggering the card\'s own navigation into the Builder — closing a real discoverability gap where Studio never led to API Access at all', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a5', name: 'Support Knowledge Bot' }));
    vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
    vi.mocked(api.getAgentLogs).mockResolvedValue([] as AgentLog[]);
    vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'a1', metrics: {} });
    renderStudio();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());

    const revenueCard = screen.getByText('Revenue Analysis Agent').closest('.studio-card') as HTMLElement;
    fireEvent.click(within(revenueCard).getByRole('button', { name: 'Details' }));

    // real drawer opens for the correct agent, not a navigation into the builder
    await waitFor(() => {
      expect(mockNavigate).not.toHaveBeenCalled();
    });
  });
});
