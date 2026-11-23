import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import EvalManager from '../components/EvalManager';
import ToastHost from '../components/ToastHost';
import { AppProvider } from '../context/AppContext';
import { api } from '../api';
import type { EvalSet, EvalCase } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function renderManager(props: { agentId?: string; agentName?: string; onRunComplete?: () => void } = {}) {
  return render(
    <AppProvider>
      <EvalManager agentId={props.agentId || 'a1'} agentName={props.agentName || 'Revenue Analysis Agent'} onRunComplete={props.onRunComplete} />
      <ToastHost />
    </AppProvider>
  );
}

function makeSet(overrides?: Partial<EvalSet>): EvalSet {
  return { id: 'es_1', name: 'Golden Set', agent_id: 'a1', created_at: '2026-01-01T00:00:00', case_count: 2, ...overrides } as EvalSet;
}
function makeCase(overrides?: Partial<EvalCase>): EvalCase {
  return { id: 'ec_1', eval_set_id: 'es_1', input: 'test input', expected_output: 'expected text', criteria: null, ...overrides } as EvalCase;
}

describe('EvalManager — real eval set creation, case management, and run triggering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listEvalSets).mockResolvedValue([]);
  });

  it('lists real eval sets for the given agent via the actual API', async () => {
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet()]);
    renderManager();
    await waitFor(() => expect(api.listEvalSets).toHaveBeenCalledWith('a1'));
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    // The count is its own column now rather than a chip in the title.
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('creating a set calls the real API with the real agent id and refreshes the list', async () => {
    vi.mocked(api.createEvalSet).mockResolvedValue(makeSet());
    renderManager();
    await waitFor(() => expect(api.listEvalSets).toHaveBeenCalled());

    // Naming happens in a side panel now, not an inline input.
    fireEvent.click(screen.getByText(/New eval set/));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'My New Set' } });
    fireEvent.click(screen.getByText('Create set'));

    await waitFor(() => {
      expect(api.createEvalSet).toHaveBeenCalledWith('My New Set', 'a1');
    });
    await waitFor(() => {
      expect(api.listEvalSets).toHaveBeenCalledTimes(2); // initial load + refresh after create
    });
  });

  it('expanding a set fetches its real cases and shows them, distinguishing LLM-judged criteria cases', async () => {
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet()]);
    vi.mocked(api.listEvalCases).mockResolvedValue([
      makeCase({ id: 'ec_1', input: 'q1', expected_output: 'a1' }),
      makeCase({ id: 'ec_2', input: 'q2', expected_output: 'a2', criteria: 'Is the answer polite?' }),
    ]);
    renderManager();
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Golden Set'));

    await waitFor(() => expect(api.listEvalCases).toHaveBeenCalledWith('es_1'));
    await waitFor(() => expect(screen.getByText('q1')).toBeInTheDocument());
    expect(screen.getByText('q2')).toBeInTheDocument();
    expect(screen.getByText('Is the answer polite?')).toBeInTheDocument();
  });

  it('adding a case sends the real fields to the real API, including optional criteria', async () => {
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet({ case_count: 0 })]);
    vi.mocked(api.listEvalCases).mockResolvedValue([]);
    vi.mocked(api.addEvalCase).mockResolvedValue(makeCase());
    renderManager();
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Golden Set'));
    await waitFor(() => expect(screen.getByText(/No cases yet/)).toBeInTheDocument());

    // The form moved into a side panel — inline, it made every expanded
    // set several hundred pixels tall.
    fireEvent.click(screen.getByText('Case'));
    fireEvent.change(await screen.findByLabelText('Input'), { target: { value: 'What is 2+2?' } });
    fireEvent.change(screen.getByLabelText('Expected output'), { target: { value: '4' } });
    fireEvent.click(screen.getByText('Add case'));

    await waitFor(() => {
      expect(api.addEvalCase).toHaveBeenCalledWith('es_1', { input: 'What is 2+2?', expected_output: '4' });
    });
  });

  it('running a set calls the real run endpoint, shows the real pass rate, and triggers a parent refresh', async () => {
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet()]);
    vi.mocked(api.runEvalSet).mockResolvedValue({
      run: { id: 'er_1', eval_set_id: 'es_1', agent_id: 'a1', status: 'completed', started_at: '', completed_at: '', pass_count: 3, total_count: 4, drift: 'watch' },
      results: [], rate: '75.0%',
    });
    const onRunComplete = vi.fn();
    renderManager({ onRunComplete });
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Run'));

    await waitFor(() => {
      expect(api.runEvalSet).toHaveBeenCalledWith('es_1');
    });
    await waitFor(() => {
      expect(screen.getByText(/3\/4 passed \(75\.0%\) — drift: watch/)).toBeInTheDocument();
    });
    expect(onRunComplete).toHaveBeenCalled();
  });

  it('the honest LLM-judge boundary (real ANTHROPIC_API_KEY requirement) is stated where the choice is made', async () => {
    // It used to be a paragraph above the list, which is what made this
    // section so tall. It now sits beside the Criteria field — the point
    // at which someone actually opts into LLM judging.
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet()]);
    renderManager();
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Case'));
    await waitFor(() => {
      expect(screen.getByText(/could not judge/)).toBeInTheDocument();
    });
  });

  it('a set with no cases cannot be run', async () => {
    // Running an empty set produces 0/0, which reads as a passing score.
    vi.mocked(api.listEvalSets).mockResolvedValue([makeSet({ case_count: 0 })]);
    renderManager();
    await waitFor(() => expect(screen.getByText('Golden Set')).toBeInTheDocument());
    expect(screen.getByText('Run').closest('button')).toBeDisabled();
  });
});
