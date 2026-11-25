import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import ModalHost from '../components/ModalHost';
import { api } from '../api';

// TOP-LEVEL role catalogue stub: PublishModal loads it on mount, and
// this file has several describe blocks with their own beforeEach, so
// stubbing inside one of them leaves the others throwing.
beforeEach(() => {
  vi.mocked(api.getAgentRoleCatalogue).mockResolvedValue([]);
});
// Separately registered so it runs after each block's own clearAllMocks
// would not matter: clearAllMocks keeps implementations.
beforeEach(() => grantPermissions());
import type { Agent } from '../types';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');
afterEach(() => { cleanup(); sessionStorage.clear(); });

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Flow', type: 'App', subtype: 'Custom', plane: 'App', owner: 'you',
    status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'workflow', runs_7d: 0,
    cost_7d: '$0.00', first_party: false, tags: [], description: '',
    updated_at: '2026-01-01T00:00:00', metrics: {}, features: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'gov', x: 220, y: 140, sub: '', config: { enabled: true } },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }],
    },
    ...overrides,
  } as Agent;
}

function base() {
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
  vi.mocked(api.listAgentVersions).mockResolvedValue([]);
}

const renderBuilder = () => render(
  <MemoryRouter initialEntries={['/builder?agent=a1']}>
    <AppProvider><Builder /><ModalHost /></AppProvider>
  </MemoryRouter>
);

/**
 * Publishing happens in PublishModal, which ModalHost renders rather
 * than the Builder. The modal refetched into its OWN state and told
 * nobody, so the status chip underneath stayed "draft" until the page
 * was reloaded.
 */
describe('the status chip after publishing', () => {
  beforeEach(() => { vi.clearAllMocks(); base(); });

  it('updates without a page reload', async () => {
    const draft = makeAgent({ status: 'draft' });
    const published = makeAgent({ status: 'published', version: 'v2' });
    vi.mocked(api.getAgent).mockResolvedValue(draft);
    vi.mocked(api.publishAgent).mockResolvedValue(
      { version: 'v2', entitlement_plan: {}, audited_as: 'AGENT_PUBLISH' } as any);

    renderBuilder();
    await waitFor(() => expect(screen.getByText('draft')).toBeInTheDocument());

    // Open the publish panel from the Builder's own toolbar.
    fireEvent.click(screen.getByText('Publish'));
    await waitFor(() => expect(screen.getByText('Publish Update')).toBeInTheDocument());

    // From here on the server reports it published.
    vi.mocked(api.getAgent).mockResolvedValue(published);
    fireEvent.click(screen.getByText('Publish Update'));

    await waitFor(() => expect(api.publishAgent).toHaveBeenCalled());
    // The chip lives in the Builder, not the modal — this is the part
    // that used to need a page reload.
    await waitFor(() => expect(screen.getByText('published')).toBeInTheDocument());
  });

  it('does not throw the Try it panel over the publish dialog', async () => {
    // Publishing is not "entering the playground". Keying the auto-open
    // on the status alone made it fire the moment the agent became
    // published, opening a second overlay on top of the one just used.
    const draft = makeAgent({ status: 'draft' });
    vi.mocked(api.getAgent).mockResolvedValue(draft);
    vi.mocked(api.publishAgent).mockResolvedValue(
      { version: 'v2', entitlement_plan: {}, audited_as: 'AGENT_PUBLISH' } as any);

    renderBuilder();
    await waitFor(() => expect(screen.getByText('draft')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Publish'));
    await waitFor(() => expect(screen.getByText('Publish Update')).toBeInTheDocument());

    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published', version: 'v2' }));
    fireEvent.click(screen.getByText('Publish Update'));

    await waitFor(() => expect(screen.getByText('published')).toBeInTheDocument());
    expect(screen.queryByText(/Try it — Flow/)).not.toBeInTheDocument();
  });
});

/**
 * Entering a published agent's playground leads with the app — once.
 * It used to be set inside load(), so every mount reopened it and a
 * refresh brought it back every single time.
 */
describe('the Try it panel on a published agent', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    base();
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published', version: 'v2' }));
  });

  it('opens on the first visit', async () => {
    renderBuilder();
    await waitFor(() => expect(screen.getByText(/Try it — Flow/)).toBeInTheDocument());
  });

  it('does not reopen on a reload within the same session', async () => {
    const first = renderBuilder();
    await waitFor(() => expect(screen.getByText(/Try it — Flow/)).toBeInTheDocument());
    first.unmount();          // a page refresh is a fresh mount
    cleanup();

    renderBuilder();
    await waitFor(() => expect(screen.getByText('published')).toBeInTheDocument());
    expect(screen.queryByText(/Try it — Flow/)).not.toBeInTheDocument();
  });

  it('opens again in a genuinely new session', async () => {
    const first = renderBuilder();
    await waitFor(() => expect(screen.getByText(/Try it — Flow/)).toBeInTheDocument());
    first.unmount();
    cleanup();
    sessionStorage.clear();   // new tab / new session

    renderBuilder();
    await waitFor(() => expect(screen.getByText(/Try it — Flow/)).toBeInTheDocument());
  });

  it('never opens itself for a draft', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('draft')).toBeInTheDocument());
    expect(screen.queryByText(/Try it — Flow/)).not.toBeInTheDocument();
  });

  it('survives sessionStorage being unavailable', async () => {
    // Private windows and blocked site data make these throw; a dead
    // storage API must not take the Builder down.
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      renderBuilder();
      await waitFor(() => expect(screen.getByText('published')).toBeInTheDocument());
    } finally {
      spy.mockRestore();
    }
  });
});
