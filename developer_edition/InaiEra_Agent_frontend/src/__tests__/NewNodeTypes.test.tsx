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

function makeAgent(nodeType: string, config: Record<string, any> = {}): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: nodeType, x: 200, y: 140, sub: '', config },
        { id: 'n3', type: 'respond', x: 400, y: 140, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        { id: 'e2', source: 'n2', target: 'n3', source_handle: null },
      ],
    },
  } as Agent;
}

function renderBuilder(agent: Agent) {
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
  return render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
}

/** Every one of these node type labels ("Answer", "Parameter
 * Extractor", "Variable Assigner") genuinely appears twice once a node
 * of that type exists on the canvas: once as the palette's own
 * draggable node-type entry, once as the selected node's config panel
 * SecHead — the same real ambiguity class caught earlier with the
 * sidebar's "Settings" nav item. Scope to the config panel specifically
 * rather than a plain screen.getByText(...), which would throw on the
 * genuine multiple match. */
function waitForConfigHeader(text: string) {
  return waitFor(() => expect(document.querySelector('.cfg .sechead')?.textContent).toBe(text));
}

async function selectNode() {
  await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
  fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);
}

describe('Answer node — real config UI', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the real Chatflow-only note and a real content textarea', async () => {
    renderBuilder(makeAgent('answer'));
    await selectNode();
    await waitForConfigHeader('Answer');
    expect(screen.getByText(/Chatflow apps only/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Hello \{\{name\}\}/)).toBeInTheDocument();
  });

  it('typing real content and blurring genuinely saves it via the real PATCH call', async () => {
    renderBuilder(makeAgent('answer'));
    await selectNode();
    await waitForConfigHeader('Answer');
    const textarea = screen.getByPlaceholderText(/Hello \{\{name\}\}/);
    fireEvent.change(textarea, { target: { value: 'Thanks, {{name}}!' } });
    fireEvent.blur(textarea);
    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'n2', expect.objectContaining({ content: 'Thanks, {{name}}!' }), 1);
    });
  });

  it('mentions real multiple-Answer-node accumulation behavior, not overwrite', async () => {
    renderBuilder(makeAgent('answer'));
    await selectNode();
    await waitFor(() => expect(screen.getByText(/running transcript/)).toBeInTheDocument());
  });
});

describe('Parameter Extractor node — real config UI', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the real header and starts with no parameters', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    expect(screen.getByText('No parameters yet — add one above.')).toBeInTheDocument();
  });

  it('adding a real parameter shows real name/type/description/required fields', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    fireEvent.click(screen.getByTitle('Add parameter'));
    await waitFor(() => expect(screen.getByPlaceholderText('parameter_name')).toBeInTheDocument());
    expect(screen.getByPlaceholderText(/Description/)).toBeInTheDocument();
    expect(screen.getByText('Required')).toBeInTheDocument();
  });

  it('real __is_success and __reason built-in output variables are always shown', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    expect(screen.getByText('__is_success')).toBeInTheDocument();
    expect(screen.getByText('__reason')).toBeInTheDocument();
  });

  it('a real added parameter shows up in the real output variables list, matching its real configured name', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    fireEvent.click(screen.getByTitle('Add parameter'));
    await waitFor(() => expect(screen.getByPlaceholderText('parameter_name')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('parameter_name'), { target: { value: 'city' } });
    fireEvent.blur(screen.getByPlaceholderText('parameter_name'));
    await waitFor(() => {
      const kvs = [...document.querySelectorAll('.kv')].map((el) => el.textContent);
      expect(kvs.some((t) => t?.includes('city'))).toBe(true);
    });
  });

  it('switching inference mode to Prompt-based shows real prompt-mode explanatory text', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    expect(screen.getByText(/Anthropic tool-use/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Inference mode' })).toHaveTextContent('Function Call / Tool Call');
    pickOption(screen.getByRole('button', { name: 'Inference mode' }), 'Prompt-based');
    await waitFor(() => expect(screen.getByText(/pure-prompt extraction/)).toBeInTheDocument());
  });

  it('enabling Memory shows a real explanatory note about conversation history', async () => {
    renderBuilder(makeAgent('parameter_extractor'));
    await selectNode();
    await waitForConfigHeader('Parameter Extractor');
    expect(screen.queryByText(/includes real conversation history/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Memory').parentElement!.querySelector('.tog')!);
    await waitFor(() => expect(screen.getByText(/includes real conversation history/)).toBeInTheDocument());
  });
});

describe('Variable Assigner node — real config UI', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the real conversation-persistence explanation and starts with no assignments', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    expect(screen.getByText(/genuinely persistent/)).toBeInTheDocument();
    expect(screen.getByText('No assignments yet — add one above.')).toBeInTheDocument();
  });

  it('adding a real assignment defaults to Overwrite and shows a real source-variable field', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Operation' })).toHaveTextContent('Overwrite (from a variable)');
    expect(screen.getByPlaceholderText(/source_var.*Arithmetic/)).toBeInTheDocument();
  });

  it('switching operation to Arithmetic shows a real operator dropdown, not the plain source field alone', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Operation' })).toHaveTextContent('Overwrite (from a variable)');
    pickOption(screen.getByRole('button', { name: 'Operation' }), 'Arithmetic (number)');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Arithmetic operation' })).toHaveTextContent('Add'));
  });

  it('switching operation to Set shows a real fixed-value field instead of a variable reference', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Operation' })).toHaveTextContent('Overwrite (from a variable)');
    pickOption(screen.getByRole('button', { name: 'Operation' }), 'Set (a fixed value)');
    await waitFor(() => expect(screen.getByPlaceholderText('Fixed value')).toBeInTheDocument());
    expect(screen.queryByPlaceholderText(/source_var.*Arithmetic/)).not.toBeInTheDocument();
  });

  it('switching operation to Clear shows neither a source field nor a fixed-value field', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Operation' })).toHaveTextContent('Overwrite (from a variable)');
    pickOption(screen.getByRole('button', { name: 'Operation' }), 'Clear');
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/source_var.*Arithmetic/)).not.toBeInTheDocument();
      expect(screen.queryByPlaceholderText('Fixed value')).not.toBeInTheDocument();
    });
  });

  it('typing a real variable name and blurring genuinely saves it via the real PATCH call', async () => {
    renderBuilder(makeAgent('variable_assigner'));
    await selectNode();
    await waitForConfigHeader('Variable Assigner');
    fireEvent.click(screen.getByTitle('Add assignment'));
    await waitFor(() => expect(screen.getByPlaceholderText('variable_name')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('variable_name'), { target: { value: 'visit_count' } });
    fireEvent.blur(screen.getByPlaceholderText('variable_name'));
    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'n2', {
        assignments: [{ variable: 'visit_count', operation: 'overwrite', source_var: '', value: '', arithmetic_op: '+' }],
      }, 1);
    });
  });
});
