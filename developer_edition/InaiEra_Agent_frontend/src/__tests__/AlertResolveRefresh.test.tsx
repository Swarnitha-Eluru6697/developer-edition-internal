import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Alerts from '../pages/Alerts';
import DrawerHost from '../components/DrawerHost';
import ToastHost from '../components/ToastHost';
import { api } from '../api';
import type { Alert } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');
afterEach(() => cleanup());

function alert(overrides?: Partial<Alert>): Alert {
  return {
    id: 'al-1', sev: 'high', rule: 'error-rate', agent_id: 'a1',
    agent_name: 'Acme bot FAQ', state: 'open', acked: false,
    ts: '2026-09-24 09:00', summary: '70 of 104 runs failed.', history: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAlerts).mockResolvedValue([alert()]);
  vi.mocked(api.listAlertsPage).mockResolvedValue({ items: [alert()], nextCursor: null, hasMore: false });
  vi.mocked(api.getAlert).mockResolvedValue(alert());
  vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.listAlertDestinationsPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
  vi.mocked(api.previewAlertRules).mockResolvedValue({ conditions: [], thresholds: {} });
  vi.mocked(api.listAgents).mockResolvedValue([] as never);
});

function renderPage() {
  return render(
    <MemoryRouter><AppProvider>
      <Alerts /><DrawerHost /><ToastHost />
    </AppProvider></MemoryRouter>);
}

describe('resolving an alert', () => {
  it('reloads the list so the resolved row leaves the Open tab', async () => {
    // The bug: the drawer resolved the alert and closed itself, but
    // never told the list. The resolved row sat in the Open tab until
    // the page was reloaded, so the action looked like it had failed.
    vi.mocked(api.resolveAlert).mockResolvedValue(alert({ state: 'resolved' }));
    renderPage();
    fireEvent.click(await screen.findByText('error-rate'));
    fireEvent.click(await screen.findByText('Resolve'));
    await waitFor(() => expect(api.resolveAlert).toHaveBeenCalled());
    // Two calls: the initial load, and the reload after resolving.
    await waitFor(() => expect(vi.mocked(api.listAlertsPage).mock.calls.length)
      .toBeGreaterThan(1));
  });

  it('reloads the list after acknowledging too', async () => {
    vi.mocked(api.acknowledgeAlert).mockResolvedValue(alert({ acked: true }));
    renderPage();
    fireEvent.click(await screen.findByText('error-rate'));
    fireEvent.click(await screen.findByText('Acknowledge'));
    await waitFor(() => expect(vi.mocked(api.listAlertsPage).mock.calls.length)
      .toBeGreaterThan(1));
  });

  it('re-counts open alerts after one is resolved', async () => {
    // The sidebar badge is fetched in App.tsx. Read once on mount, it
    // kept claiming work that had already been done — the same staleness
    // as the Open tab kept showing a resolved row.
    vi.mocked(api.resolveAlert).mockResolvedValue(alert({ state: 'resolved' }));
    renderPage();
    fireEvent.click(await screen.findByText('error-rate'));
    fireEvent.click(await screen.findByText('Resolve'));
    await waitFor(() => expect(
      vi.mocked(api.listAlertsPage).mock.calls
        .filter((c) => c[0]?.state === 'open').length).toBeGreaterThan(1));
  });

  it('asks for the acknowledged tab separately from open', async () => {
    // Acting on an alert from Slack or Teams has to move it out of Open:
    // acknowledging says someone is on it, resolving says it is fixed.
    renderPage();
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'open' })));
    fireEvent.click(screen.getByText('Acknowledged'));
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'acknowledged' })));
  });

  it('warns when alerts reached no channel instead of hiding them', async () => {
    // The Open tab lists only delivered alerts, so an undelivered one
    // would otherwise vanish from the UI entirely.
    vi.mocked(api.listAlerts).mockImplementation((state?: string) =>
      Promise.resolve(state === 'undelivered' ? [alert({ id: 'al-2' })] : []));
    renderPage();
    await screen.findByText(/1 alert reached no channel yet/);
  });

  it('does not offer a manual send — delivery is automatic', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('error-rate'));
    await screen.findByText('Resolve');
    expect(screen.queryByText('Send to channels')).toBeNull();
  });
});


describe('paging the alert list', () => {
  const page = (n: number, hasMore: boolean, cursor: string | null) => ({
    items: Array.from({ length: n }, (_, i) => alert({ id: `al-${i}` })),
    nextCursor: cursor, hasMore,
  });

  it('says which rows are on screen, and whether more exist', async () => {
    // The endpoint reports hasMore, not a total — counting would be a
    // second full query per page. The pager must not invent "of 685".
    vi.mocked(api.listAlertsPage).mockResolvedValue(page(25, true, 'c1'));
    renderPage();
    await screen.findByText(/Showing 1–25 of more/);
  });

  it('carries the real offset forward instead of assuming full pages', async () => {
    // A short page would make the row numbers drift and never recover if
    // the offset were pageIndex * pageSize.
    vi.mocked(api.listAlertsPage).mockResolvedValue(page(10, true, 'c1'));
    renderPage();
    await screen.findByText(/Showing 1–10/);
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText(/Showing 11–20/);
  });

  it('cannot go back from the first page, or forward past the last', async () => {
    vi.mocked(api.listAlertsPage).mockResolvedValue(page(5, false, null));
    renderPage();
    await screen.findByText(/Showing 1–5/);
    expect(screen.getByText('‹ Previous')).toBeDisabled();
    expect(screen.getByText('Next ›')).toBeDisabled();
  });

  it('starts paging again from the top when the tab changes', async () => {
    // A cursor is a position in ONE ordered result set — reusing an Open
    // cursor against Resolved would land on an unrelated row.
    vi.mocked(api.listAlertsPage).mockResolvedValue(page(25, true, 'c1'));
    renderPage();
    await screen.findByText(/Showing 1–25/);
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText('Page 2');
    fireEvent.click(screen.getByText('Resolved'));
    await screen.findByText('Page 1');
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'resolved', cursor: null })));
  });

  it('changing the page size restarts paging too', async () => {
    vi.mocked(api.listAlertsPage).mockResolvedValue(page(25, true, 'c1'));
    renderPage();
    await screen.findByText(/Showing 1–25/);
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText('Page 2');
    pickOption(screen.getByRole('button', { name: 'Rows per page' }), '50');
    await screen.findByText('Page 1');
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 50, cursor: null })));
  });

  it('filters by date on every tab that lists events', async () => {
    vi.mocked(api.listAlertsPage).mockResolvedValue(
      { items: [], nextCursor: null, hasMore: false });
    renderPage();
    fireEvent.change(await screen.findByLabelText(/From/), { target: { value: '2026-09-01' } });
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ fromDate: '2026-09-01' })));
    fireEvent.change(screen.getByLabelText(/To/), { target: { value: '2026-09-30' } });
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ fromDate: '2026-09-01', toDate: '2026-09-30' })));
  });

  it('sends the date range to the rules list too', async () => {
    vi.mocked(api.listAlertRulesPage).mockResolvedValue(
      { items: [], nextCursor: null, hasMore: false });
    renderPage();
    fireEvent.click(screen.getByText('Rules'));
    fireEvent.change(await screen.findByLabelText(/From/), { target: { value: '2026-09-10' } });
    await waitFor(() => expect(api.listAlertRulesPage).toHaveBeenCalledWith(
      expect.objectContaining({ fromDate: '2026-09-10' })));
  });

  it('offers no date range on Channels — a channel is configuration, not an event', async () => {
    renderPage();
    fireEvent.click(screen.getByText('Channels'));
    await waitFor(() => expect(api.listAlertDestinationsPage).toHaveBeenCalled());
    expect(screen.queryByLabelText(/From/)).toBeNull();
  });

  it('restarts paging when the date range changes', async () => {
    // A cursor points into one ordered result set; reusing it after a
    // filter change would land on an unrelated row.
    vi.mocked(api.listAlertsPage).mockResolvedValue(
      { items: Array.from({ length: 25 }, (_, i) => alert({ id: `al-${i}` })),
        nextCursor: 'c1', hasMore: true });
    renderPage();
    await screen.findByText(/Showing 1–25/);
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText('Page 2');
    fireEvent.change(screen.getByLabelText(/From/), { target: { value: '2026-09-01' } });
    await screen.findByText('Page 1');
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: null, fromDate: '2026-09-01' })));
  });

  it('does not fetch a tab that is not on screen', async () => {
    // Three lists on one page would otherwise all fetch on mount.
    renderPage();
    await waitFor(() => expect(api.listAlertsPage).toHaveBeenCalled());
    expect(api.listAlertRulesPage).not.toHaveBeenCalled();
    expect(api.listAlertDestinationsPage).not.toHaveBeenCalled();
  });
});
