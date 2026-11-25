import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import AgenticWorkflowApp from '../AgenticWorkflowApp';
import { AppRoutes } from '../App';
import { api } from '../api';
import { effectivePermissions } from '../context/AppContext';
import { setHostAccountMenu, setHostRoleBadges } from '../host';
import { ALL_PERMISSIONS, P } from '../permissions';
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

function navLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.sidebar .navitem span:not(.count)')).map((s) => s.textContent || '');
}

beforeEach(() => {
  vi.resetAllMocks();
  lastPath = '';
  vi.mocked(api.listUcsPrincipals).mockResolvedValue({ principals: [] } as never);
  vi.mocked(api.listPublishedForUser).mockResolvedValue([]);
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listAlerts).mockResolvedValue([]);
  vi.mocked(api.getHomeStats).mockResolvedValue({ agents: 0, runsToday: 0, successRate: 0, openAlerts: 0, avgCost: '$0' } as never);
});
afterEach(() => {
  cleanup();
  setHostRoleBadges(undefined);
  setHostAccountMenu(null);
});

describe('effectivePermissions', () => {
  it('null / undefined leave /api/me permissions unchanged', () => {
    expect(effectivePermissions(['a', 'b'], null)).toEqual(['a', 'b']);
    expect(effectivePermissions(['a', 'b'], undefined)).toEqual(['a', 'b']);
  });

  it('narrows to the intersection and never adds a permission the user lacks', () => {
    expect(effectivePermissions(['a', 'b'], ['b', 'c'])).toEqual(['b']);
    expect(effectivePermissions([], ['a'])).toEqual([]);
    expect(effectivePermissions(['a'], [])).toEqual([]);
  });
});

describe('the portal\'s "view as role" (viewPermissions)', () => {
  it('narrowing hides a tab the full set shows', async () => {
    grantPermissions(ALL_PERMISSIONS);
    const { container } = render(<AgenticWorkflowApp viewPermissions={[P.overviewView, P.runsView]} />);
    await waitFor(() => expect(navLabels(container)).toEqual(['Overview', 'Runs']));
    expect(screen.queryByRole('link', { name: /^Studio/ })).toBeNull();
  });

  it('can never show a page the user does not hold', async () => {
    grantPermissions([P.webappsUse]);
    const { container } = render(<AgenticWorkflowApp viewPermissions={[P.webappsUse, ...ALL_PERMISSIONS]} />);
    await waitFor(() => expect(navLabels(container)).toEqual(['Web Apps']));
  });

  it('null and undefined show every page /api/me grants', async () => {
    grantPermissions(ALL_PERMISSIONS);
    const first = render(<AgenticWorkflowApp viewPermissions={null} />);
    await waitFor(() => expect(navLabels(first.container)).toEqual(ALL_LABELS));
    first.unmount();
    const second = render(<AgenticWorkflowApp />);
    await waitFor(() => expect(navLabels(second.container)).toEqual(ALL_LABELS));
  });

  it('switching updates the tabs in place, without a remount', async () => {
    grantPermissions(ALL_PERMISSIONS);
    const { container, rerender } = render(<AgenticWorkflowApp viewPermissions={null} />);
    await waitFor(() => expect(navLabels(container)).toEqual(ALL_LABELS));
    rerender(<AgenticWorkflowApp viewPermissions={[P.overviewView, P.webappsUse]} />);
    await waitFor(() => expect(navLabels(container)).toEqual(['Overview', 'Web Apps']));
    rerender(<AgenticWorkflowApp viewPermissions={null} />);
    await waitFor(() => expect(navLabels(container)).toEqual(ALL_LABELS));
    // /api/me was loaded once: the provider stayed mounted throughout.
    expect(api.me).toHaveBeenCalledTimes(1);
  });

  it('a page no longer allowed after a switch redirects to the first allowed one', async () => {
    grantPermissions(ALL_PERMISSIONS);
    const tree = (view: string[] | null) => (
      <MemoryRouter initialEntries={['/']}>
        <AppRoutes viewPermissions={view} />
        <Where />
      </MemoryRouter>
    );
    const { rerender } = render(tree(null));
    await waitFor(() => expect(lastPath).toBe('/'));
    await screen.findByText(/Welcome back/);
    rerender(tree([P.webappsUse]));
    await waitFor(() => expect(lastPath).toBe('/webapps'));
    expect(screen.queryByText("You don't have access to Agents")).toBeNull();
  });
});
