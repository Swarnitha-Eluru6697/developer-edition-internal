import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import TryItModal from '../components/TryItModal';
import { AppProvider } from '../context/AppContext';
import type { Agent } from '../types';

vi.mock('../api');
import { api } from '../api';

afterEach(() => cleanup());

function renderTryIt(agent: Agent) {
  return render(<AppProvider><TryItModal agent={agent} onClose={() => {}} /></AppProvider>);
}

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Support Bot', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    features: {},
    ...overrides,
  } as Agent;
}

beforeEach(() => {
  // ChatRunner replays the stored transcript on mount so a
  // conversation survives closing the app. These tests drive their
  // own messages, so it resolves empty.
  vi.mocked(api.getChatHistory).mockResolvedValue([]);
});

describe('TryItModal - real publish-immutability awareness (section 0.54, GAP_REPORT)', () => {
  it('a draft agent shows the real "not yet published" banner', () => {
    renderTryIt(makeAgent({ status: 'draft' }));
    expect(screen.getByText(/still/)).toBeInTheDocument();
    expect(screen.getByText('draft')).toBeInTheDocument();
  });

  it('a published agent shows the real live-draft-may-differ banner, naming its real published version', () => {
    renderTryIt(makeAgent({ status: 'published', version: 'v5' }));
    expect(screen.getByText(/live draft/)).toBeInTheDocument();
    // The real published version is named twice in the real banner text
    // (once for "what you're testing differs from", once for "what
    // production keeps running") - both should show the real v5.
    expect(screen.getAllByText('v5').length).toBeGreaterThanOrEqual(2);
  });

  it('the two banners are mutually exclusive - never both shown at once', () => {
    renderTryIt(makeAgent({ status: 'draft' }));
    expect(screen.queryByText(/live draft/)).not.toBeInTheDocument();
    cleanup();
    renderTryIt(makeAgent({ status: 'published', version: 'v2' }));
    expect(screen.queryByText(/still/)).not.toBeInTheDocument();
  });
});
