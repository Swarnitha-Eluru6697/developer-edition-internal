/**
 * The Conversation Opener needs a message as well as a switch.
 *
 * The bug: the switch wrote `{enabled: true}` with no `text`, and the
 * runner seeds the greeting only when BOTH are set — so the feature read
 * as on and did nothing. The message lives in a sub-modal reached by
 * clicking the row, which the switch's own stopPropagation prevented, so
 * flipping the switch was a dead end with no feedback. Verified against
 * live data: agent "Acme bot FAQ" was stored as
 * `conversation_opener: {"enabled": true}` — no text.
 *
 * A second, independent bug: the runner seeded the opener in a useState
 * initializer, which runs once on mount, so enabling it while a runner
 * was already open did nothing until a remount.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { Agent } from '../types';

vi.mock('../api', () => ({
  api: { getChatHistory: vi.fn(), clearChatHistory: vi.fn(), runAgent: vi.fn(), streamAgentRun: vi.fn(), setAgentFeatures: vi.fn(), uploadFile: vi.fn() },
}));
import { api } from '../api';
vi.mock('../context/AppContext', () => ({ useApp: () => ({ toast: vi.fn() }) }));

import { ChatRunner } from '../components/RunnerUI';

function chatAgent(opener: unknown): Agent {
  return {
    id: 'c1', name: 'Acme bot FAQ', app_type: 'chatbot', status: 'published',
    input_variables: [], features: { conversation_opener: opener },
    flow: { nodes: [{ id: 'n1', type: 'trigger', config: { fields: [] } }], edges: [] },
  } as unknown as Agent;
}

const props = { appType: 'chatbot', busy: false, setBusy: vi.fn(), invokingUser: 'user:padmin' };

describe('conversation opener in the runner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
  });

  it('shows the greeting when enabled with text', async () => {
    render(<ChatRunner agent={chatAgent({ enabled: true, text: 'Hi! How can I help?' })} {...props} />);
    expect(await screen.findByText('Hi! How can I help?')).toBeTruthy();
  });

  it('shows nothing when enabled with NO text — the state that looked broken', () => {
    const { container } = render(<ChatRunner agent={chatAgent({ enabled: true })} {...props} />);
    // No greeting bubble, and no empty bubble either.
    expect(container.querySelectorAll('.chat-bubble.assistant').length).toBe(0);
  });

  it('shows nothing when disabled even if text exists', () => {
    const { container } = render(
      <ChatRunner agent={chatAgent({ enabled: false, text: 'Should not appear' })} {...props} />);
    expect(screen.queryByText('Should not appear')).toBeNull();
    expect(container.querySelectorAll('.chat-bubble.assistant').length).toBe(0);
  });

  it('seeds the greeting when the feature is enabled after mount', async () => {
    // The regression: a useState initializer runs once, so this arrived
    // too late and the greeting never appeared until a remount.
    const { rerender } = render(<ChatRunner agent={chatAgent({ enabled: false })} {...props} />);
    expect(screen.queryByText('Welcome aboard!')).toBeNull();

    rerender(<ChatRunner agent={chatAgent({ enabled: true, text: 'Welcome aboard!' })} {...props} />);
    await waitFor(() => expect(screen.getByText('Welcome aboard!')).toBeTruthy());
  });

  it('does not inject a greeting into a conversation already in progress', async () => {
    const { rerender, container } = render(
      <ChatRunner agent={chatAgent({ enabled: true, text: 'First greeting' })} {...props} />);
    await screen.findByText('First greeting');

    // Editing the text must not prepend a second greeting to a chat that
    // has already started.
    rerender(<ChatRunner agent={chatAgent({ enabled: true, text: 'Edited greeting' })} {...props} />);
    await waitFor(() =>
      expect(container.querySelectorAll('.chat-bubble.assistant').length).toBe(1));
    expect(screen.getByText('First greeting')).toBeTruthy();
  });
});
