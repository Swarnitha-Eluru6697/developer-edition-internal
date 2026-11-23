import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import NodeConfigPanel from '../components/NodeConfigPanel';
import { AppProvider } from '../context/AppContext';
import { api } from '../api';
import type { Agent, FlowNode } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function renderPanel(agent: Agent, node: FlowNode, onAgentUpdate = vi.fn(), onDelete = vi.fn()) {
  return render(
    <AppProvider>
      <NodeConfigPanel agent={agent} node={node} onAgentUpdate={onAgentUpdate} onDelete={onDelete} />
    </AppProvider>
  );
}

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: { nodes: [{ id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} }], edges: [] },
  } as Agent;
}

describe('Trigger node config panel — real trigger-type selector', () => {
  const triggerNode: FlowNode = { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.updateNodeConfig).mockImplementation((agentId, nodeId, config) =>
      Promise.resolve({ ...makeAgent(), flow: { nodes: [{ ...triggerNode, config: { ...triggerNode.config, ...config } }], edges: [] } })
    );
  });

  it('shows all three trigger options with User Input marked as most common', () => {
    renderPanel(makeAgent(), triggerNode);
    expect(screen.getByText('User Input')).toBeInTheDocument();
    expect(screen.getByText('Schedule Trigger')).toBeInTheDocument();
    expect(screen.getByText('Webhook Trigger')).toBeInTheDocument();
    expect(screen.getByText('MOST COMMON')).toBeInTheDocument();
  });

  it('picking Schedule Trigger persists trigger_type via a real API call and shows the cron field', async () => {
    const onAgentUpdate = vi.fn();
    renderPanel(makeAgent(), triggerNode, onAgentUpdate);
    fireEvent.click(screen.getByText('Schedule Trigger'));

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith('a1', 'n1', { trigger_type: 'schedule' }, 1);
    });
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/MON-FRI/i)).toBeInTheDocument();
    });
    // the honest "nothing is actively scheduling" disclosure must be visible, not hidden
    expect(screen.getByText(/nothing is actively scheduling/i)).toBeInTheDocument();
  });

  it('picking Webhook Trigger generates a token client-side and persists it, then shows a real webhook URL', async () => {
    renderPanel(makeAgent(), triggerNode);
    fireEvent.click(screen.getByText('Webhook Trigger'));

    await waitFor(() => {
      expect(api.updateNodeConfig).toHaveBeenCalledWith(
        'a1', 'n1',
        expect.objectContaining({ trigger_type: 'webhook', webhook_token: expect.any(String) }),
        1
      );
    });
    const [, , config] = vi.mocked(api.updateNodeConfig).mock.calls[0];
    await waitFor(() => {
      const urlField = screen.getByDisplayValue(new RegExp(config.webhook_token));
      expect(urlField).toBeInTheDocument();
      expect((urlField as HTMLInputElement).value).toContain('/api/webhooks/');
    });
  });

  it('"Send test webhook" calls the real webhook endpoint with the persisted token', async () => {
    vi.mocked(api.testWebhook).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', status: 'success', output: 'ok', spans: [], fail_class: null,
    });
    const nodeWithToken: FlowNode = { ...triggerNode, config: { trigger_type: 'webhook', webhook_token: 'abc-123' } };
    renderPanel(makeAgent(), nodeWithToken);

    fireEvent.click(screen.getByText(/send test webhook/i));

    await waitFor(() => {
      expect(api.testWebhook).toHaveBeenCalledWith('abc-123', expect.any(Object));
    });
  });
});
