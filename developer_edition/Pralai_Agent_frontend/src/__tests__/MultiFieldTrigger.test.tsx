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
    owner: 'you', status: 'published', version: 'v3', health_score: null, health_status: '—',
    model: 'Claude Haiku 4.5', mode: 'fixed', app_type: 'workflow', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {},
    flow: { nodes: [{ id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} }], edges: [] },
    ...overrides,
  } as Agent;
}

function makeTriggerNode(fields: any[]): FlowNode {
  return { id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: { trigger_type: 'user_input', fields } } as FlowNode;
}

describe('AddInputFieldModal — real multi-field trigger input config', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
  });

  function renderPanel(node: FlowNode, agent = makeAgent()) {
    return render(
      <MemoryRouter>
        <AppProvider><NodeConfigPanel agent={agent} node={node} onAgentUpdate={() => {}} onDelete={() => {}} /></AppProvider>
      </MemoryRouter>
    );
  }

  it('starts with an honest empty state and no input fields', async () => {
    renderPanel(makeTriggerNode([]));
    await waitFor(() => expect(screen.getByText('INPUT FIELD')).toBeInTheDocument());
    expect(screen.getByText(/No input fields yet/)).toBeInTheDocument();
  });

  it('clicking + opens the real Add Input Field modal with all 8 real field types', async () => {
    renderPanel(makeTriggerNode([]));
    fireEvent.click(screen.getByTitle('Add input field'));
    await waitFor(() => expect(screen.getByText('Add Input Field')).toBeInTheDocument());
    fireEvent.click(screen.getAllByText('Short Text')[0]);
    for (const label of ['Paragraph', 'Select', 'Number', 'Checkbox', 'Single File', 'File List', 'JSON Code']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText('Short Text').length).toBeGreaterThanOrEqual(1);
  });

  it('adding a real field persists via a real PATCH call and shows in the real field list', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent({ flow: { nodes: [makeTriggerNode([{ id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false }])], edges: [] } }));
    renderPanel(makeTriggerNode([]));
    fireEvent.click(screen.getByTitle('Add input field'));
    await waitFor(() => expect(screen.getByText('Add Input Field')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('e.g. topic'), { target: { value: 'topic' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      const call = vi.mocked(api.updateNodeConfig).mock.calls[0];
      expect(call[0]).toBe('a6');
      expect(call[2].fields[0]).toMatchObject({ name: 'topic', type: 'short_text', required: true });
    });
  });

  it('choosing Number as the field type persists the real selected type', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
    renderPanel(makeTriggerNode([]));
    fireEvent.click(screen.getByTitle('Add input field'));
    await waitFor(() => expect(screen.getByText('Add Input Field')).toBeInTheDocument());
    fireEvent.click(screen.getAllByText('Short Text')[0]); // opens the type menu (current selection)
    fireEvent.click(screen.getByText('Number'));
    fireEvent.change(screen.getByPlaceholderText('e.g. topic'), { target: { value: 'count' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      const call = vi.mocked(api.updateNodeConfig).mock.calls[0];
      expect(call[2].fields[0]).toMatchObject({ name: 'count', type: 'number' });
    });
  });

  it('an existing field in the real list shows its real name and required badge', async () => {
    renderPanel(makeTriggerNode([{ id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false }]));
    await waitFor(() => {
      expect(screen.getByText('topic')).toBeInTheDocument();
      expect(screen.getByText('required')).toBeInTheDocument();
    });
  });

  it('removing a real field via its own remove button persists the removal', async () => {
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
    renderPanel(makeTriggerNode([{ id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false }]));
    await waitFor(() => expect(screen.getByText('topic')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Remove'));
    await waitFor(() => {
      const call = vi.mocked(api.updateNodeConfig).mock.calls[0];
      expect(call[2].fields).toEqual([]);
    });
  });
});

describe('FormRunner — real dynamic multi-field form, not a single textarea, when real fields are configured', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('with no configured fields, falls back to the original single-input behavior', () => {
    render(<FormRunner agent={makeAgent()} appType="workflow" busy={false} setBusy={() => {}} />);
    expect(screen.getByPlaceholderText('Provide the input for this workflow…')).toBeInTheDocument();
  });

  it('with real configured fields, renders one real typed input per field instead of a single textarea', () => {
    const agent = makeAgent({ flow: { nodes: [makeTriggerNode([
      { id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false },
      { id: 'f2', name: 'priority', type: 'number', required: false, hidden_prefilled: false },
    ])], edges: [] } });
    render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={() => {}} />);
    expect(screen.queryByPlaceholderText('Provide the input for this workflow…')).not.toBeInTheDocument();
    expect(screen.getByText('topic')).toBeInTheDocument();
    expect(screen.getByText('priority')).toBeInTheDocument();
  });

  it('hidden & pre-filled fields are real invisible to the end user, not shown as an input', () => {
    const agent = makeAgent({ flow: { nodes: [makeTriggerNode([
      { id: 'f1', name: 'visible_field', type: 'short_text', required: true, hidden_prefilled: false },
      { id: 'f2', name: 'secret_field', type: 'short_text', required: false, hidden_prefilled: true, default: 'preset' },
    ])], edges: [] } });
    render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={() => {}} />);
    expect(screen.getByText('visible_field')).toBeInTheDocument();
    expect(screen.queryByText('secret_field')).not.toBeInTheDocument();
  });

  it('Run is genuinely disabled until all real required fields are filled', () => {
    const agent = makeAgent({ flow: { nodes: [makeTriggerNode([{ id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false }])], edges: [] } });
    render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={() => {}} />);
    expect(screen.getByText('Execute').closest('button')).toBeDisabled();
  });

  it('submitting a real multi-field form sends real structured inputs, including the real hidden pre-filled default', async () => {
    vi.mocked(api.streamAgentRun).mockImplementation(async (_id, _input, _user, inputs, onEvent) => {
      onEvent({ type: 'complete', status: 'success', output: 'ok', structured_output: null, fail_class: null });
    });
    const agent = makeAgent({ flow: { nodes: [makeTriggerNode([
      { id: 'f1', name: 'topic', type: 'short_text', required: true, hidden_prefilled: false },
      { id: 'f2', name: 'source', type: 'short_text', required: false, hidden_prefilled: true, default: 'web' },
    ])], edges: [] } });
    render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={() => {}} />);

    const inputs = document.querySelectorAll('.form-runner input:not([type=file])');
    fireEvent.change(inputs[0], { target: { value: 'AI safety' } });
    fireEvent.click(screen.getByText('Execute'));

    await waitFor(() => {
      expect(api.streamAgentRun).toHaveBeenCalledWith(
        'a6', expect.any(String), 'user:end-user', { topic: 'AI safety', source: 'web' },
        expect.any(Function), expect.any(Object), undefined,
      );
    });
  });
});
