import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { Agent } from '../types';

vi.mock('../api', () => ({
  api: {
    listUcsPrincipals: vi.fn(),
    listPublishedForUser: vi.fn(),
    getAgent: vi.fn(),
  },
}));
vi.mock('../context/AppContext', () => ({
  useApp: () => ({ endUserIdentity: 'user:padmin', setEndUserIdentity: vi.fn(), toast: vi.fn() }),
}));

import { api } from '../api';
import WebApps from '../pages/WebApps';
import { FormRunner } from '../components/RunnerUI';

const APP = {
  id: 'w1', name: 'test workflow', app_type: 'workflow', status: 'published',
  flow: { nodes: [], edges: [] }, features: {}, input_variables: [],
} as unknown as Agent;

function mountPage() {
  (api.listUcsPrincipals as ReturnType<typeof vi.fn>).mockResolvedValue({ principals: [] });
  (api.listPublishedForUser as ReturnType<typeof vi.fn>).mockResolvedValue([APP]);
  return render(<WebApps />);
}

describe('Web Apps page layout', () => {
  beforeEach(() => vi.clearAllMocks());

  it('does not pull itself outside .main with negative margins', async () => {
    const { container } = mountPage();
    await waitFor(() => expect(container.querySelector('.webapps-shell')).toBeTruthy());
    const shell = container.querySelector('.webapps-shell') as HTMLElement;
    // The page renders directly inside .main, which has no padding. A
    // negative margin here slides the rail under the sidebar and the
    // runner header under the topbar.
    expect(shell.style.marginLeft).toBe('');
    expect(shell.style.marginTop).toBe('');
    const css = shell.className;
    expect(css).toContain('webapps-shell');
  });

  it('renders the rail and the runner pane as siblings', async () => {
    const { container } = mountPage();
    await waitFor(() => expect(container.querySelector('.webapps-rail')).toBeTruthy());
    const shell = container.querySelector('.webapps-shell') as HTMLElement;
    const kids = Array.from(shell.children).map((c) => c.className);
    expect(kids[0]).toContain('webapps-rail');
    expect(kids[1]).toContain('webapps-main');
  });

  it('shows the selected app in both the rail and the runner header', async () => {
    mountPage();
    // Two places, neither of which may be clipped: the rail item and the
    // runner top bar.
    const hits = await screen.findAllByText('test workflow');
    expect(hits.length).toBe(2);
  });
});

describe('the workflow form does not reserve an empty second column', () => {
  it('is a single column before a run has produced anything', () => {
    const { container } = render(
      <FormRunner agent={APP} appType="workflow" busy={false} setBusy={vi.fn()} invokingUser="user:padmin" />,
    );
    const form = container.querySelector('.form-runner') as HTMLElement;
    // Held open from the start, the empty right-hand track made the
    // centred form read as pinned to the left of a large void.
    expect(form.classList.contains('form-runner-split')).toBe(false);
    expect(form.children.length).toBe(1);
  });

  it('becomes two columns once a run is in flight and fills both', () => {
    const { container } = render(
      <FormRunner agent={APP} appType="workflow" busy setBusy={vi.fn()} invokingUser="user:padmin" />,
    );
    const form = container.querySelector('.form-runner') as HTMLElement;
    expect(form.classList.contains('form-runner-split')).toBe(true);
    expect(form.children.length).toBe(2);
  });
});
