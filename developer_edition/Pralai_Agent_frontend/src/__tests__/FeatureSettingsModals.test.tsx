import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import TryItModal from '../components/TryItModal';
import { AppProvider } from '../context/AppContext';
import { api } from '../api';
import type { Agent } from '../types';
import { pickOption } from './pickOption';

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

function renderTryIt(agent: Agent) {
  return render(<AppProvider><TryItModal agent={agent} onClose={() => {}} /></AppProvider>);
}

async function openFeatures() {
  await waitFor(() => expect(screen.getByText('Features')).toBeInTheDocument());
  fireEvent.click(screen.getByText('Features'));
  await waitFor(() => expect(screen.getByText('Content moderation')).toBeInTheDocument());
}

describe('Conversation Opener modal — real opener message and opening questions', () => {
  beforeEach(() => {
    vi.mocked(api.whoami)?.mockResolvedValue?.({ verified_identity: null, gateway_trust_enabled: false, reason: 'no headers in this test' });
    vi.clearAllMocks();
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
  });

  it('opens as a dedicated modal when clicking the Conversation Opener row', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getByText('Conversation Opener'));
    await waitFor(() => expect(screen.getByPlaceholderText(/Hi! I can help analyze/)).toBeInTheDocument());
  });

  it('adding an opening question and saving persists both the opener text and the real question list', async () => {
    const saved = makeAgent({ features: { conversation_opener: { enabled: true, text: 'Hi there', opening_questions: ['What can you do?'] } } });
    vi.mocked(api.setAgentFeatures).mockResolvedValue(saved);
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getByText('Conversation Opener'));
    await waitFor(() => expect(screen.getByPlaceholderText(/Hi! I can help analyze/)).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(/Hi! I can help analyze/), { target: { value: 'Hi there' } });
    fireEvent.change(screen.getByPlaceholderText('Add a starter question…'), { target: { value: 'What can you do?' } });
    fireEvent.click(screen.getByText('Add'));
    await waitFor(() => expect(screen.getByText('What can you do?')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', expect.objectContaining({
        conversation_opener: { enabled: true, text: 'Hi there', opening_questions: ['What can you do?'] },
      }));
    });
  });

  it('a real configured opening question shows as a clickable starter, and clicking it sends that exact question', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success', output: 'Sure, here is what I can do.', spans: [], fail_class: null,
    });
    const agent = makeAgent({ features: { conversation_opener: { enabled: true, text: 'Hi there', opening_questions: ['What can you do?'] } } });
    renderTryIt(agent);
    await waitFor(() => expect(screen.getByText('What can you do?')).toBeInTheDocument());
    fireEvent.click(screen.getByText('What can you do?'));

    await waitFor(() => {
      expect(api.runAgent).toHaveBeenCalledWith('a1', 'What can you do?', 'user:developer', undefined, true);
    });
  });

  it('opening questions stop appearing once the real conversation has moved past the opener', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success', output: 'Real reply', spans: [], fail_class: null,
    });
    const agent = makeAgent({ features: { conversation_opener: { enabled: true, text: 'Hi there', opening_questions: ['What can you do?'] } } });
    renderTryIt(agent);
    await waitFor(() => expect(screen.getByText('What can you do?')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'a real question' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => expect(screen.getByText('Real reply')).toBeInTheDocument());
    expect(screen.queryByText('What can you do?')).not.toBeInTheDocument();
  });
});

describe('Follow-up settings modal — real model selector and default/custom prompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
  });

  it('defaults to the system default prompt, showing its real preview text', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getByText('Follow-up'));
    await waitFor(() => expect(screen.getByText('System default prompt')).toBeInTheDocument());
    expect(screen.getByText(/Suggest exactly 3 short, natural follow-up/)).toBeInTheDocument();
  });

  it('switching to Custom prompt reveals a real textarea, and Save is disabled until it has content', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getByText('Follow-up'));
    await waitFor(() => expect(screen.getByText('Custom prompt')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Custom prompt'));

    await waitFor(() => expect(screen.getByPlaceholderText('Enter a custom prompt')).toBeInTheDocument());
    expect(screen.getByText('Save')).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('Enter a custom prompt'), { target: { value: 'My real custom prompt {{input}}' } });
    expect(screen.getByText('Save')).not.toBeDisabled();
  });

  it('saving with a custom prompt and model persists both to the real agent features', async () => {
    vi.mocked(api.setAgentFeatures).mockResolvedValue(makeAgent());
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getByText('Follow-up'));
    await waitFor(() => expect(screen.getByText('Custom prompt')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Custom prompt'));
    fireEvent.change(screen.getByPlaceholderText('Enter a custom prompt'), { target: { value: 'custom text' } });
    pickOption(screen.getByRole('button', { name: 'Model' }), 'Claude Opus 4.8');
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', expect.objectContaining({
        follow_up: { enabled: true, prompt_mode: 'custom', custom_prompt: 'custom text', model: 'Claude Opus 4.8' },
      }));
    });
  });
});

describe('Content moderation settings modal — real provider selection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
  });

  it('defaults to Keywords provider, showing a real keywords textarea', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getAllByText('Content moderation')[0]);
    await waitFor(() => expect(screen.getByPlaceholderText('One per line, separated by line breaks')).toBeInTheDocument());
  });

  it('switching to Custom Endpoint reveals a real URL field, not a keywords field', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getAllByText('Content moderation')[0]);
    await waitFor(() => expect(screen.getByText('Custom Endpoint')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Custom Endpoint'));

    await waitFor(() => expect(screen.getByPlaceholderText('https://your-service.example.com/moderate')).toBeInTheDocument());
    expect(screen.queryByPlaceholderText('One per line, separated by line breaks')).not.toBeInTheDocument();
  });

  it('disabling both INPUT and OUTPUT moderation disables Save with a real validation message', async () => {
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getAllByText('Content moderation')[0]);
    await waitFor(() => expect(screen.getByText('Moderate INPUT Content')).toBeInTheDocument());

    const inputToggle = screen.getByText('Moderate INPUT Content').closest('.mod-direction-row')!.querySelector('input')!;
    const outputToggle = screen.getByText('Moderate OUTPUT Content').closest('.mod-direction-row')!.querySelector('input')!;
    fireEvent.click(inputToggle);
    fireEvent.click(outputToggle);

    await waitFor(() => {
      expect(screen.getByText(/enable at least one/)).toBeInTheDocument();
    });
    expect(screen.getByText('Save')).toBeDisabled();
  });

  it('saving real provider + preset replies persists the full real config', async () => {
    vi.mocked(api.setAgentFeatures).mockResolvedValue(makeAgent());
    renderTryIt(makeAgent());
    await openFeatures();
    fireEvent.click(screen.getAllByText('Content moderation')[0]);
    await waitFor(() => expect(screen.getByPlaceholderText('One per line, separated by line breaks')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('One per line, separated by line breaks'), { target: { value: 'badword1\nbadword2' } });
    const inputPresetArea = screen.getByText('Moderate INPUT Content').closest('.mod-direction-row')!.querySelector('textarea')!;
    fireEvent.change(inputPresetArea, { target: { value: 'Sorry, cannot help with that.' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', expect.objectContaining({
        moderation: expect.objectContaining({
          enabled: true, provider: 'keywords', keywords: ['badword1', 'badword2'],
          input_preset_reply: 'Sorry, cannot help with that.',
        }),
      }));
    });
  });
});
