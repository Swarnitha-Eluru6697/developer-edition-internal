/**
 * A conversation survives closing and reopening the web app — but only
 * when the Chat History feature is on.
 *
 * The rows always existed and were always read: harness._load_chat_history
 * hands prior turns to the model, so the agent genuinely remembers.
 * Nothing exposed them to a client, so reopening showed an empty
 * transcript while the agent carried on as though the earlier turns had
 * happened — it would answer a follow-up to a question no longer on
 * screen. Losing the display but keeping the model's memory is worse
 * than losing both.
 *
 * Replay follows features.chat_history because replaying a transcript
 * IS that feature. With the switch off nothing is stored for later
 * turns anyway, so showing an earlier conversation would imply a memory
 * the agent does not have.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { Agent } from '../types';

vi.mock('../api', () => ({
  api: {
    getChatHistory: vi.fn(),
    runAgent: vi.fn(),
    streamAgentRun: vi.fn(),
  },
}));
vi.mock('../context/AppContext', () => ({ useApp: () => ({ toast: vi.fn() }) }));

import { api } from '../api';
import { ChatRunner } from '../components/RunnerUI';

const HISTORY_ON = { chat_history: { enabled: true } };

function chatAgent(features: Record<string, unknown> = HISTORY_ON): Agent {
  return {
    id: 'c1', name: 'Acme bot FAQ', app_type: 'chatbot', status: 'published',
    input_variables: [], features,
    flow: { nodes: [{ id: 'n1', type: 'trigger', config: { fields: [] } }], edges: [] },
  } as unknown as Agent;
}

const props = { appType: 'chatbot', busy: false, setBusy: vi.fn(), invokingUser: 'user:padmin' };

const STORED = [
  { role: 'user' as const, content: 'How do I dispute a charge?', created_at: '2026-09-22T10:00:00' },
  { role: 'assistant' as const, content: 'Open the invoice and choose Dispute.', created_at: '2026-09-22T10:00:05' },
];

describe('chat history replay', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the stored transcript on mount when the feature is on', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue(STORED);
    render(<ChatRunner agent={chatAgent()} {...props} />);
    await waitFor(() => expect(api.getChatHistory).toHaveBeenCalledWith('c1', 'user:padmin'));
    expect(await screen.findByText('How do I dispute a charge?')).toBeTruthy();
    expect(screen.getByText('Open the invoice and choose Dispute.')).toBeTruthy();
  });

  it('does not fetch or show history when the feature is off', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue(STORED);
    render(<ChatRunner agent={chatAgent({ chat_history: { enabled: false } })} {...props} />);
    // Not merely hidden — never requested, so a disabled feature does no
    // work and shows nothing from an earlier conversation.
    await waitFor(() => expect(screen.getByPlaceholderText(/Type a message/i)).toBeTruthy());
    expect(api.getChatHistory).not.toHaveBeenCalled();
    expect(screen.queryByText('How do I dispute a charge?')).toBeNull();
  });

  it('treats an absent chat_history feature as off', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue(STORED);
    render(<ChatRunner agent={chatAgent({})} {...props} />);
    await waitFor(() => expect(screen.getByPlaceholderText(/Type a message/i)).toBeTruthy());
    expect(api.getChatHistory).not.toHaveBeenCalled();
  });

  it('is scoped to the invoking user, not just the agent', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
    render(<ChatRunner agent={chatAgent()} {...props} invokingUser="user:someone-else" />);
    await waitFor(() =>
      expect(api.getChatHistory).toHaveBeenCalledWith('c1', 'user:someone-else'));
  });

  it('does not push the opener in front of a returning conversation', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue(STORED);
    render(<ChatRunner
      agent={chatAgent({ ...HISTORY_ON, conversation_opener: { enabled: true, text: 'Hi! How can I help?' } })}
      {...props} />);
    await screen.findByText('How do I dispute a charge?');
    // Greeting someone mid-conversation would read as the agent having
    // forgotten, which is the opposite of what replay is for.
    expect(screen.queryByText('Hi! How can I help?')).toBeNull();
  });

  it('still shows the opener when there is no stored history', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
    render(<ChatRunner
      agent={chatAgent({ ...HISTORY_ON, conversation_opener: { enabled: true, text: 'Hi! How can I help?' } })}
      {...props} />);
    expect(await screen.findByText('Hi! How can I help?')).toBeTruthy();
  });

  it('shows the opener with history switched off', async () => {
    // The two features are independent: a greeting is not a memory.
    render(<ChatRunner
      agent={chatAgent({ conversation_opener: { enabled: true, text: 'Hi! How can I help?' } })}
      {...props} />);
    expect(await screen.findByText('Hi! How can I help?')).toBeTruthy();
  });

  it('starts empty rather than breaking when history cannot be fetched', async () => {
    vi.mocked(api.getChatHistory).mockRejectedValue(new Error('no such endpoint'));
    const { container } = render(<ChatRunner agent={chatAgent()} {...props} />);
    await waitFor(() => expect(api.getChatHistory).toHaveBeenCalled());
    expect(container.querySelector('.chat-input-row')).toBeTruthy();
  });

  it('offers no history or new-conversation controls', async () => {
    vi.mocked(api.getChatHistory).mockResolvedValue(STORED);
    render(<ChatRunner agent={chatAgent()} {...props} />);
    await screen.findByText('How do I dispute a charge?');
    expect(screen.queryByText('Chat history')).toBeNull();
    expect(screen.queryByText('New conversation')).toBeNull();
  });
});
