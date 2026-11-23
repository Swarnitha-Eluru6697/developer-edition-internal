import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';
import { api } from '../api';
import type { Agent } from '../types';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');
afterEach(() => cleanup());

const AGENT = {
  id: 'a1', name: 'Acme bot FAQ', type: 'App', subtype: 'Custom', plane: 'App',
  owner: 'you', status: 'published', version: 'v2', health_score: 90,
  health_status: 'pass', model: 'Claude Haiku 4.5', mode: 'fixed',
  app_type: 'chatbot', runs_7d: 10, cost_7d: '$1', first_party: false, tags: [],
  description: '', updated_at: '2026-09-25T00:00:00', metrics: {},
  flow: { nodes: [], edges: [] }, features: {},
} as unknown as Agent;

beforeEach(() => {
  // Counts persist across tests otherwise, so the "not called" assertion
  // would see the earlier tests' calls.
  vi.clearAllMocks();
  grantPermissions();
  vi.mocked(api.getAgent).mockResolvedValue(AGENT);
  vi.mocked(api.getAgentRoles).mockResolvedValue([]);
  vi.mocked(api.getAgentLogs).mockResolvedValue([]);
  vi.mocked(api.getAgentTraces).mockResolvedValue([]);
  vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'agent', metrics: {} } as never);
});

function open() {
  return render(<MemoryRouter><AppProvider>
    <AgentProfileDrawer agentId="a1" />
  </AppProvider></MemoryRouter>);
}

describe('the lineage and quality panels', () => {
  it('shows quality measured from this agent, not a hardcoded number', async () => {
    // The panel printed "92%" for every agent regardless of which was
    // open, which is worse than showing nothing.
    vi.mocked(api.getAgentQuality).mockResolvedValue({
      agent_id: 'a1', agent_name: 'Acme bot FAQ', fqn: 'acme-bot-faq.agent.a1',
      score: 89.2, status: 'warn', window_days: 7, computed_at: '2026-09-25T00:00:00',
      dims: {
        accuracy: { verdict: 'warn', observed: 83.3, threshold: 90, rows_scanned: 6,
                    detail: '5/6 eval cases passed across 1 set(s)' },
        reliability: { verdict: 'pass', observed: 95.2, threshold: 95, rows_scanned: 63,
                       detail: '60/63 runs succeeded in the last 7 days' },
      },
    });
    open();
    fireEvent.click(await screen.findByText('Quality'));
    await screen.findByText('89');
    await screen.findByText('83.3%');
    await screen.findByText(/5\/6 eval cases passed/);
  });

  it('says a dimension is unmeasured rather than showing it as zero', async () => {
    // An agent with no eval sets has not scored 0%; it has not been
    // measured, and those are different problems.
    vi.mocked(api.getAgentQuality).mockResolvedValue({
      agent_id: 'a1', agent_name: 'x', fqn: 'f', score: null, status: 'unknown',
      window_days: 7, computed_at: '',
      dims: { accuracy: { verdict: 'unknown', observed: null, threshold: 90,
                          rows_scanned: 0, detail: 'no eval runs yet' } },
    });
    open();
    fireEvent.click(await screen.findByText('Quality'));
    await screen.findByText('unknown');
    await screen.findByText(/no eval runs yet/);
    expect(screen.queryByText('0%')).toBeNull();
  });

  it('shows the knowledge bases this agent declares, not a fixed example', async () => {
    // Every agent used to claim it read lakehouse.sales.orders_daily.
    vi.mocked(api.getAgentLineage).mockResolvedValue({
      agent_id: 'a1', agent_name: 'Acme bot FAQ', fqn: 'acme-bot-faq.agent.a1',
      upstream: [{ id: 'acme_support_faq', name: 'ACME Support FAQ',
                   fqn: 'app_datasets.docs.acme_support_faq' }],
      downstream: [],
      runs: [{ id: 'r-1', status: 'success', ts: '2026-09-25T04:00:00' }],
    });
    open();
    fireEvent.click(await screen.findByText('Lineage'));
    await screen.findByText('ACME Support FAQ');
    await screen.findByText('app_datasets.docs.acme_support_faq');
    expect(screen.queryByText(/lakehouse.sales.orders_daily/)).toBeNull();
  });

  it('says plainly when an agent reads nothing catalogued', async () => {
    vi.mocked(api.getAgentLineage).mockResolvedValue({
      agent_id: 'a1', agent_name: 'x', fqn: 'f',
      upstream: [], downstream: [], runs: [],
    });
    open();
    fireEvent.click(await screen.findByText('Lineage'));
    await screen.findByText(/declares no knowledge bases/);
  });

  it('can publish the measurement to UCS', async () => {
    vi.mocked(api.getAgentQuality).mockResolvedValue({
      agent_id: 'a1', agent_name: 'x', fqn: 'f', score: 80, status: 'warn',
      window_days: 7, computed_at: '',
      dims: { accuracy: { verdict: 'warn', observed: 80, threshold: 90,
                          rows_scanned: 5, detail: 'd' } },
    });
    vi.mocked(api.publishAgentCatalog).mockResolvedValue({
      fqn: 'f', sent: ['accuracy', 'reliability'], failed: [],
    });
    open();
    fireEvent.click(await screen.findByText('Quality'));
    fireEvent.click(await screen.findByText(/Publish to UCS/));
    await waitFor(() => expect(api.publishAgentCatalog).toHaveBeenCalledWith('a1'));
  });

  it('does not load either panel until its tab is opened', async () => {
    open();
    await waitFor(() => expect(api.getAgent).toHaveBeenCalled());
    expect(api.getAgentLineage).not.toHaveBeenCalled();
    expect(api.getAgentQuality).not.toHaveBeenCalled();
  });

  it('lists runs that have not finished, instead of always saying none', async () => {
    // The tab was hardcoded to "No live jobs running right now" and said
    // that while five runs were running.
    vi.mocked(api.getAgentLiveJobs).mockResolvedValue({ jobs: [
      { id: 'r-live-1', status: 'running', started_at: '2026-09-25T04:00:00',
        running_for_s: 120, invoking_user: 'padmin', workflow_id: null },
    ] });
    open();
    fireEvent.click(await screen.findByText('Live Jobs'));
    await screen.findByText('r-live-1');
    await screen.findByText('running');
  });

  it('flags a run that has been going for hours as stuck', async () => {
    // Busy and stuck look identical without this, and a 43-hour run is
    // the thing worth noticing.
    vi.mocked(api.getAgentLiveJobs).mockResolvedValue({ jobs: [
      { id: 'r-old', status: 'running', started_at: '2026-09-23T10:00:00',
        running_for_s: 155716, invoking_user: 'padmin', workflow_id: null },
    ] });
    open();
    fireEvent.click(await screen.findByText('Live Jobs'));
    await screen.findByText('stuck?');
    await screen.findByText(/1d 19h/);
  });

  it('says nothing is running when nothing is', async () => {
    vi.mocked(api.getAgentLiveJobs).mockResolvedValue({ jobs: [] });
    open();
    fireEvent.click(await screen.findByText('Live Jobs'));
    await screen.findByText(/Nothing running right now/);
  });
});
