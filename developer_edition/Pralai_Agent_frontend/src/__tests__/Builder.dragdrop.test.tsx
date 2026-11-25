import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

/** A brand-new agent as create_agent now produces it: the trigger alone. */
function triggerOnlyAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Fresh Agent', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1,
    health_score: null, health_status: '—', tags: [], description: '',
    updated_at: '2026-01-01T00:00:00', model: 'Claude Sonnet 5', mode: 'agentic',
    runs_7d: 0, cost_7d: '$0.00', first_party: false, metrics: {}, app_type: 'chatbot',
    flow: {
      nodes: [{ id: 'n1', type: 'trigger', x: 40, y: 140, sub: 'user input', config: {} }],
      edges: [],
    },
    ...overrides,
  } as Agent;
}

/** A DataTransfer good enough for drag-and-drop, which jsdom does not provide. */
function dataTransfer() {
  const store: Record<string, string> = {};
  return {
    setData: (k: string, v: string) => { store[k] = v; },
    getData: (k: string) => store[k] ?? '',
    setDragImage: () => {},
    get types() { return Object.keys(store); },
    dropEffect: 'move',
    effectAllowed: 'all',
  };
}

describe('Builder palette drag-and-drop onto a trigger-only canvas', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(triggerOnlyAgent());
    vi.mocked(api.addNode).mockImplementation(async () => triggerOnlyAgent({
      draft_revision: 2,
      flow: {
        nodes: [
          { id: 'n1', type: 'trigger', x: 40, y: 140, sub: 'user input', config: {} },
          { id: 'n2', type: 'llm', x: 300, y: 140, sub: 'generate', config: {} },
        ],
        edges: [],
      },
    }));
  });

  it('shows the node palette even when the canvas has only a trigger', async () => {
    render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText('Reasoning')).toBeInTheDocument());
    // A palette that renders nothing is indistinguishable from one that
    // cannot be dragged, so assert the draggable items are really there.
    expect(screen.getByText('LLM')).toBeInTheDocument();
  });

  it('every palette entry you can actually add is draggable', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText('Reasoning')).toBeInTheDocument());
    const pal = Array.from(container.querySelectorAll('.palnode'));
    expect(pal.length).toBeGreaterThan(10);
    // The "... on canvas" rows are captions for the nodes that are placed
    // for you (trigger, the automatic UCS check, output) rather than
    // things to drag in, so they are legitimately not draggable.
    const addable = pal.filter((p) => !/on canvas/.test(p.textContent || ''));
    const notDraggable = addable
      .filter((p) => p.getAttribute('draggable') !== 'true')
      .map((p) => p.textContent);
    expect(notDraggable).toEqual([]);
  });

  it('the trigger node is not draggable, matching the server rule', async () => {
    // move_node refuses trigger/gov/respond with "This node is fixed and
    // cannot be moved (§4.4)". Marking them draggable in the UI only
    // converts a dead drag into a 400 and an error toast, so the two
    // sides are deliberately kept in agreement.
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText('Reasoning')).toBeInTheDocument());
    const trigger = container.querySelector('.cnode.trigger')!;
    expect(trigger.getAttribute('draggable')).toBe('false');
  });

  it('dragging a palette node onto the canvas really calls addNode', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText('Reasoning')).toBeInTheDocument());

    const llm = Array.from(container.querySelectorAll('.palnode'))
      .find((el) => el.textContent?.includes('LLM'))!;
    const canvas = container.querySelector('.canvas')!;
    const dt = dataTransfer();

    fireEvent.dragStart(llm, { dataTransfer: dt });
    // The payload the canvas reads back. Without it the drop is a no-op,
    // which looks exactly like "drag and drop does not work".
    expect(dt.getData('text/plain')).toBe('new:llm');

    fireEvent.dragOver(canvas, { dataTransfer: dt });
    fireEvent.drop(canvas, { dataTransfer: dt, clientX: 320, clientY: 180 });

    await waitFor(() => expect(api.addNode).toHaveBeenCalled());
    const [agentId, body, ifMatch] = vi.mocked(api.addNode).mock.calls[0];
    expect(agentId).toBe('a1');
    expect(body.type).toBe('llm');
    expect(ifMatch).toBe(1);
  });

  it('the dropped node appears on the canvas afterwards', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText('Reasoning')).toBeInTheDocument());
    expect(container.querySelectorAll('.cnode').length).toBe(1);

    const llm = Array.from(container.querySelectorAll('.palnode'))
      .find((el) => el.textContent?.includes('LLM'))!;
    const canvas = container.querySelector('.canvas')!;
    const dt = dataTransfer();
    fireEvent.dragStart(llm, { dataTransfer: dt });
    fireEvent.dragOver(canvas, { dataTransfer: dt });
    fireEvent.drop(canvas, { dataTransfer: dt, clientX: 320, clientY: 180 });

    await waitFor(() => expect(container.querySelectorAll('.cnode').length).toBe(2));
  });
});
