import { describe, it, expect, vi, afterEach } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { FormRunner } from '../components/RunnerUI';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a9', name: 'Sentiment Analysis', type: 'App', subtype: 'Workflow', plane: 'App',
    owner: 'you', status: 'published', version: 'v1', health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'fixed', app_type: 'workflow', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} },
        { id: 'n2', type: 'gov', x: 0, y: 0, sub: '', config: {} },
        { id: 'n3', type: 'respond', x: 0, y: 0, sub: '', config: {} },
      ],
      edges: [],
    },
    ...overrides,
  } as Agent;
}

function renderRunner(agent = makeAgent()) {
  return render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={() => {}} />);
}

/** A real stateful busy/setBusy wrapper — FormRunner treats these as
 * lifted state (the real app's parent page owns them), so a test that
 * needs to observe the genuine intermediate "Stop Run" state (not just
 * the before/after) needs a real re-rendering owner, not a static prop
 * and a no-op setter. */
function StatefulRunner({ agent = makeAgent() }: { agent?: Agent }) {
  const [busy, setBusy] = useState(false);
  return <FormRunner agent={agent} appType="workflow" busy={busy} setBusy={setBusy} />;
}

describe('FormRunner — real live Workflow Process panel for workflow apps', () => {
  it('shows real node rows appearing one at a time as node_start events actually arrive, not all at once', async () => {
    let sendEvent: ((e: any) => void) | null = null;
    vi.mocked(api.streamAgentRun).mockImplementation((_id, _input, _user, _inputs, onEvent) => {
      sendEvent = onEvent;
      return new Promise(() => {}); // never resolves — we drive events manually
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => expect(sendEvent).toBeTruthy());
    expect(screen.queryByText('TRIGGER')).not.toBeInTheDocument();

    sendEvent!({ type: 'node_start', node_id: 'n1', node_type: 'trigger' });
    await waitFor(() => expect(screen.getByText('TRIGGER')).toBeInTheDocument());
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.queryByText('UCS CHECK')).not.toBeInTheDocument();

    sendEvent!({ type: 'node_done', node_id: 'n1', node: 'trigger', status: 'ok' });
    sendEvent!({ type: 'node_start', node_id: 'n2', node_type: 'gov' });
    await waitFor(() => expect(screen.getByText('UCS CHECK')).toBeInTheDocument());
  });

  it('a real node failure shows a real error indicator on that specific row', async () => {
    let sendEvent: ((e: any) => void) | null = null;
    vi.mocked(api.streamAgentRun).mockImplementation((_id, _input, _user, _inputs, onEvent) => {
      sendEvent = onEvent;
      return new Promise(() => {});
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => expect(sendEvent).toBeTruthy());

    sendEvent!({ type: 'node_start', node_id: 'n2', node_type: 'gov' });
    sendEvent!({ type: 'node_done', node_id: 'n2', node: 'gov', status: 'ucs_denied' });
    await waitFor(() => expect(screen.getByText('UCS CHECK')).toBeInTheDocument());
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
  });

  it('completing a run shows the real JSON Output panel with the real structured output', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'node_start', node_id: 'n1', node_type: 'trigger' });
      onEvent({ type: 'node_done', node_id: 'n1', node: 'trigger', status: 'ok' });
      onEvent({ type: 'complete', status: 'success', output: null, structured_output: { sentiment: 'positive', score: '0.9' }, fail_class: null });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'great day' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => expect(screen.getByText('JSON Output')).toBeInTheDocument());
    expect(screen.getByText(/"sentiment": "positive"/)).toBeInTheDocument();
  });

  it('Stop Run genuinely aborts the real in-flight stream request', async () => {
    const abortedSignals: AbortSignal[] = [];
    vi.mocked(api.streamAgentRun).mockImplementation((_id, _input, _user, _inputs, _onEvent, signal) => {
      abortedSignals.push(signal);
      return new Promise(() => {});
    });
    render(<StatefulRunner />);
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => expect(screen.getByText('Stop Run')).toBeInTheDocument());
    expect(abortedSignals[0].aborted).toBe(false);
    fireEvent.click(screen.getByText('Stop Run'));
    expect(abortedSignals[0].aborted).toBe(true);
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
  });

  it('a real backend "cancelled" status is reflected honestly, not shown as a fake success', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'node_start', node_id: 'n1', node_type: 'trigger' });
      onEvent({ type: 'node_done', node_id: 'n1', node: 'trigger', status: 'ok' });
      onEvent({ type: 'cancelled' });
      onEvent({ type: 'complete', status: 'cancelled', output: null, fail_class: null });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => expect(screen.getByText('Stopped')).toBeInTheDocument());
    expect(screen.queryByText('JSON Output')).not.toBeInTheDocument();
  });
});
