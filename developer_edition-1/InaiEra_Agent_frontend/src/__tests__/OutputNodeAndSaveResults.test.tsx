import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import NodeConfigPanel from '../components/NodeConfigPanel';
import { FormRunner } from '../components/RunnerUI';
import { api } from '../api';
import type { Agent, FlowNode } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a6', name: 'Invoice Approval Flow', type: 'App', subtype: 'Task-Automation', plane: 'App',
    owner: 'you', status: 'published', version: 'v1', health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'fixed', app_type: 'workflow', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function makeOutputNode(overrides?: Partial<FlowNode>): FlowNode {
  return { id: 'n4', type: 'output', x: 0, y: 0, sub: '', config: { variables: [] }, ...overrides } as FlowNode;
}

describe('NodeConfigPanel — real Output node config, matching the reference OUTPUT VARIABLE list', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
  });

  function renderPanel(node: FlowNode, agent = makeAgent()) {
    return render(
      <MemoryRouter>
        <AppProvider>
          <NodeConfigPanel agent={agent} node={node} onAgentUpdate={() => {}} onDelete={() => {}} />
        </AppProvider>
      </MemoryRouter>
    );
  }

  it('starts with no variables and an honest empty state', () => {
    renderPanel(makeOutputNode());
    expect(screen.getByText('No output variables yet — add one above.')).toBeInTheDocument();
  });

  it('adding a variable shows a real name field and a real {x} Set variable expression field', () => {
    renderPanel(makeOutputNode());
    fireEvent.click(screen.getByTitle('Add output variable'));
    expect(screen.getByPlaceholderText('Variable name')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('{x} Set variable')).toBeInTheDocument();
    expect(screen.queryByText('No output variables yet — add one above.')).not.toBeInTheDocument();
  });

  it('editing and blurring persists the real variable name and expression via a real PATCH call', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
    renderPanel(makeOutputNode({ config: { variables: [{ name: '', expr: '' }] } }));

    fireEvent.change(screen.getByPlaceholderText('Variable name'), { target: { value: 'summary' } });
    fireEvent.blur(screen.getByPlaceholderText('Variable name'));
    fireEvent.change(screen.getByPlaceholderText('{x} Set variable'), { target: { value: '{{template_output}}' } });
    fireEvent.blur(screen.getByPlaceholderText('{x} Set variable'));

    await waitFor(() => {
      const calls = vi.mocked(api.updateNodeConfig).mock.calls;
      const lastCall = calls[calls.length - 1];
      expect(lastCall?.[2]).toEqual({ variables: [{ name: 'summary', expr: '{{template_output}}' }] });
    });
  });

  it('removing a variable row genuinely removes it, not just visually', () => {
    renderPanel(makeOutputNode({ config: { variables: [{ name: 'a', expr: '{{x}}' }, { name: 'b', expr: '{{y}}' }] } }));
    const removeButtons = screen.getAllByTitle('Remove');
    fireEvent.click(removeButtons[0]);
    expect(screen.queryByDisplayValue('a')).not.toBeInTheDocument();
    expect(screen.getByDisplayValue('b')).toBeInTheDocument();
  });

  it('a real delete-node button is present, matching every other non-locked node type', () => {
    renderPanel(makeOutputNode());
    expect(screen.getByRole('button', { name: 'Delete node' })).toBeInTheDocument();
  });
});

describe('FormRunner — real structured output display, Copy, and Save (self-contained, no AppContext dependency)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  function renderRunner(appType: 'workflow' | 'text_generator' = 'workflow') {
    return render(<FormRunner agent={makeAgent({ app_type: appType })} appType={appType} busy={false} setBusy={() => {}} />);
  }

  it('renders standalone, with NO AppProvider wrapper needed — matches the real standalone /app/ page', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: 'plain text result', structured_output: null, fail_class: null });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'run it' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => expect(screen.getByText('plain text result')).toBeInTheDocument());
  });

  it('a real structured_output result shows genuine named fields, not a single text blob', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: 'fallback text', structured_output: { status: 'approved', vendor: 'Acme Corp' }, fail_class: null });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'onboard Acme' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => {
      expect(screen.getByText('approved')).toBeInTheDocument();
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      expect(screen.getByText('status')).toBeInTheDocument();
      expect(screen.getByText('vendor')).toBeInTheDocument();
    });
    expect(screen.queryByText('fallback text')).not.toBeInTheDocument();
  });

  it('Copy writes the real structured output (not the raw JSON, formatted as key: value) to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: null, structured_output: { status: 'approved' }, fail_class: null });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'x' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => expect(screen.getAllByText('Copy').length).toBeGreaterThan(0));
    // Real structured-output Copy — formats as "key: value", distinct from
    // the JSON Output side panel's own real Copy (raw JSON) — click the
    // first one specifically, since both are now real, separate features.
    fireEvent.click(screen.getAllByText('Copy')[0]);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('status: approved'));
  });

  it('Save genuinely calls the real save endpoint with the real input and output, then disables further saving', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: 'the real output', structured_output: null, fail_class: null });
    });
    vi.mocked(api.saveResult).mockResolvedValue({ id: 1, agent_id: 'a6', invoking_user: 'user:end-user', input: 'onboard Acme', output: 'the real output', structured_output: null, created_at: '2026-01-01' });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'onboard Acme' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.saveResult).toHaveBeenCalledWith('a6', expect.objectContaining({ input: 'onboard Acme', output: 'the real output' }));
      expect(screen.getByText('Saved')).toBeInTheDocument();
      expect(screen.getByText('Saved').closest('button')).toBeDisabled();
    });
  });

  it('Save also downloads the result as a JSON file named after the agent', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: 'the real output', structured_output: null, fail_class: null, run_id: 'r-1' } as never);
    });
    vi.mocked(api.saveResult).mockResolvedValue({ id: 1 } as never);
    let saved: { name: string; blob: Blob } | null = null;
    const created: Blob[] = [];
    const origCreate = URL.createObjectURL, origRevoke = URL.revokeObjectURL;
    URL.createObjectURL = ((b: Blob) => { created.push(b); return 'blob:test'; }) as never;
    URL.revokeObjectURL = (() => {}) as never;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved = { name: this.download, blob: created[created.length - 1] };
    });
    try {
      renderRunner();
      fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'onboard Acme' } });
      fireEvent.click(screen.getByText('Execute'));
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument());
      fireEvent.click(screen.getByText('Save'));
      await waitFor(() => expect(saved).not.toBeNull());
      expect(saved!.name).toMatch(/^Invoice_Approval_Flow_result_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.json$/);
      const body = JSON.parse(await saved!.blob.text());
      expect(body).toMatchObject({ agent: { id: 'a6', name: 'Invoice Approval Flow' }, run_id: 'r-1', input: 'onboard Acme', output: 'the real output' });
    } finally {
      click.mockRestore(); URL.createObjectURL = origCreate; URL.revokeObjectURL = origRevoke;
    }
  });

  it('a real failed run shows an honest error, not a fake success, and no structured output UI', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, _inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'failed', output: null, structured_output: null, fail_class: 'harness' });
    });
    renderRunner();
    fireEvent.change(screen.getByPlaceholderText('Provide the input for this workflow…'), { target: { value: 'x' } });
    fireEvent.click(screen.getByText('Execute'));
    await waitFor(() => {
      expect(screen.getByText(/Failed \(failed\)/)).toBeInTheDocument();
    });
    expect(screen.queryByText('Copy')).not.toBeInTheDocument();
    expect(screen.queryByText('Save')).not.toBeInTheDocument();
  });
});
