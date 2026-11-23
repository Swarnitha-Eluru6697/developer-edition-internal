import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import ToastHost from '../components/ToastHost';
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
        { id: 'note1', type: 'comment', x: 400, y: 40, sub: '', config: { title: 'Bring in your data', body: 'Upload reference docs here.' } },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }],
    },
    ...overrides,
  } as Agent;
}

describe('Comment node — documentation only, never part of execution', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.addEdge).mockResolvedValue(makeAgent());
  });

  it('renders as a sticky note showing its real title and body, not a generic node card', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByText('Bring in your data')).toBeInTheDocument();
    });
    expect(screen.getByText('Upload reference docs here.')).toBeInTheDocument();
    const note = container.querySelector('.comment-note');
    expect(note).toBeInTheDocument();
    // it must NOT render as a regular execution node
    expect(note?.className).not.toContain('cnode');
  });

  it('has no output port — it can never be dragged-from to connect into the flow', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('Bring in your data')).toBeInTheDocument());
    const ports = container.querySelectorAll('[data-nodeid="note1"].port');
    expect(ports.length).toBe(0);
  });

  it('dragging a real connection and dropping it onto a comment is rejected with an honest message, not silently wired in', async () => {
    const agentWithLlm = makeAgent({
      flow: {
        nodes: [...makeAgent().flow.nodes, { id: 'llm1', type: 'llm', x: 400, y: 200, sub: '', config: {} }],
        edges: [
          { id: 'e1', source: 'n1', target: 'n2', source_handle: null },
        ],
      },
    });
    vi.mocked(api.getAgent).mockResolvedValue(agentWithLlm);

    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /><ToastHost /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 1200, bottom: 800, width: 1200, height: 800, x: 0, y: 0, toJSON: () => {},
    });

    const govPort = container.querySelector('[data-nodeid="n2"].port') as HTMLElement;
    fireEvent.mouseDown(govPort, { clientX: 328, clientY: 167 });
    fireEvent.mouseMove(canvas, { clientX: 400, clientY: 40 });
    const commentEl = container.querySelector('[data-nodeid="note1"]') as HTMLElement;
    fireEvent.mouseUp(commentEl, { clientX: 400, clientY: 40 });

    await waitFor(() => {
      expect(screen.getByText(/documentation, not a flow step/i)).toBeInTheDocument();
    });
    expect(api.addEdge).not.toHaveBeenCalled();
  });

  it('the config panel shows real Title/Body fields and persists edits via the standard config PATCH', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
    render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    const note = await waitFor(() => {
      const el = screen.getByText('Bring in your data');
      expect(el).toBeInTheDocument();
      return el;
    });
    fireEvent.click(note.closest('.comment-note')!);

    await waitFor(() => expect(screen.getByPlaceholderText(/Bring in your data/i)).toBeInTheDocument());
    const titleInput = screen.getByPlaceholderText(/Bring in your data/i);
    fireEvent.change(titleInput, { target: { value: 'Updated title' } });
    fireEvent.blur(titleInput);

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'note1', expect.objectContaining({ title: 'Updated title' }), 1);
    });
  });

  it('shows 6 real color swatches, and picking one genuinely persists and applies the real color', async () => {
    vi.mocked(api.updateNodeConfig).mockImplementation(async (_agentId, nodeId, patch) => {
      const a = makeAgent();
      return { ...a, flow: { ...a.flow, nodes: a.flow.nodes.map((n) => (n.id === nodeId ? { ...n, config: { ...n.config, ...patch } } : n)) } };
    });
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    fireEvent.click(container.querySelector('[data-nodeid="note1"]')!);
    await waitFor(() => expect(container.querySelectorAll('.comment-color-swatch').length).toBe(6));

    const swatches = container.querySelectorAll('.comment-color-swatch');
    fireEvent.click(swatches[4]); // pink

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'note1', expect.objectContaining({ color: 'pink' }), 1);
    });
    await waitFor(() => {
      const note = container.querySelector('[data-nodeid="note1"]') as HTMLElement;
      expect(note.style.background).toBe('rgb(253, 237, 246)'); // real pink swatch bg, genuinely applied to the canvas note
    });
  });

  it('clicking Bold genuinely wraps the real selected text with real Markdown, not a no-op button', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    fireEvent.click(container.querySelector('[data-nodeid="note1"]')!);

    const textarea = await waitFor(() => {
      const el = container.querySelector('#comment-body-textarea') as HTMLTextAreaElement;
      expect(el).toBeTruthy();
      expect(el.value).toBe('Upload reference docs here.');
      return el;
    });
    textarea.setSelectionRange(0, 6); // select "Upload"
    fireEvent.click(screen.getByTitle('Bold'));

    await waitFor(() => {
      expect((container.querySelector('#comment-body-textarea') as HTMLTextAreaElement).value).toBe('**Upload** reference docs here.');
    });
  });

  it('a real bold selection genuinely renders as bold on the canvas note itself', async () => {
    const boldAgent = makeAgent({
      flow: {
        nodes: [
          { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
          { id: 'note1', type: 'comment', x: 400, y: 40, sub: '', config: { title: 'Note', body: '**important** detail' } },
        ],
        edges: [],
      },
    });
    vi.mocked(api.getAgent).mockResolvedValue(boldAgent);
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => {
      const strong = container.querySelector('.comment-body strong');
      expect(strong?.textContent).toBe('important');
    });
  });
});
