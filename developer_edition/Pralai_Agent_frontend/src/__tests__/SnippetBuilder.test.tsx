import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeSnippetAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'snip1', name: 'My Snippet', type: 'App', subtype: 'Custom', plane: 'Snippet',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '',
    updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: { nodes: [], edges: [] },
    input_variables: [],
    ...overrides,
  } as Agent;
}

function makeWorkflowAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Target Workflow', type: 'App', subtype: 'Workflow', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '',
    updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'gov', x: 200, y: 140, sub: '', config: {} },
        { id: 'n3', type: 'respond', x: 360, y: 140, sub: '', config: {} },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }, { id: 'e2', source: 'n2', target: 'n3', source_handle: null }],
    },
    ...overrides,
  } as Agent;
}

function setupCommonMocks() {
  vi.clearAllMocks();
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
}

describe('Builder — editing a real Snippet restricts the palette (§0.38, GAP_REPORT)', () => {
  beforeEach(() => {
    setupCommonMocks();
    vi.mocked(api.getAgent).mockResolvedValue(makeSnippetAgent());
    vi.mocked(api.listAgents).mockResolvedValue([]);
  });

  it('does not show the static Trigger/Output "on canvas" entries a snippet never has', async () => {
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('Input Variables')).toBeInTheDocument());
    expect(screen.queryByText('on canvas')).not.toBeInTheDocument();
  });

  it('does not offer Knowledge Retrieval or Human Input in the palette, matching Dify\u2019s own documented restriction', async () => {
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('Input Variables')).toBeInTheDocument());
    expect(screen.queryByText('Knowledge Retrieval')).not.toBeInTheDocument();
    expect(screen.queryByText('Human Input')).not.toBeInTheDocument();
  });

  it('still offers real, allowed node types like LLM and Code', async () => {
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('Input Variables')).toBeInTheDocument());
    expect(screen.getByText('LLM')).toBeInTheDocument();
    expect(screen.getByText('Code')).toBeInTheDocument();
  });
});

describe('Builder — real Input Variables editor for a Snippet', () => {
  beforeEach(() => {
    setupCommonMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
  });

  it('shows automatically when a snippet with no nodes loads (nothing else to select)', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeSnippetAgent());
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Input Variables')).toBeInTheDocument());
    expect(screen.getByText('No input variables yet — add one above.')).toBeInTheDocument();
  });

  it('adding a real variable calls the real meta PATCH with a real new entry', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeSnippetAgent());
    vi.mocked(api.setAgentMeta).mockResolvedValue(makeSnippetAgent({ input_variables: [{ name: '', type: 'string', description: '' }] }));
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Input Variables')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Add input variable'));
    await waitFor(() => {
      expect(api.setAgentMeta).toHaveBeenCalledWith('snip1', { input_variables: [{ name: '', type: 'string', description: '' }] });
    });
  });

  it('a real declared variable is genuinely shown, with its real name and type', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeSnippetAgent({ input_variables: [{ name: 'review_text', type: 'string', description: 'the review' }] }));
    render(<MemoryRouter initialEntries={['/builder?agent=snip1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByDisplayValue('review_text')).toBeInTheDocument());
  });
});

describe('Builder — inserting a real published Snippet into a normal workflow', () => {
  beforeEach(() => {
    setupCommonMocks();
  });

  it('lists real published snippets in a real Snippets palette group', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeWorkflowAgent());
    vi.mocked(api.listAgents).mockImplementation((plane) => {
      if (plane === 'Snippet') return Promise.resolve([makeSnippetAgent({ id: 'snip2', name: 'Sentiment Analysis', status: 'published' })]);
      return Promise.resolve([]);
    });
    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Sentiment Analysis')).toBeInTheDocument());
  });

  it('dropping a real snippet onto the canvas calls the real insert-snippet endpoint', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeWorkflowAgent());
    vi.mocked(api.listAgents).mockImplementation((plane) => {
      if (plane === 'Snippet') return Promise.resolve([makeSnippetAgent({ id: 'snip2', name: 'Sentiment Analysis', status: 'published' })]);
      return Promise.resolve([]);
    });
    vi.mocked(api.insertSnippet).mockResolvedValue(makeWorkflowAgent());
    const { container } = render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Sentiment Analysis')).toBeInTheDocument());

    const canvas = container.querySelector('.canvas') as HTMLElement;
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1200, bottom: 800, width: 1200, height: 800, x: 0, y: 0, toJSON: () => {} });

    const dataTransfer = { getData: () => 'snippet:snip2', setData: () => {} };
    fireEvent.drop(canvas, { dataTransfer, clientX: 500, clientY: 300 });

    await waitFor(() => {
      expect(api.insertSnippet).toHaveBeenCalledWith('a1', 'snip2', expect.any(Number), expect.any(Number), 1);
    });
  });
});
