/**
 * Run Batch for a workflow whose only input is a file.
 *
 * The bug: the batch tab always offered a CSV template. For a trigger
 * with one `single_file` field that sent you in a circle — the template
 * asked for a `document` column, and nothing you can type into a CSV
 * cell is something the Doc Extractor can fetch. The only way through
 * was to upload each file out-of-band and paste its URL, which the UI
 * never said and gave you no way to do.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { Agent } from '../types';

vi.mock('../api', () => ({
  api: { uploadFile: vi.fn(), batchRun: vi.fn(), runAgent: vi.fn(), streamAgentRun: vi.fn() },
}));
import { api } from '../api';
import { FormRunner } from '../components/RunnerUI';

function agentWith(fields: unknown[]): Agent {
  return {
    id: 'w1', name: 'test workflow', app_type: 'workflow', status: 'published',
    features: {}, input_variables: [],
    flow: { nodes: [{ id: 'n1', type: 'trigger', config: { fields } }], edges: [] },
  } as unknown as Agent;
}

const FILE_ONLY = agentWith([{ id: 'f1', name: 'document', type: 'single_file', required: true }]);
const MIXED = agentWith([
  { id: 'f1', name: 'document', type: 'single_file', required: true },
  { id: 'f2', name: 'reviewer', type: 'text', required: true },
]);
const TEXT_ONLY = agentWith([{ id: 'f1', name: 'question', type: 'paragraph', required: true }]);

function mount(agent: Agent) {
  const r = render(<FormRunner agent={agent} appType="workflow" busy={false} setBusy={vi.fn()} invokingUser="user:padmin" />);
  fireEvent.click(screen.getByText('Run Batch'));
  return r;
}

describe('batching a file-only workflow', () => {
  beforeEach(() => vi.clearAllMocks());

  it('offers a file picker instead of a CSV template', () => {
    mount(FILE_ONLY);
    expect(screen.queryByText('Download CSV template')).toBeNull();
    expect(screen.getByText(/Add files to run \(document\)/)).toBeTruthy();
    expect(screen.getByText(/each one becomes a real, independent run/i)).toBeTruthy();
  });

  it('uploads every picked file and makes one row per file', async () => {
    vi.mocked(api.uploadFile)
      .mockResolvedValueOnce({ id: 'u1', filename: 'a.md', content_type: 'text/markdown', size: 1, url: 'http://h/api/uploads/u1/download' })
      .mockResolvedValueOnce({ id: 'u2', filename: 'b.md', content_type: 'text/markdown', size: 1, url: 'http://h/api/uploads/u2/download' });

    const { container } = mount(FILE_ONLY);
    const input = container.querySelector('input[type=file]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['a'], 'a.md'), new File(['b'], 'b.md')] },
    });

    await waitFor(() => expect(api.uploadFile).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/2 files queued/)).toBeTruthy();
    expect(screen.getByText('a.md')).toBeTruthy();
    expect(screen.getByText('b.md')).toBeTruthy();
    expect(screen.getByText(/Run batch \(2 rows\)/)).toBeTruthy();
  });

  it('adds to the list on a second pick instead of replacing it', async () => {
    // The regression: a file picker returns one dialog's worth of files,
    // and most dialogs cannot reach across folders, so replacing the
    // list made a multi-folder batch impossible — each pick silently
    // threw away the previous one.
    vi.mocked(api.uploadFile).mockImplementation((f: File) =>
      Promise.resolve({ id: f.name, filename: f.name, content_type: 'text/markdown', size: 1,
                        url: `http://h/api/uploads/${f.name}/download` }));

    const { container } = mount(FILE_ONLY);
    const input = container.querySelector('input[type=file]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [new File(['a'], 'a.md'), new File(['b'], 'b.md')] } });
    expect(await screen.findByText(/2 files queued/)).toBeTruthy();

    fireEvent.change(input, { target: { files: [new File(['c'], 'c.md')] } });
    expect(await screen.findByText(/3 files queued/)).toBeTruthy();

    for (const n of ['a.md', 'b.md', 'c.md']) expect(screen.getByText(n)).toBeTruthy();
    expect(screen.getByText(/Run batch \(3 rows\)/)).toBeTruthy();
  });

  it('clears the input so re-picking the same file still registers', async () => {
    vi.mocked(api.uploadFile).mockImplementation((f: File) =>
      Promise.resolve({ id: f.name, filename: f.name, content_type: 'text/markdown', size: 1,
                        url: `http://h/api/uploads/${f.name}/download` }));
    const { container } = mount(FILE_ONLY);
    const input = container.querySelector('input[type=file]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(['a'], 'a.md')] } });
    await screen.findByText(/1 file queued/);
    // A file input fires no change event when its value is unchanged, so
    // the handler must reset it after reading the selection.
    expect(input.value).toBe('');
  });

  it('removes one queued file without disturbing the rest', async () => {
    vi.mocked(api.uploadFile).mockImplementation((f: File) =>
      Promise.resolve({ id: f.name, filename: f.name, content_type: 'text/markdown', size: 1,
                        url: `http://h/api/uploads/${f.name}/download` }));
    const { container } = mount(FILE_ONLY);
    const input = container.querySelector('input[type=file]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['a'], 'a.md'), new File(['b'], 'b.md'), new File(['c'], 'c.md')] },
    });
    await screen.findByText(/3 files queued/);

    fireEvent.click(screen.getByLabelText('Remove b.md'));
    expect(await screen.findByText(/2 files queued/)).toBeTruthy();
    expect(screen.getByText('a.md')).toBeTruthy();
    expect(screen.getByText('c.md')).toBeTruthy();
    expect(screen.queryByText('b.md')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    await waitFor(() => expect(screen.queryByText(/files queued/)).toBeNull());
  });

  it('refuses a selection that would exceed the row cap, counting what is already queued', async () => {
    vi.mocked(api.uploadFile).mockImplementation((f: File) =>
      Promise.resolve({ id: f.name, filename: f.name, content_type: 'text/markdown', size: 1,
                        url: `http://h/api/uploads/${f.name}/download` }));
    const { container } = mount(FILE_ONLY);
    const input = container.querySelector('input[type=file]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [new File(['a'], 'a.md')] } });
    await screen.findByText(/1 file queued/);
    vi.mocked(api.uploadFile).mockClear();

    const many = Array.from({ length: 200 }, (_, i) => new File(['x'], `f${i}.md`));
    fireEvent.change(input, { target: { files: many } });
    // Rejected before a single upload, not after 200 of them.
    expect(await screen.findByText(/would make 201 files/)).toBeTruthy();
    expect(screen.getByText(/you can add 199 more/)).toBeTruthy();
    expect(api.uploadFile).not.toHaveBeenCalled();
  });
});

describe('batching a workflow that mixes a file field with text', () => {
  beforeEach(() => vi.clearAllMocks());

  it('keeps the CSV but says the file column needs a URL, and helps produce one', async () => {
    vi.mocked(api.uploadFile).mockResolvedValue({
      id: 'u9', filename: 'spec.md', content_type: 'text/markdown', size: 2,
      url: 'http://h/api/uploads/u9/download',
    });
    const { container } = mount(MIXED);
    expect(screen.getByText('Download CSV template')).toBeTruthy();
    expect(screen.getByText(/needs a fetchable\s+URL/)).toBeTruthy();

    // Two file inputs in this layout: the URL helper, then the CSV.
    const fileInputs = container.querySelectorAll('input[type=file]');
    expect(fileInputs.length).toBe(2);
    expect((fileInputs[1] as HTMLInputElement).accept).toBe('.csv');

    fireEvent.change(fileInputs[0], { target: { files: [new File(['s'], 'spec.md')] } });
    expect(await screen.findByText('http://h/api/uploads/u9/download')).toBeTruthy();
  });
});

describe('a text-only workflow is untouched', () => {
  it('still uses the CSV template with no file guidance', () => {
    mount(TEXT_ONLY);
    expect(screen.getByText('Download CSV template')).toBeTruthy();
    expect(screen.queryByText(/Add files to run/)).toBeNull();
    expect(screen.queryByText(/fetchable/)).toBeNull();
  });
});
