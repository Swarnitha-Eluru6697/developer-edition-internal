import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';
import { grantPermissions } from './grantPermissions';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a1', name: 'Support Bot', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'respond', x: 200, y: 140, sub: '', config: {} },
      ],
      edges: [],
    },
    ...overrides,
  } as Agent;
}

function renderBuilder() {
  return render(
    <MemoryRouter initialEntries={['/builder?agent=a1']}>
      <AppProvider><Builder /></AppProvider>
    </MemoryRouter>
  );
}

describe('Builder — real "Try it" panel, not a toast-based single-shot test', () => {
  beforeEach(() => {
    // FeaturesPanel asks whether THIS request carries a verified
    // identity, so its banner reflects reality rather than asserting.
    vi.mocked(api.whoami).mockResolvedValue({ verified_identity: null, gateway_trust_enabled: false, reason: 'no headers in this test' });
    // ChatRunner replays the stored transcript on mount so a
    // conversation survives closing the app. These tests drive their
    // own messages, so it resolves empty.
    vi.mocked(api.getChatHistory).mockResolvedValue([]);
    vi.clearAllMocks();
    // The auto-open is once per browser session per agent now, so it
    // remembers having been shown. Several tests here mount the Builder
    // on the same agent id, and without clearing that memory the later
    // ones would see it suppressed — testing the memory rather than what
    // they mean to test. A real reload keeping it closed is the point;
    // see PublishStatusRefresh.test.tsx.
    sessionStorage.clear();
    // Try it needs agent:agents:run; everything is granted here.
    grantPermissions();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
  });

  it('opens a real chat interface for a chatbot-type App agent, available while still in draft', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument();
    });
    // honest note that this is a draft, shown right there in the panel.
    // Matched on the banner's own wording rather than the bare word
    // "still", which also occurs in unrelated copy elsewhere in the
    // Builder and made this assertion fire on the wrong element.
    expect(screen.getByText(/testing the same real flow an end user will get/)).toBeInTheDocument();
    expect(screen.getAllByText('draft').length).toBeGreaterThan(0);
  });

  it('sending a message in Try It calls the real /run endpoint tagged as a builder preview, not real end-user traffic', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'r1', trace_id: 't1', execution_mode: 'sync', status: 'success',
      output: 'Real preview reply', spans: [], fail_class: null,
    });
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Type a message…'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() => {
      // The real selected identity. This used to assert
      // 'user:builder-preview', a made-up principal that made every
      // preview of a governed flow fail with "denied: ucs". The final
      // `true` is what marks the run as a builder preview.
      expect(api.runAgent).toHaveBeenCalledWith('a1', 'hello', 'user:developer', undefined, true);
    });
    await waitFor(() => {
      expect(screen.getByText('Real preview reply')).toBeInTheDocument();
    });
  });

  it('shows the real form UI for a workflow-type App agent, not chat', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));

    await waitFor(() => {
      expect(screen.getByText('Execute')).toBeInTheDocument();
    });
    expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
  });

  it('a published agent shows no draft warning in Try It', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));

    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());
    expect(screen.queryByText(/testing the same real flow an end user will get/)).not.toBeInTheDocument();
  });

  it('entering a published agent auto-opens Try It — going inside a deployed agent leads with the real end-user UI', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published' }));
    renderBuilder();
    // no click on "Try it" at all — the panel should already be open on load
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument();
    });
  });

  it('entering a draft agent does NOT auto-open Try It — the canvas is the default view until published', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
  });

  it('Data-type agents get a real form preview too (single input/output), not an error', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ type: 'Data', app_type: undefined }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));

    await waitFor(() => {
      expect(screen.getByText('Execute')).toBeInTheDocument();
    });
  });

  it('closing the modal and reopening does not carry over stale api call state', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));
    await waitFor(() => expect(screen.getByPlaceholderText('Type a message…')).toBeInTheDocument());

    fireEvent.click(document.querySelector('.tryit-modal .x')!);
    await waitFor(() => {
      expect(screen.queryByPlaceholderText('Type a message…')).not.toBeInTheDocument();
    });
  });

  it('the form runner has no "Manage features →" link — Features opens from the panel header only', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    expect(screen.queryByText('Manage features →')).not.toBeInTheDocument();
  });

  it('the header Features button on a workflow agent opens the real Features panel', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ app_type: 'workflow' }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Features'));
    await waitFor(() => expect(document.querySelector('.features-panel')).toBeInTheDocument());
  });

  it('a Data-type agent correctly still has no Manage features link — Data agents have no features concept', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ type: 'Data', app_type: undefined }));
    renderBuilder();
    await waitFor(() => expect(screen.getByText('Try it')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Try it'));
    await waitFor(() => expect(screen.getByText('Execute')).toBeInTheDocument());
    expect(screen.queryByText('Manage features →')).not.toBeInTheDocument();
  });
});
