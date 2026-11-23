import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Sidebar from '../components/Sidebar';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');
afterEach(() => cleanup());

async function renderSidebar(agentCount: number, openAlertCount: number) {
  grantPermissions();
  const r = render(
    <MemoryRouter>
      <AppProvider>
        <Sidebar agentCount={agentCount} openAlertCount={openAlertCount} />
      </AppProvider>
    </MemoryRouter>);
  // Nothing is listed until /api/me has answered.
  await screen.findByText('Alerts');
  return r;
}

function badgeFor(label: string) {
  const link = screen.getByText(label).closest('a');
  return link?.querySelector('.count');
}

describe('the sidebar badges', () => {
  it('shows the number of open alerts, not just that some exist', async () => {
    // It was a dot before. How many alerts are waiting is the thing you
    // decide on; a dot answers only whether any exist at all.
    await renderSidebar(26, 7);
    expect(badgeFor('Alerts')?.textContent).toBe('7');
  });

  it('uses the same badge shape as Studio', async () => {
    await renderSidebar(26, 7);
    expect(badgeFor('Studio')?.textContent).toBe('26');
    expect(badgeFor('Alerts')?.className).toContain('count');
  });

  it('distinguishes work outstanding from a neutral tally', async () => {
    // An agent count is information; alerts waiting are work. Same
    // shape, different tint.
    await renderSidebar(26, 7);
    expect(badgeFor('Studio')?.className).not.toContain('alert');
    expect(badgeFor('Alerts')?.className).toContain('alert');
  });

  it('hides the alert badge at zero rather than showing a 0', async () => {
    await renderSidebar(0, 0);
    expect(badgeFor('Alerts')).toBeNull();
    // Studio's count still shows — an account with no agents is worth
    // stating, whereas "0 alerts" is noise on every page load.
    expect(badgeFor('Studio')?.textContent).toBe('0');
  });
});
