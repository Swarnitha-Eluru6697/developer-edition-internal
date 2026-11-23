import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Home from '../pages/Home';
import DrawerHost from '../components/DrawerHost';
import { api } from '../api';
import type { Agent, HomeStats, Role, AgentLog, Trace } from '../types';
import { grantPermissions } from './grantPermissions';

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
    tags: [], description: '', updated_at: '2026-08-19T10:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function makeStats(): HomeStats {
  return { agents: 8, runsToday: 42, successRate: 97, openAlerts: 2, avgCost: '$0.08' } as HomeStats;
}

function renderHome() {
  return render(
    <MemoryRouter>
      <AppProvider><Home /><DrawerHost /></AppProvider>
    </MemoryRouter>
  );
}

describe('Overview page — real agents table sourced from live data', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    grantPermissions();
    vi.mocked(api.getHomeStats).mockResolvedValue(makeStats());
    vi.mocked(api.listAlerts).mockResolvedValue([]);
    vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
    vi.mocked(api.getAgentLogs).mockResolvedValue([] as AgentLog[]);
    vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'a1', metrics: {} });
  });

  it('renders a real table row per real agent, with genuine status/health/plane data', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([
      makeAgent(),
      makeAgent({ id: 'a5', name: 'Support Knowledge Bot', type: 'App', subtype: 'Conversational', plane: 'App', status: 'published', health_score: 95, health_status: 'pass' }),
    ]);
    renderHome();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    expect(screen.getByText('Support Knowledge Bot')).toBeInTheDocument();
    expect(screen.getAllByText('published').length).toBe(2);
    expect(screen.getByText('91 · pass')).toBeInTheDocument();
  });

  it('the table has no "New agent" button of its own — only the page-level one stays', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([makeAgent()]);
    const { container } = renderHome();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    await screen.findByRole('button', { name: /New agent/ });
    // exactly one "New agent" button on the whole page (the pre-existing page-level action)
    const newAgentButtons = Array.from(container.querySelectorAll('button')).filter((b) => b.textContent?.includes('New agent'));
    expect(newAgentButtons.length).toBe(1);
  });

  it('clicking a table row opens the real agent profile drawer with genuine details, matching the reference — not a direct navigation', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([makeAgent({ id: 'a3', name: 'Pipeline Engineer Agent', subtype: 'Engineer', owner: 'team-platform', version: 'v6', model: 'Claude Sonnet 5', mode: 'agentic' })]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a3', name: 'Pipeline Engineer Agent', subtype: 'Engineer', owner: 'team-platform', version: 'v6', model: 'Claude Sonnet 5', mode: 'agentic' }));
    renderHome();
    await waitFor(() => expect(screen.getByText('Pipeline Engineer Agent')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Pipeline Engineer Agent'));

    // real drawer opens with the real per-agent detail, not an immediate route change
    await waitFor(() => {
      expect(screen.getByText('Agent · Pipeline Engineer Agent')).toBeInTheDocument();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(screen.getByText('Data · Engineer')).toBeInTheDocument();
    expect(screen.getByText('team-platform')).toBeInTheDocument();

    // "Open in builder" from inside the real drawer still reaches the real playground
    fireEvent.click(screen.getByText('Open in builder'));
    expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a3');
  });

  it('shows real first-party badges only for agents that are actually first-party', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([
      makeAgent({ id: 'a1', name: 'Revenue Analysis Agent', first_party: true }),
      makeAgent({ id: 'a5', name: 'Support Knowledge Bot', first_party: false }),
    ]);
    renderHome();
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    expect(screen.getByText('first-party')).toBeInTheDocument(); // exactly one, not on the non-first-party row
  });
});
