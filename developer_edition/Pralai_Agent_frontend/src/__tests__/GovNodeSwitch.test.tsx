import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

/**
 * The UCS check is placed on every canvas like the Trigger, and carries
 * its own switch. Switching it off stands the node down in place — it is
 * not deleted, so the graph keeps its shape — and entitlement is still
 * checked before the run either way (see the backend gate).
 */
function agentWith(enabled: boolean | undefined): Agent {
  return {
    id: 'a1', name: 'Fresh', type: 'App', subtype: 'Custom', plane: 'App', owner: 'you',
    status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0,
    cost_7d: '$0.00', first_party: false, tags: [], description: '',
    updated_at: '2026-01-01T00:00:00', metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: 'user input', config: {} },
        { id: 'n2', type: 'gov', x: 220, y: 140, sub: 'entitlement gate',
          config: enabled === undefined ? {} : { enabled } },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }],
    },
  } as Agent;
}

describe('the UCS check node and its switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(agentWith(true));
    vi.mocked(api.updateNodeConfig).mockResolvedValue(agentWith(false));
  });

  const renderBuilder = () => render(
    <MemoryRouter initialEntries={['/builder?agent=a1']}>
      <AppProvider><Builder /></AppProvider>
    </MemoryRouter>
  );

  it('renders the check on the canvas, like the trigger', async () => {
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    expect(container.querySelector('.cnode.trigger')).toBeTruthy();
  });

  it('puts the switch on the node itself', async () => {
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    const sw = container.querySelector('.cnode.gov .cnode-switch input') as HTMLInputElement;
    expect(sw).toBeTruthy();
    expect(sw.checked).toBe(true);
  });

  it('is not offered on other node types', async () => {
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    expect(container.querySelectorAll('.cnode-switch')).toHaveLength(1);
  });

  it('switching it off saves enabled:false on that node', async () => {
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    fireEvent.click(container.querySelector('.cnode.gov .cnode-switch input')!);

    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenCalled());
    const [agentId, nodeId, cfg] = vi.mocked(api.updateNodeConfig).mock.calls[0];
    expect(agentId).toBe('a1');
    expect(nodeId).toBe('n2');
    expect((cfg as any).enabled).toBe(false);
  });

  it('a node with no enabled flag reads as on', async () => {
    // Flows built before the switch existed have no such field, and they
    // were checking — so absent must not read as off.
    vi.mocked(api.getAgent).mockResolvedValue(agentWith(undefined));
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    const sw = container.querySelector('.cnode.gov .cnode-switch input') as HTMLInputElement;
    expect(sw.checked).toBe(true);
    expect(container.querySelector('.cnode.gov.off')).toBeNull();
  });

  it('a stood-down node is marked as such on the canvas', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(agentWith(false));
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    expect(container.querySelector('.cnode.gov.off')).toBeTruthy();
    const sw = container.querySelector('.cnode.gov .cnode-switch input') as HTMLInputElement;
    expect(sw.checked).toBe(false);
  });

  it('clicking the switch does not also select or drag the node', async () => {
    // The node is clickable (selects) and draggable, so the switch has to
    // stop propagation or toggling it would do three things at once.
    const { container } = renderBuilder();
    await waitFor(() => expect(container.querySelector('.cnode.gov')).toBeTruthy());
    expect(container.querySelector('.cnode.gov.sel')).toBeNull();
    fireEvent.click(container.querySelector('.cnode.gov .cnode-switch')!);
    expect(container.querySelector('.cnode.gov.sel')).toBeNull();
  });
});
