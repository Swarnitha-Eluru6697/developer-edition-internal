/**
 * Monitoring's eval table was read-only text.
 *
 * It showed a set's name and its latest pass rate and stopped there:
 * the rows were not clickable, so there was no way to see WHICH cases a
 * set applies, what each expects, or which one failed — a "7/8" told
 * you something was wrong without saying what. The payload did not even
 * carry a set id to look them up with.
 *
 * Creating sets was also invisible: the Eval Manager only renders for a
 * single agent, and on "All agents" nothing said so, so the table
 * looked like the whole feature.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { Agent, Eval } from '../types';

vi.mock('../api', () => ({
  api: {
    getMetrics: vi.fn(), listAgents: vi.fn(), getEvals: vi.fn(), getEvalHealth: vi.fn(),
    listEvalCases: vi.fn(), getEvalRun: vi.fn(),
  },
}));
vi.mock('../components/EvalManager', () => ({ default: () => <div>EvalManager</div> }));
vi.mock('../context/AppContext', () => ({ useApp: () => ({ toast: vi.fn() }) }));

import { api } from '../api';
import Monitoring from '../pages/Monitoring';
import { pickOption } from './pickOption';

const AGENT = { id: 'ag1', name: 'workflow process', plane: 'Control' } as unknown as Agent;

const ROW: Eval = {
  agent: 'workflow process', set: 'ACME control & support golden set',
  passed: '7/8', rate: '87.5%', drift: 'none', ts: '2026-09-22 06:36',
  set_id: 'es_1', agent_id: 'ag1', run_id: 'er_1',
};

const CASES = [
  { id: 'c1', eval_set_id: 'es_1', input: 'What encryption does SEC-201 mandate?', expected_output: 'AES-256-GCM', criteria: null },
  { id: 'c2', eval_set_id: 'es_1', input: 'Refund window for an annual plan?', expected_output: '14-day', criteria: null },
  { id: 'c3', eval_set_id: 'es_1', input: 'Summarise the control catalogue', expected_output: '', criteria: 'Is the summary faithful?' },
];

const RESULTS = [
  { id: 1, eval_run_id: 'er_1', eval_case_id: 'c1', actual_output: 'AES-256-GCM at rest', passed: true, score: 1, reason: '' },
  { id: 2, eval_run_id: 'er_1', eval_case_id: 'c2', actual_output: 'Annual plans have a 14 day window', passed: false, score: 0, reason: 'expected substring not found' },
];

async function setup(filter: 'all' | 'ag1' = 'all', rows: unknown[] = [ROW]) {
  vi.mocked(api.listAgents).mockResolvedValue([AGENT]);
  vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'platform', metrics: { sample_size: 5, window_days: 7 } } as never);
  vi.mocked(api.getEvals).mockResolvedValue(rows as never);
  vi.mocked(api.getEvalHealth).mockResolvedValue(null as never);
  vi.mocked(api.listEvalCases).mockResolvedValue(CASES as never);
  vi.mocked(api.getEvalRun).mockResolvedValue({
    run: { id: 'er_1', eval_set_id: 'es_1', agent_id: 'ag1', status: 'completed',
           started_at: '', completed_at: '', pass_count: 7, total_count: 8,
           drift: 'none', agent_version: 'v5' },
    results: RESULTS,
  } as never);
  const r = render(<Monitoring />);
  // The filter argument was accepted and then never applied, so every
  // call behaved as 'all' — including the ones written to test the
  // single-agent path.
  //
  // It is now driven the way someone would: open the dropdown and click
  // the name. The agent list arrives from a promise, so the wait is not
  // optional — the old fireEvent.change ran before the options existed.
  if (filter !== 'all') {
    fireEvent.click(await screen.findByLabelText('Filter by agent'));
    fireEvent.click(await screen.findByRole('option', { name: 'workflow process' }));
  }
  // Results moved behind its own tab — managing sets and reading results
  // used to be two stacked blocks that ran past the fold.
  fireEvent.click(screen.getByText('Results'));
  return r;
}

describe('eval set popup', () => {
  beforeEach(() => vi.clearAllMocks());

  it('opens the cases when a row is clicked', async () => {
    await setup();
    const cell = await screen.findByText('ACME control & support golden set');
    fireEvent.click(cell);
    await waitFor(() => expect(api.listEvalCases).toHaveBeenCalledWith('es_1'));
    expect(await screen.findByText('What encryption does SEC-201 mandate?')).toBeTruthy();
    expect(screen.getByText('Refund window for an annual plan?')).toBeTruthy();
  });

  it('marks which case passed and which failed, with the reason', async () => {
    await setup();
    fireEvent.click(await screen.findByText('ACME control & support golden set'));
    await screen.findByText('What encryption does SEC-201 mandate?');
    expect(screen.getByText('pass')).toBeTruthy();
    expect(screen.getByText('fail')).toBeTruthy();
    // The failure has to say WHY — that was the whole gap.
    expect(screen.getByText('expected substring not found')).toBeTruthy();
    expect(screen.getByText('Annual plans have a 14 day window')).toBeTruthy();
  });

  it('distinguishes a substring case from an LLM-judged one', async () => {
    await setup();
    fireEvent.click(await screen.findByText('ACME control & support golden set'));
    await screen.findByText('What encryption does SEC-201 mandate?');
    expect(screen.getAllByText(/substring match/).length).toBeGreaterThan(0);
    expect(screen.getByText('Is the summary faithful?')).toBeTruthy();
  });

  it('shows a case that has never been run as such, rather than as a failure', async () => {
    await setup();
    fireEvent.click(await screen.findByText('ACME control & support golden set'));
    // c3 has no result row in this run.
    expect(await screen.findByText('not yet run')).toBeTruthy();
  });

  it('records which published version the run scored against', async () => {
    await setup();
    fireEvent.click(await screen.findByText('ACME control & support golden set'));
    expect(await screen.findByText('v5')).toBeTruthy();
  });

  it('says why sets cannot be created while scoped to all agents', async () => {
    // A set belongs to one agent, so there is nothing coherent to create
    // here. The guidance now sits on the Eval sets tab — where someone
    // goes to create one — rather than under the results table.
    await setup('all');
    fireEvent.click(screen.getByText('Eval sets'));
    expect(await screen.findByText(/Pick a single agent in the filter above/)).toBeTruthy();
  });

  it('shows one section with two tabs, not two stacked blocks', async () => {
    // Managing sets and reading results each had their own heading and
    // prose, so on a single agent the page ran well past the fold before
    // showing a single result.
    await setup('ag1');
    fireEvent.click(screen.getByText('Eval sets'));
    // The results table is not on screen while managing sets…
    await screen.findByText('EvalManager');
    await waitFor(() => expect(screen.queryByText('Pass rate')).toBeNull());
    fireEvent.click(screen.getByText('Results'));
    // …and the manager is not on screen while reading results. (The
    // manager is stubbed in this file, so assert on the stub — asserting
    // the absence of its internals would pass no matter what.)
    await screen.findByText('Pass rate');
    expect(screen.queryByText('EvalManager')).toBeNull();
  });

  it('pages the results and states the exact total', async () => {
    // This list is computed per eval set rather than selected, so the
    // whole set is already in hand — which means the pager can show a
    // real total, unlike the keyset pagers elsewhere.
    await setup('all', Array.from({ length: 23 }, (_, i) => ({ ...ROW, set: `Set ${i}` })));
    await screen.findByText(/Showing 1–10 of 23/);
    expect(screen.getByText('Page 1 of 3')).toBeTruthy();
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText(/Showing 11–20 of 23/);
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText(/Showing 21–23 of 23/);
    expect(screen.getByText('Next ›').closest('button')).toBeDisabled();
  });

  it('does not strand you on a page that no longer exists', async () => {
    // Filtering to an agent with fewer sets while on page 3 would
    // otherwise show an empty table with no way back.
    await setup('all', Array.from({ length: 23 }, (_, i) => ({ ...ROW, set: `Set ${i}` })));
    await screen.findByText(/Showing 1–10 of 23/);
    fireEvent.click(screen.getByText('Next ›'));
    fireEvent.click(screen.getByText('Next ›'));
    await screen.findByText('Page 3 of 3');
    // The page size changes, so page 3 no longer exists.
    pickOption(screen.getByRole('button', { name: 'Rows per page' }), '50');
    await screen.findByText('Page 1 of 1');
    await screen.findByText(/Showing 1–23 of 23/);
  });

  it('the eval tab bar does not stick to the page header', async () => {
    // .wrap > .tabs is sticky with a 20px shadow block — written for a
    // page's PRIMARY tab bar. A secondary bar partway down inherited it
    // and painted over the text directly beneath.
    await setup('ag1');
    const tabs = screen.getByText('Eval sets').closest('.tabs') as HTMLElement;
    expect(tabs.className).toContain('inline');
  });
});
