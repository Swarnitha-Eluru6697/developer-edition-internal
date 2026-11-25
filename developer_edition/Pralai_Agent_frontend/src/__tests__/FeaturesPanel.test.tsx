import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import TryItModal from '../components/TryItModal';
import { AppProvider } from '../context/AppContext';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

// FeaturesPanel re-reads the agent when it opens, so its payload is
// never built from a stale snapshot (that staleness is what made
// enabled features appear not to save). These tests supply the agent
// directly and assert against it, so the refresh is made to fail: the
// panel catches that and keeps the snapshot it was given, which is
// exactly what they assume.
beforeEach(() => {
  vi.mocked(api.getAgent).mockRejectedValue(new Error('no refresh in this test'));
});

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

describe('Features panel — real conversation opener, follow-up, citations, moderation', () => {
  beforeEach(() => {
    vi.mocked(api.whoami)?.mockResolvedValue?.({ verified_identity: null, gateway_trust_enabled: false, reason: 'no headers in this test' });
    vi.clearAllMocks();
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
  });

  it('opens from Try It via a real Features button', async () => {
    renderTryIt(makeAgent());
    await waitFor(() => expect(screen.getByText('Features')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => {
      expect(screen.getByText('Conversation Opener')).toBeInTheDocument();
      expect(screen.getByText('Content moderation')).toBeInTheDocument();
    });
  });

  it('deferred features (TTS/STT/Annotation Reply) are shown honestly as unavailable, not as working toggles', async () => {
    renderTryIt(makeAgent());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => expect(screen.getByText('Text to Speech')).toBeInTheDocument());
    expect(screen.getAllByText('Coming soon').length).toBe(3);
  });

  it('enabling Conversation Opener calls the real API and persists the toggle', async () => {
    const saved = makeAgent({ features: { conversation_opener: { enabled: true, text: '' } } });
    vi.mocked(api.setAgentFeatures).mockResolvedValue(saved);
    renderTryIt(makeAgent());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => expect(screen.getByText('Conversation Opener')).toBeInTheDocument());

    const openerRow = screen.getByText('Conversation Opener').closest('.feature-row')!;
    fireEvent.click(openerRow.querySelector('input[type="checkbox"]')!);

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', { conversation_opener: { enabled: true } });
    });
  });

  it('enabling Chat History calls the real API with the real nested shape, and is shown as inherited by any node without its own memory setting', async () => {
    const saved = makeAgent({ features: { chat_history: { enabled: true } } });
    vi.mocked(api.setAgentFeatures).mockResolvedValue(saved);
    renderTryIt(makeAgent());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => expect(screen.getByText('Chat History')).toBeInTheDocument());
    expect(screen.getByText(/Any LLM node without its own explicit memory setting inherits this/)).toBeInTheDocument();

    const row = screen.getByText('Chat History').closest('.feature-row')!;
    fireEvent.click(row.querySelector('input[type="checkbox"]')!);

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', { chat_history: { enabled: true } });
    });
  });

  it('enabling Require authenticated user calls the real API with a plain boolean, not the nested {enabled} shape other features use', async () => {
    const saved = makeAgent({ features: { require_authenticated_user: true } });
    vi.mocked(api.setAgentFeatures).mockResolvedValue(saved);
    renderTryIt(makeAgent());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => expect(screen.getByText('Require authenticated user')).toBeInTheDocument());

    const row = screen.getByText('Require authenticated user').closest('.feature-row')!;
    fireEvent.click(row.querySelector('input[type="checkbox"]')!);

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', { require_authenticated_user: true });
    });
  });

  it('Require authenticated user is honest that this platform does not do OIDC login itself, when off', async () => {
    renderTryIt(makeAgent());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => {
      expect(screen.getByText(/an upstream gateway verifies the person/)).toBeInTheDocument();
    });
  });

  it('Require authenticated user shows real, distinct wording once enabled', async () => {
    renderTryIt(makeAgent({ features: { require_authenticated_user: true } }));
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => {
      expect(screen.getByText(/A self-reported invoking_user is rejected/)).toBeInTheDocument();
    });
  });

  it('a real configured conversation opener shows as the first chat message, before any user input', async () => {
    const agent = makeAgent({ features: { conversation_opener: { enabled: true, text: 'Hi! Ask me about your data.' } } });
    renderTryIt(agent);
    await waitFor(() => {
      expect(screen.getByText('Hi! Ask me about your data.')).toBeInTheDocument();
    });
  });

  it('real follow-up suggestions from the backend render as clickable chips, and clicking one sends it as the next message', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'Revenue grew 12% this quarter.', spans: [], fail_class: null,
      follow_ups: ['What drove the growth?', 'How does this compare to last year?'],
    });
    renderTryIt(makeAgent());
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'how did revenue do' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => expect(screen.getByText('What drove the growth?')).toBeInTheDocument());
    fireEvent.click(screen.getByText('What drove the growth?'));

    await waitFor(() => {
      expect(api.runAgent).toHaveBeenCalledWith('a1', 'What drove the growth?', 'user:developer', undefined, true);
    });
  });

  it('real citations from the backend are shown alongside the reply, only when present', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'You can reset it in Settings.', spans: [], fail_class: null,
      citations: ['To reset your password, go to Settings > Security > Reset Password.'],
    });
    renderTryIt(makeAgent());
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'how do I reset my password' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => {
      expect(screen.getByText('Sources')).toBeInTheDocument();
      expect(screen.getByText('To reset your password, go to Settings > Security > Reset Password.')).toBeInTheDocument();
    });
  });

  it('a real "blocked" status from server-side moderation shows an honest message, not a fake reply', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'blocked (moderation, flow never ran)', status: 'blocked',
      output: null, spans: [], fail_class: 'moderation',
    });
    renderTryIt(makeAgent());
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'a blocked message' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => {
      expect(screen.getByText(/blocked by this app's content moderation/)).toBeInTheDocument();
    });
  });

  it('the chat has no "Manage features →" link; the header Features button opens the panel', async () => {
    renderTryIt(makeAgent());
    await waitFor(() => expect(screen.getByText('Features')).toBeInTheDocument());
    expect(screen.queryByText('Manage features →')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => {
      expect(screen.getByText('Content moderation')).toBeInTheDocument();
    });
  });

  it('a Data-type agent (form UI, no chat) does not show a Features button at all — features only make sense for chat', async () => {
    renderTryIt(makeAgent({ type: 'Data', app_type: undefined }));
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    expect(screen.queryByText('Features')).not.toBeInTheDocument();
  });
});
