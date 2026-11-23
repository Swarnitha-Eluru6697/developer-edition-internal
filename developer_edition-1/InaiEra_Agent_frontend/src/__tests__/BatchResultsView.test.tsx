/**
 * Batch results must stay readable when a row's output is an entire
 * extracted document.
 *
 * The bug: the Output cell rendered the raw text. An extracted document
 * is tens of thousands of characters with long unbroken runs, so one
 * row pushed the table wider than the pane (horizontal overflow) and
 * buried every other row under a screen of text. There was also no way
 * to tell which file a row's output came from — the table showed only
 * a row number.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { Agent, BatchRunResult } from '../types';

vi.mock('../api', () => ({
  api: { uploadFile: vi.fn(), batchRun: vi.fn(), runAgent: vi.fn(), streamAgentRun: vi.fn() },
}));
import { api } from '../api';
import { FormRunner } from '../components/RunnerUI';

const AGENT = {
  id: 'w1', name: 'test workflow', app_type: 'workflow', status: 'published',
  features: {}, input_variables: [],
  flow: { nodes: [{ id: 'n1', type: 'trigger', config: { fields: [
    { id: 'f1', name: 'document', type: 'single_file', required: true },
  ] } }], edges: [] },
} as unknown as Agent;

// A realistic extracted-document payload: long, and with a run that has
// no break opportunity of its own.
const HUGE = '### Attendance\n' + ('| 2023-11-27 00:00:00 | login | logout '.repeat(400));

const RESULT: BatchRunResult = {
  total: 2, succeeded: 2, failed: 0, paused: 0,
  results: [
    { row_index: 0, inputs: { document: 'http://h/api/uploads/u1/download' },
      status: 'success', output: HUGE, structured_output: null },
    { row_index: 1, inputs: { document: 'http://h/api/uploads/u2/download' },
      status: 'success', output: 'short', structured_output: null },
  ],
};

async function runBatchOf(files: string[]) {
  vi.mocked(api.uploadFile).mockImplementation((f: File) =>
    Promise.resolve({ id: f.name, filename: f.name, content_type: 'text/markdown', size: 1,
                      url: `http://h/api/uploads/${f.name}/download` }));
  vi.mocked(api.batchRun).mockResolvedValue(RESULT);

  const r = render(<FormRunner agent={AGENT} appType="workflow" busy={false} setBusy={vi.fn()} invokingUser="user:padmin" />);
  fireEvent.click(screen.getByText('Run Batch'));
  const input = r.container.querySelector('input[type=file]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: files.map((n) => new File(['x'], n)) } });
  await screen.findByText(new RegExp(`${files.length} files? queued`));
  fireEvent.click(screen.getByText(/Run batch/));
  await waitFor(() => expect(api.batchRun).toHaveBeenCalled());
  return r;
}

describe('batch results view', () => {
  beforeEach(() => vi.clearAllMocks());

  it('caps every output cell in its own scroller instead of rendering it raw', async () => {
    const { container } = await runBatchOf(['attendance.md', 'short.md']);
    const cells = container.querySelectorAll('.batch-results-table .so-value');
    // One capped scroller per row, not a single wall of text.
    expect(cells.length).toBe(2);
    for (const c of cells) {
      expect((c as HTMLElement).style.maxHeight).toBe('180px');
    }
  });

  it('hides the scrollbar on each cell rather than showing one per row', async () => {
    await runBatchOf(['attendance.md', 'short.md']);
    // .so-value carries scrollbar-width:none and a ::-webkit-scrollbar
    // reset in styles.css; the class is the contract those rules key off.
    const cell = document.querySelector('.batch-results-table .so-value') as HTMLElement;
    expect(cell.className).toContain('so-value');
  });

  it('fixes the table layout so one long row cannot widen the pane', async () => {
    const { container } = await runBatchOf(['attendance.md']);
    const table = container.querySelector('.batch-results-table') as HTMLElement;
    // Without this an auto-layout table sizes to its widest content no
    // matter what the cells are told to do.
    expect(table.classList.contains('fixed')).toBe(true);
  });

  it('names the file each output came from', async () => {
    const { container } = await runBatchOf(['attendance.md', 'short.md']);
    expect(screen.getByText('Input')).toBeTruthy();
    // Scoped to the results table: the names also appear in the queued
    // list above it, which is a different thing.
    const inputCells = Array.from(container.querySelectorAll('.batch-cell-input'))
      .map((c) => c.textContent);
    expect(inputCells).toEqual(['attendance.md', 'short.md']);
    // The full value is available on hover, since the cell truncates.
    const first = container.querySelector('.batch-cell-input') as HTMLElement;
    expect(first.getAttribute('title')).toBe('attendance.md');
  });

  it('falls back to the row inputs when there are no filenames (CSV batches)', async () => {
    vi.mocked(api.batchRun).mockResolvedValue({
      total: 1, succeeded: 1, failed: 0, paused: 0,
      results: [{ row_index: 0, inputs: { question: 'What is SEC-201?' },
                  status: 'success', output: 'AES-256-GCM', structured_output: null }],
    });
    const textAgent = {
      ...AGENT,
      flow: { nodes: [{ id: 'n1', type: 'trigger', config: { fields: [
        { id: 'f1', name: 'question', type: 'paragraph', required: true },
      ] } }], edges: [] },
    } as unknown as Agent;

    const r = render(<FormRunner agent={textAgent} appType="workflow" busy={false} setBusy={vi.fn()} invokingUser="user:padmin" />);
    fireEvent.click(screen.getByText('Run Batch'));
    const csv = r.container.querySelector('input[accept=".csv"]') as HTMLInputElement;
    const file = new File(['question\nWhat is SEC-201?\n'], 'q.csv', { type: 'text/csv' });
    // jsdom's File has no .text() in every version; supply it.
    Object.defineProperty(file, 'text', { value: () => Promise.resolve('question\nWhat is SEC-201?\n') });
    fireEvent.change(csv, { target: { files: [file] } });
    await screen.findByText(/1 row\(s\) ready to run/);
    fireEvent.click(screen.getByText(/Run batch/));
    await waitFor(() => expect(api.batchRun).toHaveBeenCalled());
    expect(await screen.findByText('What is SEC-201?')).toBeTruthy();
  });
});
