/**
 * Paging on the Traces page.
 *
 * The endpoint has always been keyset-paginated — 100 rows by default,
 * against 685 traces here — and returns the position to resume from in
 * an `X-Next-Cursor` response header. The client's req() returns only
 * the parsed body, so that header was discarded and the page could show
 * nothing beyond the first batch. It also meant the agent filter was
 * narrowing one page in the browser while matches sat on later ones.
 *
 * Keyset gives a cursor for the NEXT page only, so going back needs the
 * cursors already visited, and any change to what is being listed
 * invalidates all of them: a cursor is a position in one specific
 * ordered result set.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { Trace } from '../types';

vi.mock('../api', () => ({
  api: { listTracesPage: vi.fn(), listAgents: vi.fn(), listTraces: vi.fn() },
}));
vi.mock('../context/AppContext', () => ({ useApp: () => ({ openDrawer: vi.fn() }) }));

import { api } from '../api';
import Traces from '../pages/Traces';
import { pickOption } from './pickOption';

function trace(i: number): Trace {
  return {
    id: `tr-${i}`, agent_id: 'a5', agent_name: 'Support Knowledge Bot', version: 'v1',
    principal: 'user:padmin', plane: 'App', spans: 3, duration: '1.0s',
    ts: '2026-09-22 10:00:00', status: 'ok', user_input: `question ${i}`,
  } as unknown as Trace;
}

const PAGE1 = { items: [trace(1), trace(2)], nextCursor: 'cur-2', hasMore: true };
const PAGE2 = { items: [trace(3)], nextCursor: null, hasMore: false };

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAgents).mockResolvedValue([
    { id: 'a5', name: 'Support Knowledge Bot' },
  ] as never);
  vi.mocked(api.listTracesPage).mockResolvedValue(PAGE1 as never);
});

describe('traces paging', () => {
  it('requests the first page with no cursor', async () => {
    render(<Traces />);
    await waitFor(() => expect(api.listTracesPage).toHaveBeenCalled());
    const call = vi.mocked(api.listTracesPage).mock.calls[0][0];
    expect(call.cursor).toBeNull();
    expect(call.limit).toBe(25);
  });

  it('shows the row range it can honestly know', async () => {
    render(<Traces />);
    // No total is available without a second COUNT over the whole
    // filtered set, so the pager states the range and whether more
    // exist rather than inventing "of 685".
    expect(await screen.findByText(/Showing 1–2/)).toBeTruthy();
    expect(screen.getByText(/Page 1/)).toBeTruthy();
  });

  it('follows the cursor to the next page', async () => {
    render(<Traces />);
    await screen.findByText(/Showing 1–2/);
    vi.mocked(api.listTracesPage).mockResolvedValue(PAGE2 as never);

    fireEvent.click(screen.getByText(/Next/));
    await waitFor(() => {
      const last = vi.mocked(api.listTracesPage).mock.calls[vi.mocked(api.listTracesPage).mock.calls.length - 1][0];
      expect(last.cursor).toBe('cur-2');
    });
    expect(await screen.findByText(/Page 2/)).toBeTruthy();
    // Row numbering continues rather than restarting at 1.
    expect(screen.getByText(/Showing 3–3/)).toBeTruthy();
  });

  it('disables Next on the last page', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue(PAGE2 as never);
    render(<Traces />);
    await screen.findByText(/Showing 1–1/);
    expect((screen.getByText(/Next/) as HTMLButtonElement).disabled).toBe(true);
  });

  it('disables Previous on the first page', async () => {
    render(<Traces />);
    await screen.findByText(/Showing 1–2/);
    expect((screen.getByText(/Previous/) as HTMLButtonElement).disabled).toBe(true);
  });

  it('goes back by dropping the last cursor, not by asking for one', async () => {
    render(<Traces />);
    await screen.findByText(/Showing 1–2/);
    fireEvent.click(screen.getByText(/Next/));
    await screen.findByText(/Page 2/);

    vi.mocked(api.listTracesPage).mockResolvedValue(PAGE1 as never);
    fireEvent.click(screen.getByText(/Previous/));
    await waitFor(() => {
      const last = vi.mocked(api.listTracesPage).mock.calls[vi.mocked(api.listTracesPage).mock.calls.length - 1][0];
      expect(last.cursor).toBeNull();
    });
    expect(await screen.findByText(/Page 1/)).toBeTruthy();
  });

  it('resets to page one when a filter changes', async () => {
    render(<Traces />);
    await screen.findByText(/Showing 1–2/);
    fireEvent.click(screen.getByText(/Next/));
    await screen.findByText(/Page 2/);

    // A cursor is a position in one specific ordered result set, so
    // carrying it across a filter change would land on an unrelated row.
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } });
    await waitFor(() => {
      const last = vi.mocked(api.listTracesPage).mock.calls[vi.mocked(api.listTracesPage).mock.calls.length - 1][0];
      expect(last.cursor).toBeNull();
      expect(last.fromDate).toBe('2026-09-01');
    });
    expect(await screen.findByText(/Page 1/)).toBeTruthy();
  });

  it('resets to page one when the page size changes', async () => {
    render(<Traces />);
    await screen.findByText(/Showing 1–2/);
    fireEvent.click(screen.getByText(/Next/));
    await screen.findByText(/Page 2/);

    pickOption(screen.getByRole('button', { name: 'Rows per page' }), '50');
    await waitFor(() => {
      const last = vi.mocked(api.listTracesPage).mock.calls[vi.mocked(api.listTracesPage).mock.calls.length - 1][0];
      expect(last.limit).toBe(50);
      expect(last.cursor).toBeNull();
    });
  });
});
