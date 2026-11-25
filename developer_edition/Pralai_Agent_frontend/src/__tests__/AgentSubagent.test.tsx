import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import NodeConfigPanel from '../components/NodeConfigPanel';
import { AppProvider } from '../context/AppContext';
import { api } from '../api';
import { openSelect, pickOption } from './pickOption';
import type { Agent, FlowNode } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Orchestrator', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderPanel(agent: Agent, node: FlowNode, onAgentUpdate = vi.fn(), onDelete = vi.fn()) {
  return render(
    <AppProvider>
      <NodeConfigPanel agent={agent} node={node} onAgentUpdate={onAgentUpdate} onDelete={onDelete} />
    </AppProvider>
  );
}

// The strategy lives under the Agent node's collapsed "Advanced settings".
const openAdvanced = async () => fireEvent.click(await screen.findByRole('button', { name: /Advanced settings/ }));

describe('Agent node — real strategy selector with backend-matching values', () => {
  const agentNode: FlowNode = { id: 'n1', type: 'agent', x: 0, y: 0, sub: '', config: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
  });

  it('the three strategy options use real backend-matching values, not the display labels', async () => {
    renderPanel(makeAgent(), { ...agentNode, config: { strategy: 'function_calling' } });
    await openAdvanced();
    const trigger = await screen.findByRole('button', { name: 'Agentic strategy' });
    // The dropdown no longer exposes option values in the DOM, so each
    // label is checked against the value it actually persists.
    pickOption(trigger, 'ReAct');
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenLastCalledWith('a1', 'n1', { strategy: 'react' }, 1));
    pickOption(trigger, 'Function calling');
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenLastCalledWith('a1', 'n1', { strategy: 'function_calling' }, 1));
    pickOption(trigger, /Plan & execute/);
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenLastCalledWith('a1', 'n1', { strategy: 'plan_execute' }, 1));
  });

  it('selecting ReAct persists the real value "react" via the config PATCH and reveals Max iterations', async () => {
    renderPanel(makeAgent(), { ...agentNode, config: { strategy: 'function_calling' } });
    await openAdvanced();
    const trigger = await screen.findByRole('button', { name: 'Agentic strategy' });
    pickOption(trigger, 'ReAct');

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'n1', { strategy: 'react' }, 1);
    });
    await waitFor(() => {
      expect(screen.getByText('Max iterations')).toBeInTheDocument();
    });
  });

  it('selecting Plan & execute does NOT show Max iterations (a fixed, real two-call shape)', async () => {
    renderPanel(makeAgent(), { ...agentNode, config: { strategy: 'plan_execute' } });
    await openAdvanced();
    await waitFor(() => expect(screen.getByText(/exactly two real calls/i)).toBeInTheDocument());
    expect(screen.queryByText('Max iterations')).not.toBeInTheDocument();
  });
});

describe('Sub-Agent node — real target picker sourced from the actual agent list', () => {
  const subagentNode: FlowNode = { id: 'n2', type: 'subagent', x: 0, y: 0, sub: '', config: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([
      makeAgent({ id: 'a1', name: 'Orchestrator' }),
      makeAgent({ id: 'a2', name: 'Worker Agent' }),
      makeAgent({ id: 'a3', name: 'Another Agent' }),
    ]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
  });

  it('lists real agents from the actual API, excluding the current agent itself (no self-selection)', async () => {
    renderPanel(makeAgent({ id: 'a1' }), subagentNode);
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    openSelect(screen.getByRole('button', { name: 'Target agent' }));
    await waitFor(() => expect(screen.getByText('Worker Agent')).toBeInTheDocument());
    expect(screen.getByText('Another Agent')).toBeInTheDocument();
    expect(screen.queryByText('Orchestrator')).not.toBeInTheDocument();
  });

  it('selecting a target persists the real field name target_agent_id, matching the backend', async () => {
    renderPanel(makeAgent({ id: 'a1' }), subagentNode);
    const trigger = screen.getByRole('button', { name: 'Target agent' });
    openSelect(trigger);
    await waitFor(() => expect(screen.getByText('Worker Agent')).toBeInTheDocument());
    pickOption(trigger, 'Worker Agent');

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'n2', { target_agent_id: 'a2' }, 1);
    });
  });

  it('shows a real warning when the configured target no longer matches any known agent', async () => {
    renderPanel(makeAgent({ id: 'a1' }), { ...subagentNode, config: { target_agent_id: 'deleted-agent' } });
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByText(/doesn't match any agent currently visible/i)).toBeInTheDocument();
    });
  });
});
