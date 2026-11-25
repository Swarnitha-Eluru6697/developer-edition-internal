import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react';
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
        { id: 'ni1', type: 'iteration', x: 400, y: 140, sub: '', config: { input_var: '{{numbers}}', output_var: 'results' } },
        { id: 'c1', type: 'code', x: 420, y: 140, sub: '', config: {}, parent_id: 'ni1' },
        { id: 'nl1', type: 'loop', x: 700, y: 140, sub: '', config: { max_loops: 5 } },
        { id: 'c2', type: 'exit_loop', x: 720, y: 140, sub: '', config: {}, parent_id: 'nl1' },
        { id: 'n3', type: 'respond', x: 900, y: 140, sub: '', config: {} },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'ni1', source_handle: null }],
    },
    ...overrides,
  } as Agent;
}

describe('Iteration — real container, genuinely separate from loop (loop untouched)', () => {
  function setupMocks() {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.addNode).mockResolvedValue(makeAgent());
    vi.mocked(api.addEdge).mockResolvedValue(makeAgent());
  }

  it('renders a real, separate container box around the iteration node and its real child', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelectorAll('.loop-container').length).toBeGreaterThanOrEqual(2));
    const labels = [...container.querySelectorAll('.loop-container-label')].map((el) => el.textContent);
    expect(labels.some((l) => l?.includes('Iteration'))).toBe(true);
  });

  it('both a real iteration container AND a real loop container coexist without interfering with each other', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelectorAll('.loop-container').length).toBe(2));
  });

  it('dropping a new node inside the iteration box makes it a real body child (parent_id set), not a top-level node', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('.canvas')).toBeInTheDocument());

    const canvasEl = container.querySelector('.canvas') as HTMLElement;
    canvasEl.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1400, bottom: 900, width: 1400, height: 900, x: 0, y: 0, toJSON: () => {},
    });
    // jsdom's DragEvent constructor doesn't reliably apply clientX/clientY
    // from MouseEventInit — set explicitly via defineProperty, the same
    // real gotcha LoopPorts.test.tsx's own working drop test documents.
    const dataTransfer = { getData: () => 'new:code', setData: () => {} };
    const dropEvent = new Event('drop', { bubbles: true, cancelable: true }) as any;
    dropEvent.dataTransfer = dataTransfer;
    Object.defineProperty(dropEvent, 'clientX', { value: 500 });
    Object.defineProperty(dropEvent, 'clientY', { value: 160 });
    fireEvent(canvasEl, dropEvent);

    await waitFor(() => {
      expect(api.addNode).toHaveBeenCalledWith('a1', expect.objectContaining({ type: 'code', parent_id: 'ni1' }), 1);
    });
  });

  it('the loop node itself is genuinely untouched — still renders its own real container, unaffected by iteration support existing', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => {
      const labels = [...container.querySelectorAll('.loop-container-label')].map((el) => el.textContent);
      expect(labels.some((l) => l?.includes('Loop'))).toBe(true);
    });
  });
});
