import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, fireEvent, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import NodeConfigPanel from '../components/NodeConfigPanel';
import AddInputFieldModal from '../components/AddInputFieldModal';
import { api, ApiError } from '../api';
import type { Agent, FlowNode } from '../types';

vi.mock('../api', async (importOriginal) => {
  // Real, necessary partial mock (§0.71, GAP_REPORT test fix) — a
  // plain `vi.mock('../api')` auto-mocks EVERY export, including the
  // real ApiError class itself, which breaks `err instanceof ApiError`
  // inside Builder.tsx's own handleMutationError (the mocked class
  // loses its real prototype chain). Keep the real ApiError (and BASE)
  // from the actual module; only the request functions get auto-mocked.
  const actual = await importOriginal<typeof import('../api')>();
  return { ...actual, api: Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()])) };
});

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'App', subtype: 'Workflow', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'llm', x: 300, y: 140, sub: '', config: {} },
      ],
      edges: [],
    },
    ...overrides,
  } as Agent;
}

async function setupCanvasAndDrag(container: HTMLElement) {
  const canvas = await waitFor(() => {
    const el = container.querySelector('.canvas');
    expect(el).toBeTruthy();
    return el as HTMLElement;
  });
  canvas.getBoundingClientRect = () => ({
    left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
  });
  const port = await waitFor(() => {
    const el = container.querySelector('[data-nodeid="n1"].port');
    expect(el).toBeTruthy();
    return el as HTMLElement;
  });
  const targetNode = container.querySelector('[data-nodeid="n2"]') as HTMLElement;
  expect(targetNode).toBeTruthy();

  fireEvent.mouseDown(port, { clientX: 60, clientY: 154 });
  fireEvent.mouseMove(canvas, { clientX: 300, clientY: 150 });
  fireEvent.mouseUp(targetNode, { clientX: 310, clientY: 150 });
}

describe('Builder — real optimistic-locking conflict handling (§0.71, GAP_REPORT)', () => {
  beforeEach(() => {
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.listAgents).mockResolvedValue([]);
  });

  it('the breadcrumb links back to Studio', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    const back = await screen.findByRole('link', { name: 'Studio' });
    expect(back).toHaveAttribute('href', '/agents');
  });

  it('a real 409 from a mutation triggers a real refresh to the latest agent state, not a generic error toast', async () => {
    const staleAgent = makeAgent();
    const freshAgent = makeAgent({ draft_revision: 2 });

    vi.mocked(api.getAgent).mockResolvedValue(staleAgent);
    vi.mocked(api.addEdge).mockRejectedValue(new ApiError(
      "This agent's flow has changed since you last loaded it (your revision 1, current revision 2) — reload and reapply your change.",
      409,
    ));

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const callsBeforeDrag = vi.mocked(api.getAgent).mock.calls.length;
    vi.mocked(api.getAgent).mockResolvedValue(freshAgent);

    await setupCanvasAndDrag(container);

    // The real, honest recovery: a real second GET refreshes the
    // Builder to the latest state — not left showing stale data with
    // no indication anything went wrong.
    await waitFor(() => expect(vi.mocked(api.getAgent).mock.calls.length).toBeGreaterThan(callsBeforeDrag));
  });

  it('a real non-409 error does not trigger the real, extra refetch the 409 path uses', async () => {
    const agent = makeAgent();
    vi.mocked(api.getAgent).mockResolvedValue(agent);
    vi.mocked(api.addEdge).mockRejectedValue(new ApiError('Source or target node not found', 404));

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const callsBeforeDrag = vi.mocked(api.getAgent).mock.calls.length;

    await setupCanvasAndDrag(container);
    await waitFor(() => expect(api.addEdge).toHaveBeenCalled());

    // A real, ordinary (non-409) error must never trigger the extra
    // refetch that's specifically for recovering from a genuine
    // revision mismatch — that's the real, distinguishing behavior
    // handleMutationError implements.
    expect(vi.mocked(api.getAgent).mock.calls.length).toBe(callsBeforeDrag);
  });

  it('addEdge is genuinely called with the real current draft_revision as If-Match', async () => {
    const agent = makeAgent({ draft_revision: 7 });
    vi.mocked(api.getAgent).mockResolvedValue(agent);
    vi.mocked(api.addEdge).mockResolvedValue(agent);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());

    await setupCanvasAndDrag(container);

    await waitFor(() => {
      expect(api.addEdge).toHaveBeenCalledWith('a1', 'n1', 'n2', null, 7);
    });
  });

  // §0.100, GAP_REPORT — the actual fix for a real, reported gap:
  // NodeConfigPanel's own shared `set()` function (every node config
  // field change in the Builder funnels through it) and
  // AddInputFieldModal both called api.updateNodeConfig without ever
  // passing agent.draft_revision as If-Match — every OTHER real
  // Builder mutation (addNode/moveNode/deleteNode/addEdge/deleteEdge)
  // already correctly passed it, so node config edits specifically
  // were the one, real, silent exception where two collaborators
  // could still race an unconditional write. These tests render
  // NodeConfigPanel/AddInputFieldModal directly, the same real,
  // established pattern this test suite's own other node-type tests
  // (AgentSubagent/Comment/TriggerPicker/NewNodeTypes) already use.
  it('NodeConfigPanel\'s own shared set() passes the real current draft_revision as If-Match', async () => {
    const agent = makeAgent({
      draft_revision: 5,
      flow: { nodes: [{ id: 'note1', type: 'comment', x: 0, y: 0, sub: '', config: { title: 'old title' } }], edges: [] },
    });
    vi.mocked(api.updateNodeConfig).mockResolvedValue(agent);
    vi.mocked(api.listAgents).mockResolvedValue([]);

    render(
      <AppProvider>
        <NodeConfigPanel agent={agent} node={agent.flow.nodes[0]} onAgentUpdate={vi.fn()} onDelete={vi.fn()} />
      </AppProvider>
    );

    const titleInput = await screen.findByDisplayValue('old title');
    fireEvent.change(titleInput, { target: { value: 'new title' } });
    fireEvent.blur(titleInput);

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith(
        'a1', 'note1', expect.objectContaining({ title: 'new title' }), 5
      );
    });
  });

  it('AddInputFieldModal passes the real current draft_revision as If-Match', async () => {
    const agent = makeAgent({
      draft_revision: 9,
      flow: { nodes: [{ id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: { fields: [] } }], edges: [] },
    });
    vi.mocked(api.updateNodeConfig).mockResolvedValue(agent);

    render(
      <AppProvider>
        <AddInputFieldModal agent={agent} node={agent.flow.nodes[0]} onClose={vi.fn()} onSaved={vi.fn()} />
      </AppProvider>
    );

    fireEvent.change(screen.getByPlaceholderText('e.g. topic'), { target: { value: 'topic' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith(
        'a1', 'n1', expect.objectContaining({ fields: expect.any(Array) }), 9
      );
    });
  });
});
