import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import AgentRunner from '../pages/AgentRunner';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Support Bot', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'published', version: 'v1', health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderRunner(agentId = 'a1') {
  return render(
    <MemoryRouter initialEntries={[`/app/${agentId}`]}>
      <Routes><Route path="/app/:agentId" element={<AgentRunner />} /></Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  // ChatRunner replays the stored transcript on mount so a
  // conversation survives closing the app. These tests drive their
  // own messages, so it resolves empty.
  vi.mocked(api.getChatHistory).mockResolvedValue([]);
});

describe('AgentRunner — the real end-user page, no builder chrome', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('shows an honest message instead of the runner UI when the agent is not published', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    renderRunner();
    await waitFor(() => {
      expect(screen.getByText(/isn't published yet/i)).toBeInTheDocument();
    });
    expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
  });

  it('shows a real "this app doesn\'t exist" message for an unknown agent, not a crash', async () => {
    vi.mocked(api.getAgent).mockRejectedValue(new Error('404'));
    renderRunner('nope');
    await waitFor(() => {
      expect(screen.getByText(/doesn't exist/i)).toBeInTheDocument();
    });
  });

  it('app_type=chatbot renders the real chat UI, not the form UI', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'chatbot' }));
    renderRunner();
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument();
    });
    expect(screen.queryByText('Execute')).not.toBeInTheDocument();
  });

  it('app_type=workflow renders the real form UI, not chat', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    renderRunner();
    await waitFor(() => {
      expect(screen.getByText('Execute')).toBeInTheDocument();
    });
    expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
  });

  it('app_type=text_generator renders the form UI labeled for generation, not workflow', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'text_generator' }));
    renderRunner();
    await waitFor(() => {
      expect(screen.getByText('Generate')).toBeInTheDocument();
    });
  });

  it('a real Data-type agent (no app_type at all) gets the real form UI, never chat — the exact bug found and fixed: Data agents were defaulting to a chat interface', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ type: 'Data', app_type: undefined, name: 'Revenue Analysis Agent' }));
    renderRunner();
    await waitFor(() => {
      expect(screen.getByText('Execute')).toBeInTheDocument();
    });
    expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
  });

  it('sending a chat message calls the real runAgent API and appends the real reply', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'chatbot' }));
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'Real reply text', spans: [], fail_class: null,
    });
    renderRunner();
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'Hello there' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => {
      expect(api.runAgent).toHaveBeenCalledWith('a1', 'Hello there', 'user:end-user', undefined, undefined);
    });
    await waitFor(() => {
      expect(screen.getByText('Real reply text')).toBeInTheDocument();
    });
    expect(screen.getByText('Hello there')).toBeInTheDocument();
  });

  it('app_type=agent surfaces the real reasoning steps behind a toggle, not shown by default', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'agent' }));
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'Final answer', spans: [], fail_class: null,
      agent_strategy: 'plan_execute',
      agent_steps: [{ phase: 'plan', output: 'Step 1, step 2' }, { phase: 'execute', output: 'Did the steps' }],
    });
    renderRunner();
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'plan something' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => expect(screen.getByText('Final answer')).toBeInTheDocument());
    expect(screen.queryByText('Step 1, step 2')).not.toBeInTheDocument(); // collapsed by default
    fireEvent.click(screen.getByText(/Show reasoning/));
    await waitFor(() => {
      expect(screen.getByText('Step 1, step 2')).toBeInTheDocument();
    });
  });

  it('app_type=chatbot does NOT request or show reasoning steps even if the backend happened to include them', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'chatbot' }));
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'A plain reply', spans: [], fail_class: null,
      agent_strategy: 'react', agent_steps: [{ phase: 'step_1', output: 'irrelevant for a chatbot-type app' }],
    });
    renderRunner();
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => expect(screen.getByText('A plain reply')).toBeInTheDocument());
    expect(screen.queryByText(/Show reasoning/)).not.toBeInTheDocument();
  });

  it('a real failed run shows an honest error, not a fake success', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'denied', output: null, fail_class: 'ucs' });
    });
    renderRunner();
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'test' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => {
      expect(screen.getByText(/Failed \(denied\)/)).toBeInTheDocument();
    });
  });
});

describe('what a failed run tells you', () => {
  // "Failed (failed: orchestration)" names the class of failure and
  // nothing else, so the reason — which the run already had, on the span
  // of the node that failed — could only be found by opening the trace.
  it('repeats the failing node\'s own message', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({
        type: 'complete', status: 'failed', output: null, fail_class: 'orchestration',
        spans: [
          { node: 'trigger', status: 'ok' },
          { node: 'iteration', status: 'error',
            error: 'Iteration input {{items}} is not an array. Got str "[\'lily\']".' },
        ],
      } as never);
    });
    renderRunner();
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'),
      { target: { value: 'test' } });
    fireEvent.click(screen.getByText('Execute'));
    await screen.findByText(/iteration step failed/);
    await screen.findByText(/is not an array/);
  });

  it('still says something useful when no span carries a message', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'failed', output: null, fail_class: 'orchestration' } as never);
    });
    renderRunner();
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'),
      { target: { value: 'test' } });
    fireEvent.click(screen.getByText('Execute'));
    await screen.findByText(/Failed \(failed\)/);
    await screen.findByText(/orchestration/);
  });
});

describe('a workflow that pauses for a person', () => {
  // paused_for_human_input is the HITL node working. The form runner
  // treated anything that was not "success" as a failure, so Showcase 7
  // above its threshold reported "Failed (paused_for_human_input)" and
  // offered no way to answer — the run could only be completed from
  // somewhere else.
  const paused = {
    type: 'complete', status: 'paused_for_human_input', output: null, fail_class: null,
    hitl_request_id: 'hitl-1', hitl_rendered_content: 'Approve this 9000 payment?',
    hitl_fields: [], hitl_actions: [{ id: 'continue', title: 'Continue', style: 'primary' }],
  };

  async function runToPause() {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    vi.mocked(api.runAgent).mockResolvedValue(paused as never);
    vi.mocked(api.streamAgentRun).mockImplementation(async (_i, _n, _u, _f, onEvent) => {
      onEvent(paused as never);
    });
    renderRunner();
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'),
      { target: { value: '9000' } });
    fireEvent.click(screen.getByText('Execute'));
  }

  it('is shown as awaiting approval, not as a failure', async () => {
    await runToPause();
    await screen.findByText('awaiting approval');
    expect(screen.queryByText(/Failed/)).toBeNull();
  });

  it('shows what is being approved', async () => {
    await runToPause();
    await screen.findByText('Approve this 9000 payment?');
  });

  it('offers the action the node declared, and sends the response', async () => {
    vi.mocked(api.respondToHitl).mockResolvedValue(
      { status: 'success', output: 'Approved', fail_class: null } as never);
    await runToPause();
    fireEvent.click(await screen.findByText('Continue'));
    await waitFor(() => expect(api.respondToHitl).toHaveBeenCalledWith(
      'a1', 'hitl-1', 'continue', {}));
  });

  it('says so when the approval step has no message configured', async () => {
    // Showcase 7's node stores its text under `prompt`, but the platform
    // reads `form_content` — so the card came through blank.
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    const blank = { ...paused, hitl_rendered_content: '' };
    vi.mocked(api.streamAgentRun).mockImplementation(async (_i, _n, _u, _f, onEvent) => {
      onEvent(blank as never);
    });
    renderRunner();
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'),
      { target: { value: '9000' } });
    fireEvent.click(screen.getByText('Execute'));
    await screen.findByText(/no message configured/);
  });
});
