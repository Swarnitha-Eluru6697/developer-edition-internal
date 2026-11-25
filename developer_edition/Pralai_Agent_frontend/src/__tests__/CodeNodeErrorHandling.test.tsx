import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'code', x: 200, y: 140, sub: '', config: { code: 'def main(arg1, arg2):\n    return arg1' } },
        { id: 'n3', type: 'respond', x: 400, y: 140, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        { id: 'e2', source: 'n2', target: 'n3', source_handle: null },
      ],
    },
    ...overrides,
  } as Agent;
}

function renderBuilder(agent = makeAgent()) {
  vi.mocked(api.getAgent).mockResolvedValue(agent);
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
  // A real PATCH .../nodes/{id}/config genuinely returns the agent with
  // that node's config merged — mirror that here rather than always
  // resolving with the stale original, or the canvas (which reads node
  // config from the agent prop, not NodeConfigPanel's own local state)
  // would never see a config change like error_handling: 'Fail branch'.
  vi.mocked(api.updateNodeConfig).mockImplementation(async (_agentId, nodeId, patch) => ({
    ...agent,
    flow: { ...agent.flow, nodes: agent.flow.nodes.map((n) => (n.id === nodeId ? { ...n, config: { ...n.config, ...patch } } : n)) },
  }));
  return render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
}

describe('Code node — real error-handling config UI', () => {
  beforeEach(() => vi.clearAllMocks());

  async function selectCodeNode() {
    renderBuilder();
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);
    await waitFor(() => expect(screen.getByText('Code Node')).toBeInTheDocument());
  }

  it('a real Default value field appears only once "Default value" error handling is selected', async () => {
    await selectCodeNode();
    expect(screen.queryByText(/Default value \(used as this node's output/)).not.toBeInTheDocument();

    pickOption(screen.getByRole('button', { name: 'Error handling' }), 'Default value');
    await waitFor(() => expect(screen.getByText(/Default value \(used as this node's output/)).toBeInTheDocument());
  });

  it('selecting "Fail branch" shows a real note about the second output port, not a silent no-op', async () => {
    await selectCodeNode();
    pickOption(screen.getByRole('button', { name: 'Error handling' }), 'Fail branch');
    await waitFor(() => expect(screen.getByText(/A real second output port appears/)).toBeInTheDocument());
  });

  it('the real second output port (success/error) only renders on the canvas once Fail branch is actually configured, not by default', async () => {
    renderBuilder();
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
    // Default config (no error_handling set) — real single port, matching every other non-branching node.
    let ports = document.querySelectorAll('[data-nodeid="n2"].port');
    expect(ports.length).toBe(1);

    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);
    await waitFor(() => expect(screen.getByText('Code Node')).toBeInTheDocument());
    pickOption(screen.getByRole('button', { name: 'Error handling' }), 'Fail branch');

    await waitFor(() => {
      ports = document.querySelectorAll('[data-nodeid="n2"].port');
      expect(ports.length).toBe(2);
    });
    expect(document.querySelector('[data-nodeid="n2"].port.true')).toBeInTheDocument();
    expect(document.querySelector('[data-nodeid="n2"].port.false')).toBeInTheDocument();
  });

  it('a real Retry on failure explanation appears only once the toggle is real-enabled', async () => {
    await selectCodeNode();
    expect(screen.queryByText(/genuinely re-runs the code once more/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Retry on failure').parentElement!.querySelector('.tog')!);
    await waitFor(() => expect(screen.getByText(/genuinely re-runs the code once more/)).toBeInTheDocument());
  });
});
