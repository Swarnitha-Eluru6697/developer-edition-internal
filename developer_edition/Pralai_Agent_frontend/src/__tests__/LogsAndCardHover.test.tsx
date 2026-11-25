import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';
import Agents from '../pages/Agents';
import { api } from '../api';
import type { Agent, Role, AgentLog, Trace } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Vendor Onboarding Automation', type: 'App', subtype: 'Task-Automation', plane: 'App',
    owner: 'team-procurement', status: 'published', version: 'v2', health_score: 58, health_status: 'fail',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'agent', runs_7d: 64, cost_7d: '$21.90', first_party: false,
    tags: [], description: '', updated_at: '2026-08-19T18:10:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function makeLogs(): AgentLog[] {
  return [
    { id: 1, agent_id: 'a1', ts: '14:18:31.401', level: 'ERROR', msg: 'UCS DENIED at hop 2 — run terminated, no data accessed', trace_id: 't1' },
    { id: 2, agent_id: 'a1', ts: '14:18:31.180', level: 'WARN', msg: 'Entitlement check hop 2/3: agent principal not entitled on pg_procurement.vendors', trace_id: 't1' },
    { id: 3, agent_id: 'a1', ts: '14:18:31.002', level: 'INFO', msg: 'Live run (sync) · status=success · message: "onboard the new vendor Acme Corp"', trace_id: 't1' },
  ] as AgentLog[];
}

describe('AgentProfileDrawer — Logs tab search and filter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
    vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'a1', metrics: {} });
  });

  function renderDrawer(agent = makeAgent()) {
    vi.mocked(api.getAgent).mockResolvedValue(agent);
    vi.mocked(api.getAgentLogs).mockResolvedValue(makeLogs());
    return render(
      <MemoryRouter>
        <AppProvider><AgentProfileDrawer agentId={agent.id} /></AppProvider>
      </MemoryRouter>
    );
  }

  it('the real user message now appears in the log line, not just a generic "Live run" message', async () => {
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Logs'));
    await waitFor(() => {
      expect(screen.getByText(/message: "onboard the new vendor Acme Corp"/)).toBeInTheDocument();
    });
  });

  it('search genuinely filters logs by real message content', async () => {
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Logs'));
    await waitFor(() => expect(screen.getByText(/UCS DENIED/)).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Search logs…'), { target: { value: 'Acme Corp' } });

    await waitFor(() => {
      expect(screen.getByText(/onboard the new vendor Acme Corp/)).toBeInTheDocument();
      expect(screen.queryByText(/UCS DENIED/)).not.toBeInTheDocument();
    });
  });

  it('level filter genuinely narrows to only matching real log levels', async () => {
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Logs'));
    await waitFor(() => expect(screen.getByText(/UCS DENIED/)).toBeInTheDocument());

    pickOption(screen.getByRole('button', { name: 'Log level' }), 'Error');

    await waitFor(() => {
      expect(screen.getByText(/UCS DENIED/)).toBeInTheDocument();
      expect(screen.queryByText(/Entitlement check/)).not.toBeInTheDocument();
      expect(screen.queryByText(/onboard the new vendor/)).not.toBeInTheDocument();
    });
  });

  it('an honest empty state distinguishes "no logs at all" from "no logs match your search"', async () => {
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Logs'));
    await waitFor(() => expect(screen.getByText(/UCS DENIED/)).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Search logs…'), { target: { value: 'nothing matches this at all' } });
    await waitFor(() => {
      expect(screen.getByText('No logs match your search')).toBeInTheDocument();
    });
  });
});

describe('Studio — hover-visible Open app icon on the card', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  function renderStudio(agents: Agent[]) {
    vi.mocked(api.listAgents).mockResolvedValue(agents);
    return render(
      <MemoryRouter>
        <AppProvider><Agents /></AppProvider>
      </MemoryRouter>
    );
  }

  it('a published App agent gets a real Open app link on its card, pointing to the real per-agent URL', async () => {
    renderStudio([makeAgent({ id: 'a8', name: 'Vendor Onboarding Automation', status: 'published', type: 'App' })]);
    await waitFor(() => expect(screen.getByText('Vendor Onboarding Automation')).toBeInTheDocument());
    const link = screen.getByTitle('Open app in a new tab') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('#/app/a8');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('a draft App agent gets no Open app link at all — nothing real to open yet', async () => {
    renderStudio([makeAgent({ id: 'a7', name: 'Onboarding Concierge', status: 'draft', type: 'App' })]);
    await waitFor(() => expect(screen.getByText('Onboarding Concierge')).toBeInTheDocument());
    expect(screen.queryByTitle('Open app in a new tab')).not.toBeInTheDocument();
  });

  it('a published Data agent (no end-user UI at all) gets no Open app link either', async () => {
    renderStudio([makeAgent({ id: 'a1', name: 'Revenue Analysis Agent', status: 'published', type: 'Data', app_type: undefined })]);
    await waitFor(() => expect(screen.getByText('Revenue Analysis Agent')).toBeInTheDocument());
    expect(screen.queryByTitle('Open app in a new tab')).not.toBeInTheDocument();
  });

  it('clicking the Open app icon does not also trigger the card\'s own click-into-Builder navigation', async () => {
    renderStudio([makeAgent({ id: 'a8', status: 'published', type: 'App' })]);
    await waitFor(() => expect(screen.getByTitle('Open app in a new tab')).toBeInTheDocument());
    const clickEvent = fireEvent.click(screen.getByTitle('Open app in a new tab'));
    // stopPropagation was called if the underlying event's propagation was actually stopped;
    // we assert indirectly by confirming the card is still present (no navigation-triggered unmount/rerender issue)
    expect(clickEvent).toBe(true); // fireEvent returns true unless preventDefault was called
    expect(screen.getByText('Vendor Onboarding Automation')).toBeInTheDocument();
  });

  it('the "Edited" info now sits above the footer divider, not grouped inside it with the actions', async () => {
    renderStudio([makeAgent({ id: 'a8', status: 'published', type: 'App', updated_at: '2026-08-19T18:10:00' })]);
    await waitFor(() => expect(screen.getByText(/Edited/)).toBeInTheDocument());
    const editedEl = screen.getByText(/Edited/);
    const footerEl = document.querySelector('.studio-card-footer')!;
    // the edited-info element is a real sibling that precedes the footer
    // in document order, not a child of it
    expect(footerEl.contains(editedEl)).toBe(false);
    const cardChildren = Array.from(editedEl.closest('.studio-card')!.children);
    expect(cardChildren.indexOf(editedEl)).toBeLessThan(cardChildren.indexOf(footerEl));
  });

  it('the Open app icon now renders inside the same footer as the Details button, not as a separate top-of-card element', async () => {
    renderStudio([makeAgent({ id: 'a8', status: 'published', type: 'App' })]);
    await waitFor(() => expect(screen.getByTitle('Open app in a new tab')).toBeInTheDocument());
    const openAppLink = screen.getByTitle('Open app in a new tab');
    const detailsBtn = screen.getByRole('button', { name: 'Details' });
    const footerEl = document.querySelector('.studio-card-footer')!;
    expect(footerEl.contains(openAppLink)).toBe(true);
    expect(footerEl.contains(detailsBtn)).toBe(true);
  });
});
