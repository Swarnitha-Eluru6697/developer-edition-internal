import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import WebApps from '../pages/WebApps';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a5', name: 'Support Knowledge Bot', type: 'App', subtype: 'Conversational', plane: 'App',
    owner: 'you', status: 'published', version: 'v9', health_score: 95, health_status: 'pass',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] }, features: {},
    ...overrides,
  } as Agent;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AppProvider><WebApps /></AppProvider>
    </MemoryRouter>
  );
}

describe('WebApps — real UCS-filtered end-user experience', () => {
  beforeEach(() => {
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
    vi.clearAllMocks();
    // The "Running as" chooser loads the principals UCS really has, so
    // it can offer identities that can hold a grant. Under vi.mock's
    // automock this returns undefined unless stubbed, and the page's
    // effect would then call .then on it.
    vi.mocked(api.listUcsPrincipals).mockResolvedValue({
      principals: [
        { name: 'alice', principal: 'user:developer',
          groups: ['acme-docs-readers'], entitled: true },
        { name: 'alice', principal: 'user:alice',
          groups: ['employees_reader'], entitled: true },
      ],
    });
  });

  it('calls the real published-for-user endpoint with the real identity, and lists only what comes back', async () => {
    vi.mocked(api.listPublishedForUser).mockResolvedValue([makeAgent({ id: 'a5', name: 'Support Knowledge Bot' }), makeAgent({ id: 'a6', name: 'Invoice Approval Flow', app_type: 'workflow' })]);
    renderPage();
    await waitFor(() => {
      expect(api.listPublishedForUser).toHaveBeenCalled();
      expect(screen.getAllByText('Support Knowledge Bot').length).toBeGreaterThan(0);
      expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument();
    });
  });

  it('an honest empty state shows when UCS grants this identity no real access — not a fake "no apps yet" message', async () => {
    vi.mocked(api.listPublishedForUser).mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/No apps are accessible to/)).toBeInTheDocument());
  });

  it('a real UCS error (e.g. service unreachable) shows honestly, not a silent empty list', async () => {
    vi.mocked(api.listPublishedForUser).mockRejectedValue(new Error('UCS unreachable'));
    renderPage();
    await waitFor(() => expect(screen.getByText(/Couldn't load your apps/)).toBeInTheDocument());
  });

  it('the first accessible app auto-selects and its real runner renders', async () => {
    vi.mocked(api.listPublishedForUser).mockResolvedValue([makeAgent({ id: 'a5', name: 'Support Knowledge Bot', app_type: 'chatbot' })]);
    renderPage();
    await waitFor(() => {
      expect(screen.getAllByText('Support Knowledge Bot').length).toBeGreaterThan(0);
      expect(document.querySelector('.runner-shell')).toBeInTheDocument();
    });
  });

  it('clicking a different app in the rail switches the real selected app', async () => {
    vi.mocked(api.listPublishedForUser).mockResolvedValue([
      makeAgent({ id: 'a5', name: 'Support Knowledge Bot', app_type: 'chatbot' }),
      makeAgent({ id: 'a6', name: 'Invoice Approval Flow', app_type: 'workflow' }),
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText('Invoice Approval Flow')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Invoice Approval Flow'));
    await waitFor(() => {
      const items = document.querySelectorAll('.webapps-rail-item');
      const activeItem = Array.from(items).find((el) => el.classList.contains('active'));
      expect(activeItem?.textContent).toContain('Invoice Approval Flow');
    });
  });
});
