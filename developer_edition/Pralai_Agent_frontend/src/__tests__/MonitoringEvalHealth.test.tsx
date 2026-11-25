import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Monitoring from '../pages/Monitoring';
import { api } from '../api';
import type { Agent, EvalHealth } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a5', name: 'Support Knowledge Bot', type: 'App', subtype: 'Conversational', plane: 'App',
    owner: 'you', status: 'published', version: 'v9', health_score: 95, health_status: 'pass',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderMonitoring() {
  return render(
    <MemoryRouter>
      <AppProvider><Monitoring /></AppProvider>
    </MemoryRouter>
  );
}

describe('Monitoring — Agent health cards now computed from real eval data, not hardcoded strings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([makeAgent()]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'platform', metrics: {} });
    vi.mocked(api.getEvals).mockResolvedValue([]);
  });

  it('shows the real average pass rate returned by the backend, not the old hardcoded 89%', async () => {
    vi.mocked(api.getEvalHealth).mockResolvedValue({
      avg_pass_rate: 62.5, trend_delta_pts: null, trend_note: 'not enough data', drift_flags: [],
    } as EvalHealth);
    renderMonitoring();
    await waitFor(() => expect(screen.getByText('62.5%')).toBeInTheDocument());
    expect(screen.queryByText('89%')).not.toBeInTheDocument();
  });

  it('an honest dash and empty-state note shows when there are no completed eval runs yet', async () => {
    vi.mocked(api.getEvalHealth).mockResolvedValue({
      avg_pass_rate: null, trend_delta_pts: null, trend_note: 'no completed runs yet', drift_flags: [],
    } as EvalHealth);
    renderMonitoring();
    await waitFor(() => expect(screen.getByText('no completed runs yet')).toBeInTheDocument());
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('the real week-over-week trend renders with a real direction arrow, not a hardcoded "▲ 2.1pt"', async () => {
    vi.mocked(api.getEvalHealth).mockResolvedValue({
      avg_pass_rate: 80, trend_delta_pts: -5.5, trend_note: null, drift_flags: [],
    } as EvalHealth);
    renderMonitoring();
    await waitFor(() => expect(screen.getByText('▼ 5.5pt')).toBeInTheDocument());
  });

  it('real drift flags show the real agent name from the backend, not the hardcoded "Churn Model Scientist"', async () => {
    vi.mocked(api.getEvalHealth).mockResolvedValue({
      avg_pass_rate: 70, trend_delta_pts: 1.2, trend_note: null,
      drift_flags: [{ agent_name: 'Vendor Onboarding Automation', set_name: 'Regression set', drift: 'flagged' }],
    } as EvalHealth);
    renderMonitoring();
    await waitFor(() => expect(screen.getByText('Vendor Onboarding Automation')).toBeInTheDocument());
    const card = screen.getByText('Behavioral drift flags').closest('.card')!;
    expect(card.querySelector('.v')?.textContent).toBe('1');
  });

  it('the real endpoint is called with the current agent filter, matching the rest of the page', async () => {
    vi.mocked(api.getEvalHealth).mockResolvedValue({
      avg_pass_rate: null, trend_delta_pts: null, trend_note: 'no eval sets', drift_flags: [],
    } as EvalHealth);
    renderMonitoring();
    await waitFor(() => expect(api.getEvalHealth).toHaveBeenCalledWith('all'));
  });
});
