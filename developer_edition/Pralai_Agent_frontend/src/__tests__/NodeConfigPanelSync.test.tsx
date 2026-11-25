import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'Test', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'noteA', type: 'comment', x: 200, y: 140, sub: '', config: { title: 'TITLE_A' } },
        { id: 'noteB', type: 'comment', x: 400, y: 140, sub: '', config: { title: 'TITLE_B' } },
      ],
      edges: [],
    },
  } as Agent;
}

function setupMocks() {
  vi.clearAllMocks();
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
  vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
}

describe('NodeConfigPanel — real config state genuinely syncs to the selected node (regression guard)', () => {
  /**
   * A real, significant bug found live while building the Comment
   * node's real formatting toolbar: `cfg` state's `useState` initial
   * value only ever runs once, on this component's real first-ever
   * mount — which happens with `node` still `undefined` (Builder.tsx
   * renders NodeConfigPanel unconditionally, before any node is
   * selected). That meant `cfg` was permanently stuck at `{}` for the
   * ENTIRE lifetime of the panel, regardless of which node got
   * selected afterward — confirmed live: even a single, first-ever
   * click on a node with real, non-empty config showed a genuinely
   * empty field. Fixed with a real `useEffect` that resyncs `cfg` from
   * `node.config` whenever `node.id` changes. These two tests exist so
   * a future refactor can't silently reintroduce either half of the
   * bug (first selection ever, and switching between two selections).
   */

  it('the very first node ever selected shows its real config immediately, not an empty panel', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-nodeid="noteA"]')).toBeInTheDocument());

    fireEvent.click(container.querySelector('[data-nodeid="noteA"]')!);
    await waitFor(() => {
      const el = container.querySelector('.cfg input[placeholder*="Bring in"]') as HTMLInputElement;
      expect(el?.value).toBe('TITLE_A');
    });
  });

  it('switching from one selected node to another shows the real new config, not stale data from the previous one', async () => {
    setupMocks();
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-nodeid="noteA"]')).toBeInTheDocument());

    fireEvent.click(container.querySelector('[data-nodeid="noteA"]')!);
    await waitFor(() => {
      const el = container.querySelector('.cfg input[placeholder*="Bring in"]') as HTMLInputElement;
      expect(el?.value).toBe('TITLE_A');
    });

    fireEvent.click(container.querySelector('[data-nodeid="noteB"]')!);
    await waitFor(() => {
      const el = container.querySelector('.cfg input[placeholder*="Bring in"]') as HTMLInputElement;
      expect(el?.value).toBe('TITLE_B');
    });
  });
});
