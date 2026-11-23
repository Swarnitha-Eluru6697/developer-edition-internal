import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { AppRoutes } from '../App';
import { api } from '../api';
import { ALL_PERMISSIONS } from '../permissions';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');

const ALL_LABELS = [
  'Overview', 'Studio', 'Templates', 'RAG', 'Integrations',
  'Runs', 'Traces', 'Monitoring', 'Guardrails', 'Alerts',
  'Web Apps', 'Settings',
];

let lastPath = '';
function Where() {
  lastPath = useLocation().pathname;
  return null;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
      <Where />
    </MemoryRouter>,
  );
}

function navLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.sidebar .navitem span:not(.count)')).map((s) => s.textContent || '');
}

beforeEach(() => {
  vi.resetAllMocks();
  lastPath = '';
  // What the pages under test load on mount.
  vi.mocked(api.listUcsPrincipals).mockResolvedValue({ principals: [] } as never);
  vi.mocked(api.listPublishedForUser).mockResolvedValue([]);
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listAlerts).mockResolvedValue([]);
  vi.mocked(api.getHomeStats).mockResolvedValue({ agents: 0, runsToday: 0, successRate: 0, openAlerts: 0, avgCost: '$0' } as never);
});
afterEach(() => cleanup());

describe('pages come from /api/me permissions, not roles', () => {
  it('a user holding only agent:webapps:use sees only Web Apps, and lands there', async () => {
    grantPermissions(['agent:webapps:use']);
    const { container } = renderAt('/');
    await waitFor(() => expect(navLabels(container)).toEqual(['Web Apps']));
    for (const label of ALL_LABELS.filter((l) => l !== 'Web Apps')) {
      expect(screen.queryByRole('link', { name: new RegExp(`^${label}`) })).toBeNull();
    }
    // The Overview it asked for is not theirs: redirected to the first page it is.
    await waitFor(() => expect(lastPath).toBe('/webapps'));
    // Badges are not fetched for pages the user cannot open.
    expect(api.listAgents).not.toHaveBeenCalled();
    expect(api.getHomeStats).not.toHaveBeenCalled();
    // No search box: the user may read neither agents nor knowledge bases.
    expect(screen.queryByLabelText('Search agents or RAG pipelines')).toBeNull();
  });

  it('a page opened by direct URL that the user may not see redirects to a permitted one', async () => {
    grantPermissions(['agent:webapps:use']);
    renderAt('/runs');
    await waitFor(() => expect(lastPath).toBe('/webapps'));
  });

  it('while /api/me is still loading nothing is shown', async () => {
    vi.mocked(api.me).mockReturnValue(new Promise(() => {}));
    const { container } = renderAt('/');
    // Give effects a chance to run.
    await new Promise((r) => setTimeout(r, 20));
    expect(navLabels(container)).toEqual([]);
    expect(screen.queryByText("You don't have access to Agents")).toBeNull();
    expect(container.querySelector('.main')?.textContent).toBe('');
    expect(lastPath).toBe('/');
  });

  it('when /api/me fails (unknown user) nothing is shown but the no-access note', async () => {
    vi.mocked(api.me).mockRejectedValue(new Error('401'));
    const { container } = renderAt('/');
    await screen.findByText("You don't have access to Agents");
    expect(navLabels(container)).toEqual([]);
  });

  it('a user with no permissions at all sees the no-access note', async () => {
    grantPermissions([]);
    const { container } = renderAt('/agents');
    await screen.findByText("You don't have access to Agents");
    expect(navLabels(container)).toEqual([]);
  });

  it('the published-app runner needs a run permission', async () => {
    grantPermissions(['agent:templates:view']);
    renderAt('/app/a1');
    await screen.findByText("You don't have access to Agents");
  });

  it('full permissions show every page', async () => {
    grantPermissions(ALL_PERMISSIONS, 'Asha Rao');
    const { container } = renderAt('/');
    await waitFor(() => expect(navLabels(container)).toEqual(ALL_LABELS));
    expect(lastPath).toBe('/');
    // The user badge shows the first initial and first name, and Overview greets them by name.
    expect(container.querySelector('.topbar [aria-label^="Signed in as"]')?.textContent).toBe('AHi Asha');
    await screen.findByText(/Welcome back, Asha Rao\./);
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled());
    expect(api.getHomeStats).toHaveBeenCalled();
  });
});
