import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Topbar from '../components/Topbar';
import { setHostAccountMenu, setHostRoleBadges } from '../host';
import { grantPermissions } from './grantPermissions';
vi.mock('../api');

function renderTopbar() {
  grantPermissions(undefined, 'jane.doe');
  render(
    <MemoryRouter>
      <AppProvider>
        <Topbar />
      </AppProvider>
    </MemoryRouter>,
  );
}

describe('top bar chrome inside and outside the portal', () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    setHostRoleBadges(undefined);
    setHostAccountMenu(null);
  });

  it('hosted (roleBadges given): shows the badges, and no bell or avatar', async () => {
    setHostRoleBadges(<span>Builder · Agent Admin</span>);
    renderTopbar();
    // The search appears once /api/me has answered, so the permission-gated parts are settled.
    await screen.findByLabelText('Search agents or RAG pipelines');
    expect(screen.getByText('Builder · Agent Admin')).toBeInTheDocument();
    expect(screen.queryByTitle('Alerts')).toBeNull();
    expect(screen.queryByTitle('jane.doe')).toBeNull();
    expect(screen.queryByText('JD')).toBeNull();
  });

  it('standalone (no roleBadges): keeps the bell and the avatar', async () => {
    renderTopbar();
    await screen.findByLabelText('Search agents or RAG pipelines');
    expect(screen.getByTitle('Alerts')).toBeInTheDocument();
    // Developer Edition user badge: first initial + "Hi <first name>".
    expect(screen.getByTitle('jane.doe')).toHaveTextContent('JHi jane.doe');
    expect(document.querySelector('.rolebadges')).toBeNull();
  });

  it("an older portal's account menu still takes the avatar's place", async () => {
    setHostAccountMenu(<button>Account</button>);
    renderTopbar();
    await screen.findByLabelText('Search agents or RAG pipelines');
    expect(screen.getByRole('button', { name: 'Account' })).toBeInTheDocument();
    expect(screen.queryByText('JD')).toBeNull();
  });
});
