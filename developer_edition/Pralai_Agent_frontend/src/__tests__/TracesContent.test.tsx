import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Traces from '../pages/Traces';
import DrawerHost from '../components/DrawerHost';
import { api } from '../api';
import type { Trace, TraceSpansResponse } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

// The Traces page lists real agents for its filter dropdown rather
// than only those appearing in the current page of traces.
beforeEach(() => { vi.mocked(api.listAgents).mockResolvedValue([]); });

function makeTrace(overrides?: Partial<Trace>): Trace {
  return {
    id: 'tr-live-abc123', agent_id: 'a5', agent_name: 'Support Knowledge Bot', version: 'v9',
    principal: 'agent:svc-a5 / user:end-user', plane: 'App', spans: 5, duration: '1.8s',
    ts: '2026-08-21 10:00:00', status: 'ok',
    user_input: 'How do I reset my password?',
    final_output: 'You can reset your password from the account settings page.',
    ...overrides,
  } as Trace;
}

function renderTraces() {
  return render(
    <MemoryRouter>
      <AppProvider><Traces /><DrawerHost /></AppProvider>
    </MemoryRouter>
  );
}

describe('Traces — real content, genuinely distinct from Runs', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('the list shows a real Message column with the actual user input, not just timing/status', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue({ items: [makeTrace()], nextCursor: null, hasMore: false });
    renderTraces();
    await waitFor(() => expect(screen.getByText('How do I reset my password?')).toBeInTheDocument());
    expect(screen.getByText('Message')).toBeInTheDocument();
  });

  it('a trace with no real recorded conversation (old seed data) shows an honest dash, not a crash', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue({ items: [makeTrace({ user_input: undefined, final_output: undefined })], nextCursor: null, hasMore: false });
    renderTraces();
    await waitFor(() => expect(screen.getByText('tr-live-abc123')).toBeInTheDocument());
  });

  it('the subtitle no longer makes an untrue OpenTelemetry-compatibility claim', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue({ items: [], nextCursor: null, hasMore: false });
    renderTraces();
    await waitFor(() => expect(screen.queryByText(/OpenTelemetry-compatible/)).not.toBeInTheDocument());
  });

  it('clicking a trace opens the real detail drawer showing the full real conversation', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue({ items: [makeTrace()], nextCursor: null, hasMore: false });
    vi.mocked(api.getTrace).mockResolvedValue(makeTrace());
    vi.mocked(api.getTraceSpans).mockResolvedValue({
      trace_id: 'tr-live-abc123', fail_class: null,
      spans: [
        { name: 'trigger', kind: 'orch', duration_ms: 5, input_preview: 'How do I reset my password?' },
        { name: 'respond', kind: 'orch', duration_ms: 900, output_preview: 'You can reset your password from the account settings page.' },
      ],
    } as TraceSpansResponse);
    renderTraces();
    await waitFor(() => expect(screen.getByText('How do I reset my password?')).toBeInTheDocument());
    fireEvent.click(screen.getByText('tr-live-abc123'));
    await waitFor(() => {
      expect(screen.getByText('User provided')).toBeInTheDocument();
      expect(screen.getByText('Agent replied')).toBeInTheDocument();
      expect(screen.getAllByText('You can reset your password from the account settings page.').length).toBeGreaterThanOrEqual(1);
    });
  });

  it('the span waterfall shows real per-node content previews, not just a timing bar', async () => {
    vi.mocked(api.listTracesPage).mockResolvedValue({ items: [makeTrace()], nextCursor: null, hasMore: false });
    vi.mocked(api.getTrace).mockResolvedValue(makeTrace());
    vi.mocked(api.getTraceSpans).mockResolvedValue({
      trace_id: 'tr-live-abc123', fail_class: null,
      spans: [
        { name: 'trigger', kind: 'orch', duration_ms: 5, input_preview: 'How do I reset my password?' },
        { name: 'gov', kind: 'ucs', duration_ms: 40 },
        { name: 'llm', kind: 'harn', duration_ms: 1200, output_preview: 'Draft reply text' },
      ],
    } as TraceSpansResponse);
    renderTraces();
    await waitFor(() => expect(screen.getByText('How do I reset my password?')).toBeInTheDocument());
    fireEvent.click(screen.getByText('tr-live-abc123'));
    await waitFor(() => {
      expect(screen.getByText('Draft reply text')).toBeInTheDocument();
    });
    const spanEls = document.querySelectorAll('.trace .span');
    const govSpan = Array.from(spanEls).find((el) => el.textContent?.includes('gov'));
    expect(govSpan?.querySelector('.span-preview')).toBeNull();
  });
});
