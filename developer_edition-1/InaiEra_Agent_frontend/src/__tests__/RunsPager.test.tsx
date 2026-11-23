import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Runs from '../pages/Runs';
import { api } from '../api';
import type { Run } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

const run = (id: string): Run => ({
  id, agent_id: 'a1', agent_name: `Agent ${id}`, status: 'success', fail_class: null,
  hops: 1, duration: '1s', cost: '$0', ts: '10:00:00',
} as unknown as Run);

function renderRuns() {
  return render(<MemoryRouter><AppProvider><Runs /></AppProvider></MemoryRouter>);
}

describe('Runs — keyset pagination on every tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listRunsPage).mockImplementation(async ({ cursor }) =>
      cursor === 'c2'
        ? { items: [run('r3')], nextCursor: null, hasMore: false }
        : { items: [run('r1'), run('r2')], nextCursor: 'c2', hasMore: true });
  });

  it('pages forward and back with the server cursor', async () => {
    renderRuns();
    await waitFor(() => expect(screen.getByText('r1')).toBeInTheDocument());
    expect(screen.getByText(/Showing 1–2 of more/)).toBeInTheDocument();
    expect(screen.getByText('‹ Previous').closest('button')).toBeDisabled();

    fireEvent.click(screen.getByText('Next ›'));
    await waitFor(() => expect(screen.getByText('r3')).toBeInTheDocument());
    expect(api.listRunsPage).toHaveBeenLastCalledWith({ status: 'all', cursor: 'c2', limit: 25 });
    expect(screen.getByText('Showing 3–3')).toBeInTheDocument();
    expect(screen.getByText('Page 2')).toBeInTheDocument();
    expect(screen.getByText('Next ›').closest('button')).toBeDisabled();

    fireEvent.click(screen.getByText('‹ Previous'));
    await waitFor(() => expect(screen.getByText('r1')).toBeInTheDocument());
    expect(screen.getByText('Page 1')).toBeInTheDocument();
  });

  it('switching tab filters on the server and starts again at page 1', async () => {
    renderRuns();
    await waitFor(() => expect(screen.getByText('r1')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Next ›'));
    await waitFor(() => expect(screen.getByText('Page 2')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Failed'));
    await waitFor(() =>
      expect(api.listRunsPage).toHaveBeenLastCalledWith({ status: 'failed', cursor: null, limit: 25 }));
    expect(screen.getByText('Page 1')).toBeInTheDocument();
  });
});
