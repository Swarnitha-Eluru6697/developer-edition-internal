import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider, useApp } from '../context/AppContext';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';
import { api, BASE } from '../api';
import type { Agent, Role, AgentLog, Trace } from '../types';

vi.mock('../api');

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

function Harness({ agentId }: { agentId: string }) {
  return <AgentProfileDrawer agentId={agentId} />;
}

function renderDrawer(agentId = 'a1') {
  return render(
    <MemoryRouter>
      <AppProvider><Harness agentId={agentId} /></AppProvider>
    </MemoryRouter>
  );
}

describe('AgentProfileDrawer — real API Access tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
    vi.mocked(api.getAgentLogs).mockResolvedValue([] as AgentLog[]);
    vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'a1', metrics: {} });
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  it('shows a real, agent-specific REST endpoint using the actual API base URL', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a3' }));
    renderDrawer('a3');
    await waitFor(() => expect(screen.getByText('Agent · Revenue Analysis Agent')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      expect(screen.getByDisplayValue(`${BASE}/api/agents/a3/run`)).toBeInTheDocument();
    });
  });

  it('shows a real, agent-specific dedicated UI URL', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a5', name: 'Support Knowledge Bot' }));
    renderDrawer('a5');
    await waitFor(() => expect(screen.getByText('Agent · Support Knowledge Bot')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      const urlInput = screen.getAllByDisplayValue(/#\/app\/a5/)[0] as HTMLInputElement;
      expect(urlInput.value).toContain('/app/a5');
    });
  });

  it('the curl example uses the real agent-specific endpoint, not a generic placeholder', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a6' }));
    renderDrawer('a6');
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      expect(screen.getByText(/curl -X POST/)).toBeInTheDocument();
    });
    expect(screen.getByText(new RegExp(`agents/a6/run`))).toBeInTheDocument();
  });

  it('copy buttons genuinely call the real clipboard API with the exact real value shown', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a1' }));
    renderDrawer('a1');
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));
    await waitFor(() => expect(screen.getByDisplayValue(`${BASE}/api/agents/a1/run`)).toBeInTheDocument());

    const endpointInput = screen.getByDisplayValue(`${BASE}/api/agents/a1/run`);
    const endpointCopyBtn = endpointInput.parentElement!.querySelector('button') as HTMLElement;
    fireEvent.click(endpointCopyBtn);

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(`${BASE}/api/agents/a1/run`);
    });
  });

  it('honestly discloses there is no auth layer yet, not a fabricated API key flow', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a1' }));
    renderDrawer('a1');
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      expect(screen.getByText(/no auth layer yet/)).toBeInTheDocument();
    });
  });

  it('an unpublished App agent shows an honest note on the UI URL, not a silent broken link', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a7', type: 'App', status: 'draft', app_type: 'chatflow' }));
    renderDrawer('a7');
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      expect(screen.getByText(/isn't published yet/)).toBeInTheDocument();
    });
  });

  it('the REST endpoint is presented as real and callable regardless of publish status — no false gating', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ id: 'a7', type: 'App', status: 'draft', app_type: 'chatflow' }));
    renderDrawer('a7');
    await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
    fireEvent.click(screen.getByText('Api Access'));

    await waitFor(() => {
      expect(screen.getByText(/regardless of publish status/)).toBeInTheDocument();
    });
  });
});
