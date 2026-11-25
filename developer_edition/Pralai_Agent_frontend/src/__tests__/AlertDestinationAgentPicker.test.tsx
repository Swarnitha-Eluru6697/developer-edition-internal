import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import AlertDestinationModal from '../components/AlertDestinationModal';
import { api } from '../api';
import { openSelect, pickOption } from './pickOption';
import type { Agent } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

function agent(id: string, name: string, extra?: Partial<Agent>): Agent {
  return { id, name, plane: 'App', status: 'published', ...extra } as Agent;
}

beforeEach(() => { vi.clearAllMocks(); });

function open() {
  return render(
    <MemoryRouter><AppProvider>
      <AlertDestinationModal onClose={() => {}} onSaved={() => {}} />
    </AppProvider></MemoryRouter>);
}

describe('the agent picker', () => {
  it('asks for every agent, not the first page', async () => {
    // The bug: listAgents returns at most 100 rows ordered by an opaque
    // id, so the picker showed an arbitrary slice — mostly leftover test
    // agents — and real agents were missing entirely.
    vi.mocked(api.listAllAgents).mockResolvedValue([agent('a1', 'Acme bot FAQ')]);
    open();
    await waitFor(() => expect(api.listAllAgents).toHaveBeenCalled());
    expect(api.listAgents).not.toHaveBeenCalled();
  });

  it('lists agents by name rather than by opaque id order', async () => {
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a9', 'Zeta'), agent('a1', 'Acme bot FAQ'), agent('a5', 'Marketplace'),
    ]);
    open();
    // Scoped to the agent select — the severity select has options too.
    const picker = await screen.findByLabelText('For agent');
    await screen.findByText(/Showing 3 of 3 agents/);
    openSelect(picker);
    const opts = within(screen.getByRole('listbox')).getAllByRole('option');
    // First option is the "All agents" default.
    expect(opts.slice(1).map((o) => o.textContent?.split(' · ')[0]))
      .toEqual(['Acme bot FAQ', 'Marketplace', 'Zeta']);
  });

  it('distinguishes agents that share a name', async () => {
    // Eight agents are called "Auth Test Agent"; without the id they are
    // indistinguishable and the choice is a guess.
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('aa031c9b05d', 'Auth Test Agent'), agent('a41f127d003', 'Auth Test Agent'),
    ]);
    open();
    await screen.findByText(/Showing 2 of 2 agents/);
    openSelect(screen.getByLabelText('For agent'));
    await screen.findByText(/aa031c9b05d/);
    await screen.findByText(/a41f127d003/);
  });

  it('marks an agent that is not published', async () => {
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a1', 'Half-built', { status: 'draft' }),
    ]);
    open();
    await screen.findByText(/Showing 1 of 1 agents/);
    openSelect(screen.getByLabelText('For agent'));
    await screen.findByText(/\(draft\)/);
  });

  it('leaves snippets out — they do not raise alerts', async () => {
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a1', 'Acme bot FAQ'), agent('a2', 'Some Snippet', { plane: 'Snippet' }),
    ]);
    open();
    await screen.findByText(/Showing 1 of 1 agents/);
    openSelect(screen.getByLabelText('For agent'));
    await screen.findByText(/Acme bot FAQ/);
    expect(screen.queryByText(/Some Snippet/)).toBeNull();
  });

  it('filters the list so a long one is usable', async () => {
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a1', 'Acme bot FAQ'), agent('a2', 'Marketplace Concierge'),
      agent('a3', 'Workspace Assistant'),
    ]);
    open();
    await screen.findByText(/Showing 3 of 3 agents/);
    openSelect(screen.getByLabelText('For agent'));
    await screen.findByText(/Acme bot FAQ/);
    fireEvent.change(screen.getByPlaceholderText(/Filter agents/), { target: { value: 'market' } });
    await waitFor(() => expect(screen.queryByText(/Acme bot FAQ/)).toBeNull());
    await screen.findByText(/Marketplace Concierge/);
  });

  it('is an ordinary dropdown, not an always-open list box', async () => {
    // size>1 renders a list box that behaves differently from every
    // other select in this modal — clicking a row did not read as
    // selecting it.
    vi.mocked(api.listAllAgents).mockResolvedValue([agent('a1', 'Acme bot FAQ')]);
    open();
    const picker = await screen.findByLabelText('For agent');
    expect(picker.getAttribute('size')).toBeNull();
  });

  it('carries the chosen agent through to the saved channel', async () => {
    // Nothing covered the selection actually reaching the payload.
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a04c5406699', 'Acme bot FAQ'), agent('a2', 'Other'),
    ]);
    vi.mocked(api.createAlertDestination).mockResolvedValue({ id: 1 } as never);
    open();
    const picker = await screen.findByLabelText('For agent');
    fireEvent.change(screen.getByPlaceholderText('e.g. acme-oncall'), { target: { value: 'oncall' } });
    fireEvent.change(screen.getByPlaceholderText(/hooks.slack.com/),
      { target: { value: 'https://hooks.slack.com/services/T/B/C' } });
    await screen.findByText(/Showing 2 of 2 agents/);
    pickOption(picker, /a04c5406699/);
    expect(picker.textContent).toContain('a04c5406699');
    fireEvent.click(screen.getByText('Add channel'));
    await waitFor(() => expect(api.createAlertDestination).toHaveBeenCalledWith(
      expect.objectContaining({ agent_id: 'a04c5406699' })));
  });

  it('keeps the chosen agent listed even when the filter excludes it', async () => {
    // Otherwise the select shows blank while the state still holds the
    // id, and the field lies about what will be saved.
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a1', 'Acme bot FAQ'), agent('a2', 'Marketplace Concierge'),
    ]);
    open();
    const picker = await screen.findByLabelText('For agent');
    await screen.findByText(/Showing 2 of 2 agents/);
    pickOption(picker, /Acme bot FAQ · a1/);
    fireEvent.change(screen.getByPlaceholderText(/Filter agents/), { target: { value: 'market' } });
    await waitFor(() => expect(picker.textContent).toContain('a1'));
    openSelect(picker);
    await screen.findByRole('option', { name: /Acme bot FAQ/ });
  });

  it('says how many agents are hidden by the filter', async () => {
    vi.mocked(api.listAllAgents).mockResolvedValue([
      agent('a1', 'Acme bot FAQ'), agent('a2', 'Marketplace Concierge'),
    ]);
    open();
    await screen.findByText(/Showing 2 of 2 agents/);
    fireEvent.change(screen.getByPlaceholderText(/Filter agents/), { target: { value: 'acme' } });
    await screen.findByText(/Showing 1 of 2 agents/);
  });
});
