import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Alerts from '../pages/Alerts';
import ToastHost from '../components/ToastHost';
import { api } from '../api';
import type { AlertCondition, AlertDestination } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

function dest(overrides?: Partial<AlertDestination>): AlertDestination {
  return {
    id: 1, name: 'acme-oncall', kind: 'slack', enabled: true, min_severity: 'high',
    agent_id: 'a04c5406699', url_hint: 'hooks.slack.com/…/XYZ123', configured: true,
    last_attempt_at: null, last_status: null, last_detail: null,
    ...overrides,
  };
}

beforeEach(() => {
  // Call counts persist across tests otherwise, so the "not called"
  // assertion below would see the earlier tests' calls.
  vi.clearAllMocks();
  vi.mocked(api.listAlerts).mockResolvedValue([]);
  vi.mocked(api.listAlertsPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [dest()], nextCursor: null, hasMore: false });
  vi.mocked(api.previewAlertRules).mockResolvedValue({ conditions: [], thresholds: {} });
  vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.getAlertRuleKinds).mockResolvedValue({ kinds: [] });
  vi.mocked(api.listAllAgents).mockResolvedValue([] as never);
  vi.mocked(api.listAgents).mockResolvedValue(
    [{ id: 'a04c5406699', name: 'Acme bot FAQ' }] as never);
});

function renderAlerts() {
  return render(
    <MemoryRouter><AppProvider><Alerts /><ToastHost /></AppProvider></MemoryRouter>);
}

async function openChannels() {
  renderAlerts();
  fireEvent.click(screen.getByText('Channels'));
  await waitFor(() => expect(api.listAlertDestinationsPage).toHaveBeenCalled());
}

describe('the channels tab', () => {
  it('names the agent a channel is scoped to, not its id', async () => {
    // The destination carries an agent_id; someone reading this page
    // cannot tell which agent 'a04c5406699' is.
    await openChannels();
    await screen.findByText('Acme bot FAQ');
  });

  it('says which alerts a channel actually receives', async () => {
    await openChannels();
    await screen.findByText(/high only/);
  });

  it('says "all agents" for an unscoped channel', async () => {
    vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [dest({ agent_id: null })], nextCursor: null, hasMore: false });
    await openChannels();
    await screen.findByText('all agents');
  });

  it('shows the redacted hint and never a usable URL', async () => {
    await openChannels();
    await screen.findByText('hooks.slack.com/…/XYZ123');
    expect(document.body.textContent).not.toContain('hooks.slack.com/services');
  });

  it('surfaces a failing channel rather than leaving it silently broken', async () => {
    vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [dest({
      last_status: 'failed', last_attempt_at: '2026-09-23T10:00:00',
      last_detail: 'HTTP 404: no_team',
    })], nextCursor: null, hasMore: false });
    await openChannels();
    await screen.findByText('last send failed');
    await screen.findByText(/no_team/);
  });

  it('tells the user a test failed instead of reporting success', async () => {
    // The API answers 200 with the reason when a channel refuses, so a
    // resolved promise is NOT proof the message arrived.
    vi.mocked(api.testAlertDestination).mockResolvedValue(
      { ...dest(), delivered: false, detail: 'HTTP 404: no_team' });
    await openChannels();
    fireEvent.click(await screen.findByText(/Send test/));
    await screen.findByText(/did not accept the message/);
  });

  it('confirms in a side panel before removing a channel, and does nothing if declined', async () => {
    // A browser dialog cannot say what is actually lost. This one names
    // the consequence people care about: the webhook URL is gone.
    await openChannels();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove acme-oncall' }));
    await screen.findByText(/webhook URL is deleted with it/);
    fireEvent.click(screen.getByText('Cancel'));
    expect(api.deleteAlertDestination).not.toHaveBeenCalled();
  });

  it('removes the channel when the side panel is confirmed', async () => {
    vi.mocked(api.deleteAlertDestination).mockResolvedValue({ deleted: 1 });
    await openChannels();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove acme-oncall' }));
    fireEvent.click(await screen.findByText('Remove channel'));
    await waitFor(() => expect(api.deleteAlertDestination).toHaveBeenCalledWith(1));
  });

  it('says plainly that alerts go nowhere when no channel is configured', async () => {
    vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
    await openChannels();
    await screen.findByText(/alerts stay inside the platform/);
  });

  it('does not fetch destinations while looking at the alert list', async () => {
    renderAlerts();
    await waitFor(() => expect(api.listAlerts).toHaveBeenCalled());
    expect(api.listAlertDestinationsPage).not.toHaveBeenCalled();
  });
});


function cond(overrides?: Partial<AlertCondition>): AlertCondition {
  return {
    sev: 'high', rule: 'error-rate', agent_id: 'a1', agent_name: 'Acme bot FAQ',
    summary: '70 of 104 runs failed in the last 24h (67%).', already_open: false,
    ...overrides,
  };
}

async function openRules() {
  renderAlerts();
  fireEvent.click(screen.getByText('Rules'));
  await waitFor(() => expect(api.previewAlertRules).toHaveBeenCalled());
}

describe('the rules tab', () => {
  it('shows what the rules found in real run data', async () => {
    vi.mocked(api.previewAlertRules).mockResolvedValue({
      conditions: [cond()], thresholds: {},
    });
    await openRules();
    await screen.findByText(/70 of 104 runs failed/);
    await screen.findByText('error-rate');
  });

  it('distinguishes what would be raised from what is already reported', async () => {
    // Without this the page cannot explain why a real, true condition
    // produced no alert — which reads as the rules being broken.
    vi.mocked(api.previewAlertRules).mockResolvedValue({
      conditions: [cond({ already_open: true }), cond({ agent_id: 'a2', agent_name: 'Other' })],
      thresholds: {},
    });
    await openRules();
    await screen.findByText('already open — suppressed');
    await screen.findByText('would raise');
  });

  it('says plainly when nothing is breaching', async () => {
    await openRules();
    await screen.findByText(/Nothing is currently breaching/);
  });

  it('reports what a sweep actually did, including deliveries', async () => {
    vi.mocked(api.previewAlertRules).mockResolvedValue({ conditions: [cond()], thresholds: {} });
    vi.mocked(api.sweepAlertRules).mockResolvedValue({
      evaluated: '16', raised: '3', suppressed_as_duplicate: '13',
      deliveries: '6', destinations: '2', resolved: '1',
    });
    await openRules();
    fireEvent.click(screen.getByText(/Run rules now/));
    await screen.findByText(/3 raised, 13 already open, 1 resolved, 6 delivered to 2 channel/);
  });

  it('does not evaluate rules while looking at the alert list', async () => {
    renderAlerts();
    await waitFor(() => expect(api.listAlerts).toHaveBeenCalled());
    expect(api.previewAlertRules).not.toHaveBeenCalled();
  });
});


function rule(overrides?: Partial<import('../types').AlertRule>) {
  return {
    id: 1, name: 'error-rate', kind: 'error_rate', severity: 'high' as const,
    threshold: 25, window_seconds: 86400, min_runs: 20, fail_classes: [],
    agent_id: null, agent_name: null, enabled: true,
    ...overrides,
  };
}

describe('the rules table', () => {
  it('lists the configured rules with what makes each one fire', async () => {
    // Rules are data now, so the page has to show the numbers someone
    // configured rather than implying they are fixed in code.
    vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [rule()], nextCursor: null, hasMore: false });
    await openRules();
    await screen.findByText('error-rate');
    await screen.findByText(/error_rate|error rate/);
    await screen.findByText(/min 20 runs/);
  });

  it('names the agent a rule is scoped to, and says so when it is not', async () => {
    vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [
      rule({ id: 1, name: 'global' }),
      rule({ id: 2, name: 'scoped', agent_id: 'a1', agent_name: 'Acme bot FAQ' }),
    ], nextCursor: null, hasMore: false });
    await openRules();
    await screen.findByText('all agents');
    await screen.findByText('Acme bot FAQ');
  });

  it('warns plainly when no rule exists, because nothing would ever alert', async () => {
    vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
    await openRules();
    await screen.findByText(/nothing will ever alert/);
  });

  it('confirms in a side panel before deleting a rule, and says what it applies to', async () => {
    vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [rule()], nextCursor: null, hasMore: false });
    await openRules();
    fireEvent.click(await screen.findByLabelText('Delete error-rate'));
    // Scoped to the panel — the rules table also prints "all agents".
    const panel = (await screen.findByText(/stop being evaluated/)).closest('.modal') as HTMLElement;
    expect(panel.textContent).toContain('all agents');
    fireEvent.click(within(panel).getByText('Cancel'));
    expect(api.deleteAlertRule).not.toHaveBeenCalled();
  });

  it('toggling a rule keeps its other settings', async () => {
    // A PATCH that sent only `enabled` would reset the threshold to the
    // schema default and silently loosen the rule.
    vi.mocked(api.listAlertRulesPage).mockResolvedValue({ items: [rule({ threshold: 40, min_runs: 5 })], nextCursor: null, hasMore: false });
    vi.mocked(api.updateAlertRule).mockResolvedValue(rule());
    await openRules();
    fireEvent.click(await screen.findByTitle('Enabled'));
    await waitFor(() => expect(api.updateAlertRule).toHaveBeenCalledWith(
      1, expect.objectContaining({ threshold: 40, min_runs: 5, enabled: false })));
  });
});
