import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => {
  cleanup();
});

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'gov', x: 200, y: 140, sub: '', config: {} },
        { id: 'n3', type: 'condition', x: 360, y: 140, sub: '', config: { expression: '1==1' } },
        { id: 'n4', type: 'template', x: 520, y: 80, sub: '', config: {} },
        { id: 'n5', type: 'template', x: 520, y: 220, sub: '', config: {} },
        { id: 'n6', type: 'respond', x: 700, y: 140, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        { id: 'e2', source: 'n2', target: 'n3', source_handle: null },
        // n3 (condition) deliberately has NO outgoing edges yet — this is
        // exactly the "empty port, needs connecting" state from the
        // screenshot that started this whole feature.
      ],
    },
    ...overrides,
  } as Agent;
}

describe('Builder drag-to-connect (real component, real DOM events)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.addEdge).mockResolvedValue(makeAgent());
    vi.mocked(api.deleteEdge).mockResolvedValue(makeAgent());
    vi.mocked(api.addNode).mockResolvedValue(makeAgent());
  });

  it('renders two distinct output ports for a condition node (True/False)', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => {
      const ports = container.querySelectorAll('.port.true, .port.false');
      expect(ports.length).toBe(2);
    });
  });

  it('dragging from the condition node\'s TRUE port to another node calls api.addEdge with source_handle "true"', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());

    const canvas = await waitFor(() => {
      const el = container.querySelector('.canvas');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });

    // canvas needs a real bounding box for the coordinate math in the
    // component (jsdom returns all-zero rects by default)
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const truePort = await waitFor(() => {
      const el = container.querySelector('.port.true');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const targetNode = container.querySelector('[data-nodeid="n4"]') as HTMLElement;
    expect(targetNode).toBeTruthy();

    // the actual sequence a person performs: press on the port, drag, release on the target node
    fireEvent.mouseDown(truePort, { clientX: 488, clientY: 154 });
    fireEvent.mouseMove(canvas, { clientX: 550, clientY: 100 });
    fireEvent.mouseUp(targetNode, { clientX: 584, clientY: 107 });

    await waitFor(() => {
      expect(api.addEdge).toHaveBeenCalledWith('a1', 'n3', 'n4', 'true', 1);
    });
  });

  it('dragging from the condition node\'s FALSE port to a different node calls api.addEdge with source_handle "false"', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const falsePort = await waitFor(() => {
      const el = container.querySelector('.port.false');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    const targetNode = container.querySelector('[data-nodeid="n5"]') as HTMLElement;

    fireEvent.mouseDown(falsePort, { clientX: 488, clientY: 178 });
    fireEvent.mouseMove(canvas, { clientX: 550, clientY: 230 });
    fireEvent.mouseUp(targetNode, { clientX: 584, clientY: 247 });

    await waitFor(() => {
      expect(api.addEdge).toHaveBeenCalledWith('a1', 'n3', 'n5', 'false', 1);
    });
  });

  it('releasing the drag over the trigger node does NOT call api.addEdge (trigger has no input)', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const truePort = container.querySelector('.port.true') as HTMLElement;
    const triggerNode = container.querySelector('[data-nodeid="n1"]') as HTMLElement;

    fireEvent.mouseDown(truePort, { clientX: 488, clientY: 154 });
    fireEvent.mouseMove(canvas, { clientX: 60, clientY: 154 });
    fireEvent.mouseUp(triggerNode, { clientX: 60, clientY: 154 });

    // give any pending microtasks a chance to run, then assert nothing fired
    await new Promise((r) => setTimeout(r, 50));
    expect(api.addEdge).not.toHaveBeenCalled();
  });

  it('clicking an existing edge line calls api.deleteEdge with that edge\'s id', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());

    const edgeHit = await waitFor(() => {
      const paths = container.querySelectorAll('.edge-hit');
      expect(paths.length).toBeGreaterThan(0);
      return paths[0] as SVGPathElement;
    });
    fireEvent.click(edgeHit);

    await waitFor(() => {
      expect(api.deleteEdge).toHaveBeenCalledWith('a1', 'e1', 1);
    });
  });

  it('a small press-and-release on a port (no real drag) opens the node picker instead of connecting', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const truePort = container.querySelector('.port.true') as HTMLElement;
    // press and release at nearly the same point — a click, not a drag
    fireEvent.mouseDown(truePort, { clientX: 488, clientY: 154 });
    fireEvent.mouseUp(truePort, { clientX: 490, clientY: 155 });

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/agent, human, knowledge/i)).toBeInTheDocument();
    });
    // no edge should have been created by a click
    expect(api.addEdge).not.toHaveBeenCalled();
  });

  it('picking a node type from the picker creates a new node AND connects it to the source port in one step', async () => {
    const agentAfterAdd = makeAgent({
      flow: {
        nodes: [
          ...makeAgent().flow.nodes,
          { id: 'n99', type: 'code', x: 580, y: 70, sub: '', config: {} },
        ],
        edges: makeAgent().flow.edges,
      },
    });
    const agentAfterConnect = makeAgent({
      flow: {
        nodes: agentAfterAdd.flow.nodes,
        edges: [...makeAgent().flow.edges, { id: 'e99', source: 'n3', target: 'n99', source_handle: 'true' }],
      },
    });
    vi.mocked(api.addNode).mockResolvedValue(agentAfterAdd);
    vi.mocked(api.addEdge).mockResolvedValue(agentAfterConnect);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const truePort = container.querySelector('.port.true') as HTMLElement;
    fireEvent.mouseDown(truePort, { clientX: 488, clientY: 154 });
    fireEvent.mouseUp(truePort, { clientX: 489, clientY: 154 });

    const codeOption = await waitFor(() => {
      const picker = container.querySelector('.node-picker') as HTMLElement;
      expect(picker).toBeInTheDocument();
      const el = within(picker).getByText('Code');
      return el;
    });
    fireEvent.click(codeOption);

    await waitFor(() => {
      expect(api.addNode).toHaveBeenCalledWith('a1', expect.objectContaining({ type: 'code' }), 1);
    });
    await waitFor(() => {
      expect(api.addEdge).toHaveBeenCalledWith('a1', 'n3', 'n99', 'true', 1);
    });
  });

  it('clicking outside the picker closes it without creating anything', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800, x: 0, y: 0, toJSON: () => {},
    });
    const truePort = container.querySelector('.port.true') as HTMLElement;
    fireEvent.mouseDown(truePort, { clientX: 488, clientY: 154 });
    fireEvent.mouseUp(truePort, { clientX: 489, clientY: 154 });
    await waitFor(() => expect(screen.getByPlaceholderText(/agent, human, knowledge/i)).toBeInTheDocument());

    const overlay = container.querySelector('.overlay') as HTMLElement;
    fireEvent.click(overlay);

    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/agent, human, knowledge/i)).not.toBeInTheDocument();
    });
    expect(api.addNode).not.toHaveBeenCalled();
  });
});
