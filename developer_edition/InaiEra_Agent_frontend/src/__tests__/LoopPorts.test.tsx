import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

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
        // a real loop container with two real children already inside it
        { id: 'nl1', type: 'loop', x: 400, y: 140, sub: '', config: { max_loops: 5 } },
        { id: 'c1', type: 'template', x: 420, y: 140, sub: '', config: {}, parent_id: 'nl1' },
        { id: 'c2', type: 'exit_loop', x: 580, y: 140, sub: '', config: {}, parent_id: 'nl1' },
        { id: 'n3', type: 'respond', x: 800, y: 140, sub: '', config: {} },
      ],
      edges: [
        { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        { id: 'ce1', source: 'c1', target: 'c2', source_handle: null },
      ],
    },
    ...overrides,
  } as Agent;
}

describe('Loop — real nested container (children via parent_id, real Exit Loop node)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.addNode).mockResolvedValue(makeAgent());
    vi.mocked(api.addEdge).mockResolvedValue(makeAgent());
  });

  it('renders a visible container box around the loop and its real children', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => {
      expect(container.querySelector('.loop-container')).toBeInTheDocument();
    });
    // the container's bounding box must be wide enough to actually span
    // from the loop node through its rightmost child (c2 at x=580) — not
    // just decoration sized to the loop node alone
    const box = container.querySelector('.loop-container') as HTMLElement;
    const width = parseFloat(box.style.width);
    expect(width).toBeGreaterThan(300); // loop(400) to c2(580)+NODE_W(128) plus padding
  });

  it('the loop node has a single regular output port, not the old two-port body/exit split', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('.loop-container')).toBeInTheDocument());
    expect(container.querySelector('.port.body')).not.toBeInTheDocument();
    expect(container.querySelector('.port.exit')).not.toBeInTheDocument();
  });

  it('dropping a new node inside the loop\'s rendered box assigns real parent_id, not just visual placement', async () => {
    const agentWithNewChild = makeAgent({
      flow: {
        nodes: [...makeAgent().flow.nodes, { id: 'c3', type: 'code', x: 430, y: 140, sub: '', config: {}, parent_id: 'nl1' }],
        edges: makeAgent().flow.edges,
      },
    });
    vi.mocked(api.addNode).mockResolvedValue(agentWithNewChild);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1400, bottom: 900, width: 1400, height: 900, x: 0, y: 0, toJSON: () => {},
    });

    // drop a "code" node at a point that falls INSIDE the loop's bounding
    // box (loop spans roughly x=376..732 given its children at 400/420..580+128).
    // jsdom's DragEvent constructor doesn't reliably apply clientX/clientY
    // from MouseEventInit, so they're set explicitly via defineProperty —
    // the same real gotcha that made an earlier version of this test pass
    // for the wrong reason (NaN coordinates always fail containment,
    // masking a missing property rather than proving correct behavior).
    const dataTransfer = { getData: () => 'new:code', setData: () => {} };
    const dropEvent = new Event('drop', { bubbles: true, cancelable: true }) as any;
    dropEvent.dataTransfer = dataTransfer;
    Object.defineProperty(dropEvent, 'clientX', { value: 450 });
    Object.defineProperty(dropEvent, 'clientY', { value: 160 });
    fireEvent(canvas, dropEvent);

    await waitFor(() => {
      expect(api.addNode).toHaveBeenCalledWith('a1', expect.objectContaining({ type: 'code', parent_id: 'nl1' }), 1);
    });
  });

  it('dropping a new node OUTSIDE any loop box does not assign a parent_id', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1400, bottom: 900, width: 1400, height: 900, x: 0, y: 0, toJSON: () => {},
    });

    const dataTransfer = { getData: () => 'new:code', setData: () => {} };
    const dropEvent = new Event('drop', { bubbles: true, cancelable: true }) as any;
    dropEvent.dataTransfer = dataTransfer;
    Object.defineProperty(dropEvent, 'clientX', { value: 1000 }); // far from the loop
    Object.defineProperty(dropEvent, 'clientY', { value: 500 });
    fireEvent(canvas, dropEvent);

    await waitFor(() => {
      expect(api.addNode).toHaveBeenCalledWith('a1', expect.objectContaining({ type: 'code', parent_id: null }), 1);
    });
  });

  it('the config panel shows the real live child count and whether an Exit Loop node is present', async () => {
    render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const loopNode = await waitFor(() => {
      const el = screen.getByText('LOOP');
      expect(el).toBeInTheDocument();
      return el;
    });
    fireEvent.click(loopNode.closest('.cnode')!);

    await waitFor(() => {
      expect(screen.getByText('Body nodes')).toBeInTheDocument();
    });
    // real count from the seeded agent: c1 + c2 = 2 children
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('yes')).toBeInTheDocument(); // c2 is a real exit_loop node
  });

  it('clicking (not dragging) a fresh loop\'s own port to add its first body node via the picker sets real parent_id — the exact reported bug: growing a loop by clicking its port silently produced a top-level node, not a real child', async () => {
    // a BRAND NEW loop with no children yet — this is the exact scenario
    // that was broken: onPickNodeType (the click-a-port-then-pick-a-type
    // path) never set parent_id at all, only the palette drag-and-drop
    // path did
    const freshLoopAgent = makeAgent({
      flow: {
        nodes: [
          { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
          { id: 'nl2', type: 'loop', x: 400, y: 140, sub: '', config: { max_loops: 5 } },
        ],
        edges: [],
      },
    });
    vi.mocked(api.getAgent).mockResolvedValue(freshLoopAgent);
    const agentAfterAdd = makeAgent({
      flow: {
        nodes: [...freshLoopAgent.flow.nodes, { id: 'newchild', type: 'template', x: 560, y: 140, sub: '', config: {}, parent_id: 'nl2' }],
        edges: freshLoopAgent.flow.edges,
      },
    });
    vi.mocked(api.addNode).mockResolvedValue(agentAfterAdd);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1400, bottom: 900, width: 1400, height: 900, x: 0, y: 0, toJSON: () => {},
    });

    // click (small movement, not a drag) the loop node's single output port
    const loopPort = container.querySelector('[data-nodeid="nl2"].port') as HTMLElement;
    fireEvent.mouseDown(loopPort, { clientX: 528, clientY: 167 });
    fireEvent.mouseUp(loopPort, { clientX: 529, clientY: 167 });

    const templateOption = await waitFor(() => {
      const picker = container.querySelector('.node-picker') as HTMLElement;
      expect(picker).toBeInTheDocument();
      const el = within(picker).getByText('Template');
      return el;
    });
    fireEvent.click(templateOption);

    await waitFor(() => {
      // the real fix: parent_id must be the loop's id, not null/undefined
      expect(api.addNode).toHaveBeenCalledWith('a1', expect.objectContaining({ type: 'template', parent_id: 'nl2' }), 1);
    });
  });
});
