import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import RAG from '../pages/RAG';
import { api } from '../api';
import type { KnowledgeBase } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');

afterEach(() => cleanup());

function makeKB(config: Record<string, any> = {}): KnowledgeBase {
  return {
    id: 'support_articles', name: 'Support Articles', provider: 'pgvector',
    config: { host: '127.0.0.1', port: 5432, dbname: 'customer_kb', user: 'kb_user',
              table: 'support_articles', upload_api_enabled: true, ...config },
    status: 'connected', last_error: null, last_checked_at: null, created_at: '2026-01-01T00:00:00',
  } as KnowledgeBase;
}

const githubSource = {
  name: 'GitHub', version: '1.1.0', status: 'connected' as const, auth: 'apikey' as const,
  config: { credentials: '••••••••' }, browsable: true, browse_supported: true,
};
const sharepointSource = {
  name: 'SharePoint', version: '1.0.0', status: 'connected' as const, auth: 'oauth' as const,
  config: { client_id: 'abc' }, browsable: true, browse_supported: true,
};
const entry = (over: Partial<any>) => ({
  id: 'x', name: 'x', is_dir: false, path: '', size: 4096, mime: null,
  ingestible: true, note: '', ...over,
});

/** Opens API → Upload with a revealed key, which is what gates the
 *  example request and the "Try it" panel. */
async function openUploadTab() {
  render(<MemoryRouter><AppProvider><RAG /></AppProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
  fireEvent.click(screen.getByText('Support Articles'));
  await waitFor(() => expect(document.querySelector('.tab')).toBeInTheDocument());
  fireEvent.click([...document.querySelectorAll('.tab')].find((t) => t.textContent === 'API')!);
  await waitFor(() => expect(screen.getByText('Upload')).toBeInTheDocument());
  fireEvent.click([...document.querySelectorAll('.tab')].find((t) => t.textContent === 'Upload')!);
  await waitFor(() => expect(screen.getByText('Upload API')).toBeInTheDocument());
  fireEvent.click(screen.getByText(/Generate key|Regenerate key/));
  await waitFor(() => expect(screen.getByDisplayValue('up-key-xyz')).toBeInTheDocument());
}

/** The single-file picker is the default (both modes resolve a full path
 *  now, and it is the conventional control). Calling this is harmless
 *  belt-and-braces for tests that depend on that mode. */
function useSingleFileMode() {
  fireEvent.click(screen.getByLabelText(/Pick one file/));
}

/** Tests about the directory picker must select it explicitly. */
function useFolderMode() {
  fireEvent.click(screen.getByLabelText(/Pick a folder/));
}

const snippet = () => (document.querySelector('.code-block, pre') as HTMLElement)?.textContent || '';

describe('Upload API — pushing documents in from a local file or a connected Data Source', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listKbMetadataFields).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB()]);
    vi.mocked(api.listKbDocuments).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.regenerateKbUploadApiKey).mockResolvedValue(makeKB({ upload_api_key: 'up-key-xyz' }));
    // Audio ingestion asks the server whether a speech provider is
    // configured. Default to none, so the base expectations describe a
    // deployment with no transcription; the audio tests override it.
    vi.mocked(api.getTranscriptionAvailability).mockResolvedValue(
      { available: false, providers: [], extensions: ['wav', 'mp3', 'm4a', 'flac', 'ogg', 'oga', 'mpga'],
        reason: 'No OpenAI or Gemini key is configured.' });
    // Staging a file writes it to real storage, so the panel asks where
    // that is before offering the button.
    vi.mocked(api.getStorageTarget).mockResolvedValue({
      backend: 's3', configured: true, label: 'S3 · test-bucket', bucket: 'test-bucket',
      endpoint_url: null, retention_days: 30, destination_id: 'env',
      destinations: [{ id: 'env', name: 'Server configuration (.env)', provider: 's3',
                       bucket: 'test-bucket', region: null, endpoint_url: null, configured: true,
                       label: 'S3 · test-bucket', editable: false, is_default: true }],
      can_store_credentials: true });
    // A folder pick asks the API where that folder really is. Default
    // to not-found, which is the container/remote case and keeps the
    // guessed-root wording; individual tests override it.
    vi.mocked(api.resolveLocalPath).mockResolvedValue({ found: false, reason: 'not found' });
    // This jsdom build ships no localStorage at all (the panel guards
    // for exactly that and falls back to an empty prefix), so a minimal
    // in-memory one stands in to exercise the remembering itself. Fresh
    // per test, or one test's prefix would leak into the next.
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v); },
      removeItem: (k: string) => { store.delete(k); },
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() { return store.size; },
    });
  });

  it('with no sources connected there is no dropdown, and a local file can only contribute its name', async () => {
    await openUploadTab();
    useSingleFileMode();
    expect(screen.queryByLabelText('Source')).not.toBeInTheDocument();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['x'], 'faq.pdf', { type: 'application/pdf' });
    Object.defineProperty(input, 'files', { value: [file] });
    fireEvent.change(input);
    await waitFor(() => expect(snippet()).toContain('-F "file=@faq.pdf"'));
    // The server is asked for the real path; when it finds nothing
    // (stubbed found:false here, as in a container) the panel says so
    // and points at the alternatives rather than guessing.
    expect(screen.getByText(/found no file by that name/)).toBeInTheDocument();
    await waitFor(() => expect(api.resolveLocalPath).toHaveBeenCalledWith('faq.pdf', expect.anything()));
    // And it must not invent one: prefixing an OS root onto a bare
    // filename would print a confidently wrong path.
    expect(snippet()).not.toContain('/Users/faq.pdf');
    expect(snippet()).not.toContain('/home/faq.pdf');
  });

  it('a typed folder completes the local path in the command, and is remembered for next time', async () => {
    await openUploadTab();
    useSingleFileMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'faq.pdf')] });
    fireEvent.change(input);
    await waitFor(() => expect(snippet()).toContain('faq.pdf'));
    fireEvent.change(screen.getByLabelText(/Folder holding/), { target: { value: '~/Downloads/' } });
    await waitFor(() => expect(snippet()).toContain('-F "file=@~/Downloads/faq.pdf"'));
    expect(localStorage.getItem('inaiera.uploadApi.fileDir')).toBe('~/Downloads/');
  });

  it('the server resolves where a picked folder really is, replacing the guessed root', async () => {
    // The browser supplies "audio/incident.vtt" but never the root, so
    // the guess produced `/Users/audio/incident.vtt` — absolute-looking
    // and pointing nowhere. The API runs on the same machine in local
    // dev, so it can simply look.
    vi.mocked(api.resolveLocalPath).mockResolvedValue({
      found: true,
      root: '/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets',
      absolute_path: '/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets/audio/incident.vtt',
    });
    await openUploadTab();
    useFolderMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const f = new File(['x'], 'incident.vtt');
    Object.defineProperty(f, 'webkitRelativePath', { value: 'audio/incident.vtt' });
    Object.defineProperty(input, 'files', { value: [f] });
    fireEvent.change(input);

    await waitFor(() => expect(api.resolveLocalPath).toHaveBeenCalledWith('audio/incident.vtt', expect.anything()));
    await waitFor(() => expect(snippet()).toContain(
      '-F "file=@/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets/audio/incident.vtt"'));
    // A disk-verified root is not presented the same way as a guess.
    expect(screen.getByText(/verified on disk/)).toBeInTheDocument();
    expect(screen.queryByText(/assumed, not known/)).not.toBeInTheDocument();
  });

  it('a folder pick lists audio only when the server can transcribe it', async () => {
    // The folder picker filters to files this pipeline can actually
    // ingest. Audio belongs in that list exactly when a speech
    // provider is configured, and not before.
    vi.mocked(api.getTranscriptionAvailability).mockResolvedValue({
      available: true, providers: ['OpenAI'],
      extensions: ['wav', 'mp3', 'm4a', 'flac', 'ogg', 'oga', 'mpga'], reason: null,
    });
    await openUploadTab();
    useFolderMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const files = [
      Object.assign(new File(['x'], 'standup.wav'), {}),
      new File(['x'], 'notes.md'),
      new File(['x'], 'logo.png'),
    ];
    Object.defineProperty(files[0], 'webkitRelativePath', { value: 'Recordings/standup.wav' });
    Object.defineProperty(files[1], 'webkitRelativePath', { value: 'Recordings/notes.md' });
    Object.defineProperty(files[2], 'webkitRelativePath', { value: 'Recordings/logo.png' });
    Object.defineProperty(input, 'files', { value: files });
    fireEvent.change(input);

    await waitFor(() => expect(screen.getByText('Recordings/standup.wav')).toBeInTheDocument());
    expect(screen.getByText('Recordings/notes.md')).toBeInTheDocument();
    // An image has no text layer and no transcription path either.
    expect(screen.queryByText('Recordings/logo.png')).not.toBeInTheDocument();
  });

  it('a folder pick hides audio when no speech provider is configured', async () => {
    await openUploadTab();   // getTranscriptionAvailability stubbed to unavailable
    useFolderMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const wav = new File(['x'], 'standup.wav');
    const md = new File(['x'], 'notes.md');
    Object.defineProperty(wav, 'webkitRelativePath', { value: 'Recordings/standup.wav' });
    Object.defineProperty(md, 'webkitRelativePath', { value: 'Recordings/notes.md' });
    Object.defineProperty(input, 'files', { value: [wav, md] });
    fireEvent.change(input);

    await waitFor(() => expect(screen.getByText('Recordings/notes.md')).toBeInTheDocument());
    expect(screen.queryByText('Recordings/standup.wav')).not.toBeInTheDocument();
  });

  it('when the server cannot find the folder it says so instead of passing off the guess', async () => {
    await openUploadTab();
    useFolderMode();   // resolveLocalPath stubbed to found:false
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const f = new File(['x'], 'incident.vtt');
    Object.defineProperty(f, 'webkitRelativePath', { value: 'audio/incident.vtt' });
    Object.defineProperty(input, 'files', { value: [f] });
    fireEvent.change(input);

    await waitFor(() => expect(screen.getByText(/assumed, not known/)).toBeInTheDocument());
    expect(screen.getByText(/different machine or in a container/)).toBeInTheDocument();
    expect(screen.queryByText(/verified on disk/)).not.toBeInTheDocument();
  });

  it('a single picked file gets its complete path from the server', async () => {
    // The single-file picker exposes only `name`, so the browser cannot
    // help — but the API runs on the same machine in local dev and can
    // look the file up, with the size narrowing same-named files.
    vi.mocked(api.resolveLocalPath).mockResolvedValue({
      found: true,
      root: '/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets/audio',
      absolute_path: '/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets/audio/incident.vtt',
    });
    await openUploadTab();
    useSingleFileMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'incident.vtt')] });
    fireEvent.change(input);

    await waitFor(() => expect(snippet()).toContain(
      '-F "file=@/Users/me/Desktop/Agent Studio 3/InaiEra_Agent_backend/sample_datasets/audio/incident.vtt"'));
    expect(screen.getByText(/verified on disk/)).toBeInTheDocument();
  });

  it('a containerised API is named as the reason, not reported as a missing file', async () => {
    // The API answering the page may be the container, whose filesystem
    // is /app — no local path can EVER resolve through it. Saying "not
    // found" sends someone looking for a file that is exactly where
    // they left it; the real explanation is which process is answering.
    vi.mocked(api.resolveLocalPath).mockResolvedValue({
      found: false, containerised: true,
      reason: 'This API is running inside a container, so it cannot see your machine\'s filesystem.',
    });
    await openUploadTab();
    useSingleFileMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'notes.md')] });
    fireEvent.change(input);

    await waitFor(() => expect(screen.getByText(/runs in a container/)).toBeInTheDocument());
    expect(screen.getByText(/paths come from the browser rather than the server/)).toBeInTheDocument();
    // Must NOT claim the file is missing.
    expect(screen.queryByText(/found no file by that name/)).not.toBeInTheDocument();
  });

  it('several files with one name are offered as a choice, never guessed between', async () => {
    vi.mocked(api.resolveLocalPath).mockResolvedValue({
      found: false, ambiguous: true,
      reason: "2 files named 'USER_GUIDE.md' with this size — pick which one you meant",
      matches: [
        { root: '/Users/me/projectA/docs', absolute_path: '/Users/me/projectA/docs/USER_GUIDE.md', size: 12 },
        { root: '/Users/me/projectB/docs', absolute_path: '/Users/me/projectB/docs/USER_GUIDE.md', size: 12 },
      ],
    });
    await openUploadTab();
    useSingleFileMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'USER_GUIDE.md')] });
    fireEvent.change(input);

    await waitFor(() => expect(screen.getByText(/2 files named/)).toBeInTheDocument());
    // Neither candidate is in the command yet — picking one for the
    // person would put a wrong path in something they then run.
    expect(snippet()).not.toContain('/Users/me/projectA');
    expect(snippet()).not.toContain('/Users/me/projectB');

    fireEvent.click(screen.getByText('/Users/me/projectB/docs/USER_GUIDE.md'));
    await waitFor(() => expect(snippet()).toContain('-F "file=@/Users/me/projectB/docs/USER_GUIDE.md"'));
    expect(screen.getByText(/verified on disk/)).toBeInTheDocument();
  });

  it('a folder root is never applied to a bare filename from the single-file picker', async () => {
    // The regression: one shared prefix meant folder mode's OS-root seed
    // was remembered and then joined to a filename in file mode, printing
    // `/Users/_make_audio.py` — absolute-looking and pointing nowhere.
    await openUploadTab();

    // Folder mode is the default; clicking is belt-and-braces.
    fireEvent.click(screen.getByLabelText(/Pick a folder/));
    const dirInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const nested = new File(['x'], 'a.md');
    Object.defineProperty(nested, 'webkitRelativePath', { value: 'Docs/a.md' });
    Object.defineProperty(dirInput, 'files', { value: [nested] });
    fireEvent.change(dirInput);
    await waitFor(() => expect(snippet()).toContain('-F "file=@/Users/Docs/a.md"'));

    // Switch to the single-file picker: that root must not come along.
    fireEvent.click(screen.getByLabelText(/Pick one file/));
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], '_make_audio.py')] });
    fireEvent.change(fileInput);

    await waitFor(() => expect(snippet()).toContain('-F "file=@_make_audio.py"'));
    expect(snippet()).not.toContain('/Users/_make_audio.py');
    expect(localStorage.getItem('inaiera.uploadApi.fileDir')).toBeNull();
  });

  it('a folder pick supplies the real nested path from the browser, no typing needed for it', async () => {
    await openUploadTab();
    // Switching to the directory picker: webkitdirectory is the only
    // browser API that returns path structure.
    fireEvent.click(screen.getByLabelText(/Pick a folder/));
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.getAttribute('webkitdirectory')).toBe('');

    const deep = new File(['x'], 'summary.pdf');
    Object.defineProperty(deep, 'webkitRelativePath', { value: 'Downloads/reports/q3/summary.pdf' });
    const junk = new File(['x'], '.DS_Store');
    Object.defineProperty(junk, 'webkitRelativePath', { value: 'Downloads/.DS_Store' });
    Object.defineProperty(input, 'files', { value: [junk, deep] });
    fireEvent.change(input);

    // The nested path came from the picker itself, not from typing, and
    // the unparseable file the directory sweep picked up is dropped.
    // The OS home root is pre-filled (jsdom reports darwin, so /Users),
    // making the path absolute with nothing typed.
    await waitFor(() => expect(snippet()).toContain('-F "file=@/Users/Downloads/reports/q3/summary.pdf"'));
    expect(screen.getByText('1 ingestible file(s) found — the selected one is filled into the command above.')).toBeInTheDocument();
    // The root is a guess until a person vouches for it, and the panel
    // must say so rather than presenting the path as verified.
    expect(screen.getByText(/assumed, not known/)).toBeInTheDocument();

    // And the root stays editable for a folder that lives elsewhere.
    fireEvent.change(screen.getByLabelText(/Where.*lives on your machine/), { target: { value: '/Volumes/work' } });
    await waitFor(() => expect(snippet()).toContain('-F "file=@/Volumes/work/Downloads/reports/q3/summary.pdf"'));
    expect(screen.getByText(/That is the whole absolute path/)).toBeInTheDocument();
  });

  it('staging the file drops the path from the command, so it runs from any directory', async () => {
    vi.mocked(api.stageUpload).mockResolvedValue({
      id: 'up-abc123', filename: 'faq.pdf', size: 12, url: 'http://x/api/uploads/up-abc123/download',
      expires_at: '2026-10-09T00:00:00',
    });
    vi.mocked(api.callKbUploadFromStoreApi).mockResolvedValue({
      document_id: 'doc-9', chunks_inserted: 4, table: 'support_articles', upload_id: 'up-abc123',
    } as any);

    await openUploadTab();
    useSingleFileMode();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'faq.pdf')] });
    fireEvent.change(input);
    await waitFor(() => expect(snippet()).toContain('-F "file=@faq.pdf"'));

    fireEvent.click(screen.getByRole('button', { name: /Stage on the platform/ }));
    await waitFor(() => expect(api.stageUpload).toHaveBeenCalled());

    // No -F, no path, nothing resolved against the caller's disk.
    await waitFor(() => expect(snippet()).toContain('/api/upload-from-store'));
    expect(snippet()).toContain('"upload_id":"up-abc123"');
    expect(snippet()).not.toContain('-F "file=@');
    expect(snippet()).not.toContain('faq.pdf"');
    expect(screen.getByText(/runs unchanged from any directory/)).toBeInTheDocument();

    // Call API now goes through the stored file, not a re-upload.
    fireEvent.click(screen.getByText('Call API'));
    await waitFor(() => expect(api.callKbUploadFromStoreApi).toHaveBeenCalledWith(
      'support_articles', 'up-key-xyz', { upload_id: 'up-abc123' }));
    expect(api.callKbUploadApi).not.toHaveBeenCalled();
  });

  it('picking a GitHub file switches the command to the source endpoint and shows the complete path', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([githubSource] as any);
    // GitHub addresses files BY path, so the id in the body already is
    // the full location — nothing is left for the reader to fill in.
    vi.mocked(api.browseDataSource).mockImplementation(async (_s: string, path = '') =>
      (path === ''
        ? { source: 'GitHub', path: '', entries: [entry({ id: 'me/repo', name: 'me/repo', is_dir: true, path: 'me/repo' })] }
        : { source: 'GitHub', path, entries: [entry({ id: 'me/repo/docs/policy.md', name: 'policy.md' })] }) as any);

    await openUploadTab();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByText('me/repo')).toBeInTheDocument());
    fireEvent.click(screen.getByText('me/repo'));
    await waitFor(() => expect(screen.getByLabelText(/policy\.md/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/policy\.md/));

    await waitFor(() => expect(snippet()).toContain('/api/upload-from-source'));
    expect(snippet()).toContain('"file_ids":["me/repo/docs/policy.md"]');
    expect(snippet()).toContain('Authorization: Bearer up-key-xyz');
    // The multipart form is gone — this request names a location.
    expect(snippet()).not.toContain('-F "file=@');
    expect(screen.getByText(/nothing to fill in and nothing to download first/)).toBeInTheDocument();
  });

  it('for an id-addressed source the readable path is shown alongside rather than passed off as the id', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([sharepointSource] as any);
    vi.mocked(api.browseDataSource).mockImplementation(async (_s: string, path = '') =>
      (path === ''
        ? { source: 'SharePoint', path: '', entries: [entry({ id: 'ITEM-FOLDER', name: 'Policies', is_dir: true, path: 'ITEM-FOLDER' })] }
        : { source: 'SharePoint', path, entries: [entry({ id: '01OPAQUEID', name: 'handbook.docx' })] }) as any);

    await openUploadTab();
    pickOption(await screen.findByLabelText('Source'), 'SharePoint');
    await waitFor(() => expect(screen.getByText('Policies')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Policies'));
    await waitFor(() => expect(screen.getByLabelText(/handbook\.docx/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/handbook\.docx/));

    await waitFor(() => expect(snippet()).toContain('"file_ids":["01OPAQUEID"]'));
    expect(snippet()).toContain('# path(s): SharePoint/Policies/handbook.docx');
  });

  it('Call API on a source selection hits the real source upload endpoint and reports each file', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([githubSource] as any);
    vi.mocked(api.browseDataSource).mockResolvedValue({
      source: 'GitHub', path: '', entries: [
        entry({ id: 'me/repo/a.md', name: 'a.md' }),
        entry({ id: 'me/repo/b.md', name: 'b.md' }),
      ],
    } as any);
    vi.mocked(api.callKbUploadFromSourceApi).mockResolvedValue({
      source: 'GitHub', requested: 2, ingested: 1, failed: 1,
      results: [
        { file_id: 'me/repo/a.md', filename: 'a.md', success: true, chunks_inserted: 7,
          table: 'support_articles', document_id: 'doc-1' },
        { file_id: 'me/repo/b.md', filename: 'b.md', success: false, error: 'File is empty' },
      ],
    } as any);

    await openUploadTab();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByText(/Select all 2 files in this folder/)).toBeInTheDocument());
    // One click takes the whole folder, rather than ticking each file.
    fireEvent.click(screen.getByLabelText(/Select all 2 files in this folder/));
    await waitFor(() => expect(screen.getByText('2 item(s) selected')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Call API'));
    await waitFor(() => expect(api.callKbUploadFromSourceApi).toHaveBeenCalledWith(
      'support_articles', 'up-key-xyz',
      // Folders travel separately from files, so a directory is never
      // sent as something to fetch as a document.
      { source: 'GitHub', file_ids: ['me/repo/a.md', 'me/repo/b.md'], folder_paths: [] }));
    await waitFor(() => expect(screen.getByText('doc-1')).toBeInTheDocument());
    expect(screen.getByText(/b\.md — File is empty/)).toBeInTheDocument();
  });
});
