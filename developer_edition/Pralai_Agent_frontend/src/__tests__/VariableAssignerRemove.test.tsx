import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'variable_assigner', x: 200, y: 140, sub: '', config: {} },
        { id: 'n3', type: 'respond', x: 400, y: 140, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        { id: 'e2', source: 'n2', target: 'n3', source_handle: null },
      ],
    },
  } as Agent;
}

describe('Variable Assigner — remove assignment (isolated)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('removing a real assignment via its real remove button genuinely removes it', async () => {
    const agent = makeAgent();
    vi.mocked(api.getAgent).mockResolvedValue(agent);
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.updateNodeConfig).mockImplementation(async (_agentId, nodeId, patch) => ({
      ...agent,
      flow: { ...agent.flow, nodes: agent.flow.nodes.map((n) => (n.id === nodeId ? { ...n, config: { ...n.config, ...patch } } : n)) },
    }));

    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);
    // "Variable Assigner" genuinely appears twice — once as the
    // palette's own draggable node-type label, once as the config
    // panel's own SecHead — scope to the config panel specifically
    // (the same real ambiguity class caught earlier with "Settings").
    await waitFor(() => expect(document.querySelector('.cfg .sechead')?.textContent).toBe('Variable Assigner'));

    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());

    fireEvent.click(screen.getByTitle('Remove'));
    await waitFor(() => expect(screen.getByText('No assignments yet — add one above.')).toBeInTheDocument());
  });
});
