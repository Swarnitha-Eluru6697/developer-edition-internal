import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Snippets from '../pages/Snippets';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeSnippet(overrides?: Partial<Agent>): Agent {
  return {
    id: 'snip1', name: 'Sentiment Analysis', type: 'App', subtype: 'Custom', plane: 'Snippet',
    owner: 'you', status: 'published', version: 'v2', health_score: null, health_status: '—', tags: [], description: '',
    updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: { nodes: [{ id: 's1', type: 'llm', x: 0, y: 0, sub: '', config: {} }], edges: [] },
    input_variables: [{ name: 'review_text', type: 'string', description: '' }],
    ...overrides,
  } as Agent;
}

describe('Snippets page — real list, filtered from the regular agent list', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([makeSnippet()]);
  });

  it('requests only real plane=Snippet agents from the backend', async () => {
    render(<MemoryRouter><AppProvider><Snippets /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(api.listAgents).toHaveBeenCalledWith('Snippet'));
  });

  it('shows the real snippet name, node count, and input variable count', async () => {
    render(<MemoryRouter><AppProvider><Snippets /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Sentiment Analysis')).toBeInTheDocument());
    expect(screen.getByText(/1 node/)).toBeInTheDocument();
    expect(screen.getByText(/1 input variable/)).toBeInTheDocument();
  });

  it('an empty real list shows a real, honest empty state, not a blank page', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([]);
    render(<MemoryRouter><AppProvider><Snippets /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/No snippets yet/)).toBeInTheDocument());
  });

  it('real search genuinely filters the real list, not a decorative input', async () => {
    vi.mocked(api.listAgents).mockResolvedValue([makeSnippet(), makeSnippet({ id: 'snip2', name: 'Translation Helper' })]);
    render(<MemoryRouter><AppProvider><Snippets /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Sentiment Analysis')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Search snippets…'), { target: { value: 'translation' } });
    expect(screen.queryByText('Sentiment Analysis')).not.toBeInTheDocument();
    expect(screen.getByText('Translation Helper')).toBeInTheDocument();
  });

  it('creating a real blank snippet calls the real create endpoint with plane=Snippet', async () => {
    // The name is asked for in a side panel now, not window.prompt.
    vi.mocked(api.createAgent).mockResolvedValue(makeSnippet({ id: 'snip-new', name: 'New Snippet' }));
    render(<MemoryRouter><AppProvider><Snippets /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    fireEvent.click(screen.getByText('Create Snippet'));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'New Snippet' } });
    fireEvent.click(screen.getByText('Create snippet'));
    await waitFor(() => {
      expect(api.createAgent).toHaveBeenCalledWith(expect.objectContaining({ name: 'New Snippet', plane: 'Snippet' }));
    });
  });
});
