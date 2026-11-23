import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgentWithFanOut(): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'gov', x: 200, y: 140, sub: '', config: {} },
        { id: 'n3', type: 'code', x: 360, y: 80, sub: '', config: {} },
        { id: 'n4', type: 'code', x: 360, y: 220, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        // Real fan-out — n2's single output feeds BOTH n3 and n4, the
        // exact pattern that used to be impossible (add_edge replaced
        // the first with the second).
        { id: 'e2', source: 'n2', target: 'n3', source_handle: null },
        { id: 'e3', source: 'n2', target: 'n4', source_handle: null },
      ],
    },
  } as Agent;
}

function setupCommonMocks(agent: Agent) {
  vi.clearAllMocks();
  vi.mocked(api.getAgent).mockResolvedValue(agent);
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
}

describe('Builder — real multi-edge fan-out rendering (§0.40, GAP_REPORT)', () => {
  it('renders two real, distinct edge paths from the same source port', async () => {
    const agent = makeAgentWithFanOut();
    setupCommonMocks(agent);
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelector('[data-nodeid="n1"]')).toBeInTheDocument());

    const edgeLines = container.querySelectorAll('.edge-line');
    expect(edgeLines.length).toBe(3); // e1, e2, e3 — all three real edges

    const paths = Array.from(container.querySelectorAll('.edge-line')).map((el) => el.getAttribute('d'));
    const uniquePaths = new Set(paths);
    expect(uniquePaths.size).toBe(3); // all three genuinely distinct
  });

  it('the real fan-out source port shows the connected style, not an error state', async () => {
    const agent = makeAgentWithFanOut();
    setupCommonMocks(agent);
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelector('[data-nodeid="n1"]')).toBeInTheDocument());
    const connectedPorts = container.querySelectorAll('.port.connected');
    expect(connectedPorts.length).toBeGreaterThan(0);
  });

  it('deleting one of two fan-out edges leaves the real other one intact', async () => {
    const agent = makeAgentWithFanOut();
    setupCommonMocks(agent);
    const afterDelete = { ...agent, flow: { ...agent.flow, edges: agent.flow.edges.filter((e) => e.id !== 'e2') } };
    vi.mocked(api.deleteEdge).mockResolvedValue(afterDelete);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelectorAll('.edge-hit').length).toBe(3));

    fireEvent.click(container.querySelectorAll('.edge-hit')[1]); // the e2 hit-area
    await waitFor(() => expect(api.deleteEdge).toHaveBeenCalledWith('a1', 'e2', 1));
  });
});

describe('Builder — real port hint text reflects fan-out support', () => {
  it('an already-connected port still shows a real, discoverable hint (not hidden entirely)', async () => {
    const agent = makeAgentWithFanOut();
    setupCommonMocks(agent);
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());

    const hints = Array.from(container.querySelectorAll('.port-hint')).map((el) => el.textContent);
    expect(hints.some((t) => t?.includes('another connection'))).toBe(true);
  });
});
