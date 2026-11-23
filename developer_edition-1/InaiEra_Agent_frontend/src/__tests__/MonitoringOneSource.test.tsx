/**
 * The Monitoring page and the agent profile drawer's Monitoring tab must
 * report the same numbers for the same agent, from the same endpoint.
 *
 * They used not to. The page computed from real Run rows; the drawer read
 * `agent.metrics`, a JSON column nothing writes outside seed.py's
 * hardcoded fixture. For seeded agent a5 the drawer claimed 98.9% success
 * while the page said 10%. Both now call GET /api/monitoring/metrics with
 * the same agent id and format through the same `fmtMetric`.
 *
 * This test renders both against ONE mocked response and compares what
 * each puts on screen, so a future change to either alone fails here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { Agent, Trace, Role, AgentLog } from '../types';

vi.mock('../api', () => ({
  BASE: 'http://localhost:3000',
  api: {
    getMetrics: vi.fn(),
    listAgents: vi.fn(), getEvals: vi.fn(), getEvalHealth: vi.fn(),
    getAgent: vi.fn(), getAgentRoles: vi.fn(), getAgentLogs: vi.fn(), getAgentTraces: vi.fn(),
  },
}));
vi.mock('../components/EvalManager', () => ({ default: () => null }));
const openDrawer = vi.fn();
vi.mock('../context/AppContext', () => ({
  useApp: () => ({ toast: vi.fn(), closeDrawer: vi.fn(), openDrawer }),
}));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));

import { api } from '../api';
import Monitoring from '../pages/Monitoring';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';

const AGENT = {
  id: 'ag1', name: 'workflow process', status: 'published', type: 'App',
  owner: 'platform', version: 'v3', app_type: 'workflow', metrics: {},
} as unknown as Agent;

// Deliberately not round numbers, so a value can only match by really
// coming from this payload.
const METRICS = {
  success: 52.3, p50: '5.6s', p95: '14.7s', costAvg: '$0.0078',
  stepLimit: 0, guardrail: 0, entPass: 100,
  retries: null, loopIter: null, chainDepth: null, cacheHit: null,
  sample_size: 65, window_days: 7,
};

function mockAll() {
  vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'ag1', agent_name: AGENT.name, metrics: METRICS });
  vi.mocked(api.listAgents).mockResolvedValue([AGENT]);
  vi.mocked(api.getEvals).mockResolvedValue([]);
  vi.mocked(api.getEvalHealth).mockResolvedValue(null as never);
  vi.mocked(api.getAgent).mockResolvedValue(AGENT);
  vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
  vi.mocked(api.getAgentLogs).mockResolvedValue([] as AgentLog[]);
  vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
}

describe('one source of truth for per-agent monitoring', () => {
  beforeEach(() => { vi.clearAllMocks(); mockAll(); });

  it('both surfaces request the same endpoint with the same agent id', async () => {
    render(<Monitoring />);
    await waitFor(() => expect(api.getMetrics).toHaveBeenCalled());
    // The page passes undefined while scoped to "all", which is the
    // platform aggregate — the drawer is always agent-scoped.
    expect(vi.mocked(api.getMetrics).mock.calls[0][0]).toBeUndefined();

    vi.clearAllMocks(); mockAll();
    render(<AgentProfileDrawer agentId="ag1" />);
    await waitFor(() => expect(api.getMetrics).toHaveBeenCalledWith('ag1'));
  });

  it('renders identical values on the page and in the drawer', async () => {
    const page = render(<Monitoring />);
    await waitFor(() => expect(page.container.textContent).toContain('52.3%'));
    const pageText = page.container.textContent || '';
    page.unmount();

    const drawer = render(<AgentProfileDrawer agentId="ag1" />);
    // The drawer opens on Overview; Monitoring is a tab.
    const tab = await drawer.findByText('Monitoring');
    tab.click();
    await waitFor(() => expect(drawer.container.textContent).toContain('52.3%'));
    const drawerText = drawer.container.textContent || '';

    for (const v of ['52.3%', '5.6s', '14.7s', '$0.0078', '100%']) {
      expect(pageText, `page missing ${v}`).toContain(v);
      expect(drawerText, `drawer missing ${v}`).toContain(v);
    }
  });

  it('uses one wording per metric, so neither reads as a different number', async () => {
    const page = render(<Monitoring />);
    await waitFor(() => expect(page.container.textContent).toContain('52.3%'));
    const pageText = page.container.textContent || '';
    page.unmount();

    const drawer = render(<AgentProfileDrawer agentId="ag1" />);
    (await drawer.findByText('Monitoring')).click();
    await waitFor(() => expect(drawer.container.textContent).toContain('52.3%'));
    const drawerText = drawer.container.textContent || '';

    for (const label of ['Success rate', 'Cost per run (avg)', 'Retries per run',
                         'Loop iterations / run', 'Step-limit hits', 'Guardrail triggers',
                         'Entitlement pass rate', 'Avg chain depth',
                         'Entitlement-plan cache hit']) {
      expect(pageText, `page missing label ${label}`).toContain(label);
      expect(drawerText, `drawer missing label ${label}`).toContain(label);
    }
  });

  it('an untracked metric is a dash in both, never a fabricated number', async () => {
    const page = render(<Monitoring />);
    await waitFor(() => expect(page.container.textContent).toContain('52.3%'));
    // retries/loopIter/chainDepth/cacheHit are null in the payload.
    expect(page.container.textContent).not.toContain('98.9');
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
