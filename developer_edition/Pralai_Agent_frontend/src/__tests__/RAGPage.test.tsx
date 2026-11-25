import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider, useApp } from '../context/AppContext';
import RAG from '../pages/RAG';
import NewRagPipelineModal from '../modals/NewRagPipelineModal';
import { api } from '../api';
import type { KnowledgeBase } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');

afterEach(() => cleanup());

function Harness() {
  const { modal } = useApp();
  return (
    <>
      <RAG />
      {modal === 'newRagPipeline' && <NewRagPipelineModal />}
    </>
  );
}

function renderPage(url = '/rag') {
  return render(<MemoryRouter initialEntries={[url]}><AppProvider><Harness /></AppProvider></MemoryRouter>);
}

function makeKB(overrides?: Partial<KnowledgeBase>): KnowledgeBase {
  return {
    id: 'support_articles', name: 'Support Articles', provider: 'pgvector',
    config: { host: '127.0.0.1', port: 5432, dbname: 'customer_kb', user: 'kb_user', table: 'support_articles' },
    status: 'connected', last_error: null, last_checked_at: null, created_at: '2026-01-01T00:00:00',
    ...overrides,
  } as KnowledgeBase;
}

describe('RAG page — real knowledge base document pipeline, split out from Integrations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // DocumentsTab builds its per-document metadata editor from the
    // KB's registered fields, so this call happens on every render.
    vi.mocked(api.listKbMetadataFields).mockResolvedValue([]);
    // Add file now offers configured Data Sources as an ingest origin;
    // none are connected here, so it falls back to local upload.
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB()]);
    vi.mocked(api.listKbDocuments).mockResolvedValue([]);
    // Add file asks whether audio can be transcribed before offering
    // it. No speech provider here — the audio-specific behaviour has
    // its own tests, which override this.
    vi.mocked(api.getTranscriptionAvailability).mockResolvedValue(
      { available: false, providers: [], extensions: ['wav', 'mp3', 'm4a', 'flac', 'ogg', 'oga', 'mpga'],
        reason: 'No OpenAI or Gemini key is configured.' });
    // Add file asks where a kept original file would go, so its
    // checkbox can name the real destination.
    vi.mocked(api.getStorageTarget).mockResolvedValue({
      backend: 's3', configured: true, label: 'S3 · test-bucket',
      bucket: 'test-bucket', endpoint_url: null, retention_days: 30,
      destination_id: 'env', can_store_credentials: true,
      destinations: [
        { id: 'env', name: 'Server configuration (.env)', provider: 's3', bucket: 'test-bucket',
          region: 'us-east-2', endpoint_url: null, configured: true, label: 'S3 · test-bucket',
          editable: false, is_default: true },
      ] });
  });

  it('lists real pgvector knowledge bases with a Manage entry point, no separate tab-click needed', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    expect(screen.getByText(/kb_user@127\.0\.0\.1:5432\/customer_kb/)).toBeInTheDocument();
  });

  it('a hosted vector store appears as a normal pipeline card, not in a second-class list', async () => {
    // The page used to split on `provider === 'pgvector'` and tell
    // people ingestion "isn't wired" for Pinecone/Qdrant/Weaviate —
    // which stopped being true once each got an adapter. Capability is
    // now reported by the backend, so the card cannot go stale again.
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB({
        id: 'weav', name: 'Weaviate Pipeline', provider: 'weaviate',
        config: { url: 'https://my-cluster.c0.gcp.weaviate.cloud', index: 'Docs' },
        status: 'connected', ingest_supported: true, supports_fulltext: false,
      }),
    ] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('Weaviate Pipeline')).toBeInTheDocument());
    // Rendered as a pipeline, with its provider named.
    expect(screen.getByText('RAG Pipeline · weaviate')).toBeInTheDocument();
    // Host and collection, not "undefined@undefined:undefined/undefined".
    expect(screen.getByText('my-cluster.c0.gcp.weaviate.cloud')).toBeInTheDocument();
    expect(screen.getByText('Docs')).toBeInTheDocument();
    expect(screen.queryByText(/no ingestion adapter/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Manage connection')).not.toBeInTheDocument();
  });

  it('a store with no adapter at all is still listed apart, with an accurate reason', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB({ id: 'oth', name: 'Some Store', provider: 'other',
               config: { url: 'https://example.test' }, ingest_supported: false }),
    ] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('Some Store')).toBeInTheDocument());
    // Both the section heading and the paragraph mention it, so this
    // asserts the paragraph specifically.
    expect(screen.getByText(/there is no ingestion adapter/)).toBeInTheDocument();
    // The reason must not claim pgvector is the only one supported.
    expect(screen.getByText(/Pinecone, Qdrant, Weaviate and Milvus each have one/)).toBeInTheDocument();
    expect(screen.getByText('Manage connection')).toBeInTheDocument();
  });

  it('empty state offers a real, direct way to create the first pipeline, not just a link elsewhere', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText('No RAG pipelines yet.')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Create your first pipeline'));
    await waitFor(() => expect(screen.getByText(/start from a knowledge base you have already configured/)).toBeInTheDocument());
  });

  it('New RAG Pipeline button opens the real creation modal, not the Integrations connection-only modal', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('New RAG Pipeline'));
    await waitFor(() => expect(screen.getByText(/start from a knowledge base you have already configured/)).toBeInTheDocument());
  });

  it('existing connections are a searchable list grouped by provider, with the address under each name', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB(),
      makeKB({ id: 'pc', name: 'Pinecone Movies', provider: 'pinecone',
        config: { url: 'https://movies.svc.pinecone.io', index: 'movies' } as never }),
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('New RAG Pipeline'));
    fireEvent.click(await screen.findByText('Enter connection details manually'));

    // Grouped, with the connection on a second line rather than run into the name.
    const list = () => within(screen.getByRole('listbox'));
    expect(list().getByText('pgvector (Postgres)')).toBeInTheDocument();
    expect(list().getByText('kb_user@127.0.0.1:5432/customer_kb')).toBeInTheDocument();
    expect(list().getByText('movies.svc.pinecone.io')).toBeInTheDocument();

    // Search narrows by host as well as name.
    fireEvent.change(screen.getByPlaceholderText('Search by name, host or provider…'), { target: { value: 'pinecone.io' } });
    expect(list().queryByText('kb_user@127.0.0.1:5432/customer_kb')).not.toBeInTheDocument();
    fireEvent.click(list().getByText('Pinecone Movies'));
    await waitFor(() => expect(screen.getByText(/Copied from that knowledge base/)).toBeInTheDocument());
  });

  it('search filters pipelines by name, provider or connection, with a count and a clear way back', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB(),
      makeKB({ id: 'pc', name: 'Pinecone Movies', provider: 'pinecone',
        config: { url: 'https://movies.svc.pinecone.io', index: 'movies' } as never }),
    ]);
    // The filter arrives from the top-bar search as ?q= — the page has no box of its own.
    let view = renderPage('/rag?q=pinecone');
    await waitFor(() => expect(screen.getByText('1 of 2 pipelines')).toBeInTheDocument());
    expect(screen.getByText('Pinecone Movies')).toBeInTheDocument();
    expect(screen.queryByText('Support Articles')).not.toBeInTheDocument();
    view.unmount();

    view = renderPage('/rag?q=customer_kb');   // matches the connection string
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    expect(screen.queryByText('Pinecone Movies')).not.toBeInTheDocument();
    view.unmount();

    renderPage('/rag?q=nothing-like-this');
    await waitFor(() => expect(screen.getByText(/No pipeline matches/)).toBeInTheDocument());
    // The empty state's own way back (the top-bar SearchNote has one too).
    const empty = screen.getByText(/No pipeline matches/).closest('.empty-cfg') as HTMLElement;
    fireEvent.click(within(empty).getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('2 pipelines')).toBeInTheDocument();
  });

  it('switches between grid and list view, and remembers the choice', async () => {
    // This environment has no localStorage; stand one in so the choice can persist.
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null, setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    expect(screen.getByText('1 pipeline')).toBeInTheDocument();
    expect(screen.getByLabelText('Grid view')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('columnheader', { name: 'Connection' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('List view'));
    expect(screen.getByRole('columnheader', { name: 'Connection' })).toBeInTheDocument();
    expect(screen.getByText('kb_user@127.0.0.1:5432/customer_kb')).toBeInTheDocument();
    expect(screen.getByLabelText('Edit the Support Articles connection credentials')).toBeInTheDocument();
    expect(localStorage.getItem('rag.view')).toBe('list');

    cleanup();
    renderPage();
    await waitFor(() => expect(screen.getByRole('columnheader', { name: 'Connection' })).toBeInTheDocument());
    vi.unstubAllGlobals();
  });

  it('creating a pipeline sends real pgvector connection details and lands directly on its detail view', async () => {
    vi.mocked(api.createKnowledgeBase).mockResolvedValue(makeKB({ id: 'new_kb', name: 'New KB' }));
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('New RAG Pipeline'));
    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Support Articles')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('e.g. Support Articles'), { target: { value: 'New KB' } });
    fireEvent.change(screen.getByPlaceholderText('127.0.0.1'), { target: { value: '10.0.0.9' } });
    fireEvent.change(screen.getByPlaceholderText('5432'), { target: { value: '5432' } });

    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB(), makeKB({ id: 'new_kb', name: 'New KB', config: { ...makeKB().config, host: '10.0.0.9' } })]);
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => {
      expect(api.createKnowledgeBase).toHaveBeenCalledWith({
        name: 'New KB', provider: 'pgvector', config: expect.objectContaining({ host: '10.0.0.9' }),
      });
    });
    // Real "create, then open" flow — lands on the new pipeline's Documents tab, not back on the list.
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
  });

  it('pipeline cards show real Chunk Structure and Retrieval Method summary chips, in a Studio-style grid', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB({ config: { ...makeKB().config, chunk_structure: 'qa', retrieval_method: 'hybrid', retrieval_api_enabled: true } })]);
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    const card = screen.getByText('Support Articles').closest('.studio-card') as HTMLElement;
    expect(card).toBeTruthy();
    expect(within(card).getByText('Q&A')).toBeInTheDocument();
    expect(within(card).getByText('Hybrid Search')).toBeInTheDocument();
    expect(within(card).getByText(/API enabled/)).toBeInTheDocument();
  });

  it('clicking a knowledge base opens its real detail view (Documents tab) directly, no separate Manage button', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
  });

  // ---- ingesting from a configured Data Source ----

  const connectedGithub = {
    name: 'GitHub', version: '1.1.0', status: 'connected' as const, auth: 'apikey' as const,
    config: { credentials: '••••••••' }, browsable: true, browse_supported: true,
  };
  const entry = (over: Partial<any>) => ({
    id: 'x', name: 'x', is_dir: false, path: '', size: 2048, mime: null,
    ingestible: true, note: '', ...over,
  });

  async function openAddFile() {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add file'));
  }

  it('with no data sources connected, Add file offers only local upload and says where sources come from', async () => {
    await openAddFile();
    await waitFor(() => expect(document.querySelector('input[type="file"]')).toBeInTheDocument());
    expect(screen.queryByLabelText('Source')).not.toBeInTheDocument();
    expect(screen.getByText(/Connect Google Drive, SharePoint, OneDrive/)).toBeInTheDocument();
  });

  it('keeping the original file names the real destination and sends the option', async () => {
    // Ingestion extracts text and throws the file away, so without
    // this the only copy of an uploaded PDF is on whoever's laptop it
    // came from. The label has to name where it actually goes — the
    // server is asked, rather than the UI assuming a bucket.
    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'support_articles', text_column: 'content', chunks_inserted: 2,
      embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'faq.txt',
      document_id: 'doc-2',
      original_file: {
        id: 'up-1', filename: 'faq.txt', size: 12, content_type: 'text/plain',
        storage_backend: 's3', object_key: 'anonymous/up-1/faq.txt',
        url: 'http://localhost:8001/api/uploads/up-1/download', expires_at: null,
      },
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('Also keep the original file')).toBeInTheDocument());
    // The destination is shown beside the label, so it can be read
    // before the switch is touched.
    expect(screen.getByText('S3 · test-bucket')).toBeInTheDocument();

    const file = new File(['hello world'], 'faq.txt', { type: 'text/plain' });
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));

    await waitFor(() => expect(api.ingestDocument).toHaveBeenCalledWith(
      'support_articles', file, expect.objectContaining({ keepOriginal: true })));
    // Where it went, not just that it went somewhere.
    await waitFor(() => expect(screen.getByText('anonymous/up-1/faq.txt')).toBeInTheDocument());
    expect(screen.getByText('download')).toHaveAttribute(
      'href', 'http://localhost:8001/api/uploads/up-1/download');
  });

  it('unticking the box ingests without keeping the file', async () => {
    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'support_articles', text_column: 'content', chunks_inserted: 2,
      embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'faq.txt',
      document_id: 'doc-3', original_file: null,
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('Also keep the original file')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /Also keep the original file/ }));

    const file = new File(['hello world'], 'faq.txt', { type: 'text/plain' });
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement,
                     { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));

    await waitFor(() => expect(api.ingestDocument).toHaveBeenCalledWith(
      'support_articles', file, expect.objectContaining({ keepOriginal: false })));
  });

  it('with no storage destination configured, the file cannot be kept and the UI says why', async () => {
    // An S3 backend with no bucket cannot store anything. Offering the
    // tickbox anyway would be a control whose upload then fails.
    vi.mocked(api.getStorageTarget).mockResolvedValue({
      backend: 's3', configured: false, label: 'S3 (no bucket configured)',
      bucket: null, endpoint_url: null, retention_days: 30 });
    await openAddFile();
    await waitFor(() => expect(screen.getByText(/No storage destination is configured/)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /Also keep the original file/ })).toBeDisabled();
  });

  it('a failure to keep the file is reported, not silently swallowed', async () => {
    // The document is already ingested by then, so this cannot fail
    // the upload — but saying nothing would leave someone believing
    // their file is in S3 when it is not.
    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'support_articles', text_column: 'content', chunks_inserted: 2,
      embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'faq.txt',
      document_id: 'doc-4', original_file: null,
      original_file_error: 'could not write to s3 storage: AccessDenied',
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('Also keep the original file')).toBeInTheDocument());
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement,
                     { target: { files: [new File(['x'], 'faq.txt')] } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));

    await waitFor(() => expect(
      screen.getByText(/original file could NOT be kept: could not write to s3 storage: AccessDenied/),
    ).toBeInTheDocument());
  });

  it('a document that kept its file offers it back from the list', async () => {
    vi.mocked(api.listKbDocuments).mockResolvedValue([
      { id: 'doc-1', kb_id: 'support_articles', filename: 'policy.pdf', chunking_mode: 'General',
        max_words: 180, overlap_words: 30, word_count: 900, chunk_count: 6, retrieval_count: 0,
        status: 'enabled', created_at: new Date().toISOString(), metadata: {},
        original_file: { id: 'up-9', filename: 'policy.pdf', size: 4096, content_type: 'application/pdf',
                         storage_backend: 's3', object_key: 'anonymous/up-9/policy.pdf',
                         url: 'http://localhost:8001/api/uploads/up-9/download', expires_at: null } },
      { id: 'doc-2', kb_id: 'support_articles', filename: 'faq.txt', chunking_mode: 'General',
        max_words: 180, overlap_words: 30, word_count: 100, chunk_count: 1, retrieval_count: 0,
        status: 'enabled', created_at: new Date().toISOString(), metadata: {} },
    ] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));

    await waitFor(() => expect(screen.getByText('policy.pdf')).toBeInTheDocument());
    // Exactly one link: the document ingested without keeping its file
    // has no bytes to offer, and a link that 404s is worse than none.
    const links = screen.getAllByText('original');
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', 'http://localhost:8001/api/uploads/up-9/download');
  });

  it('the upload destination can be changed without editing the server', async () => {
    // The whole point of the dialog: credentials used to live only in
    // .env, so pointing an upload at a different bucket meant editing a
    // file on the server and restarting it.
    vi.mocked(api.createStorageDestination).mockResolvedValue({
      id: 'sd-1', name: 'Marketing', provider: 's3', bucket: 'marketing-uploads',
      region: 'eu-west-1', endpoint_url: null, configured: true,
      label: 'S3 · marketing-uploads', editable: true, is_default: true,
      verified: "wrote, read and deleted a test object in 'marketing-uploads'",
    });
    vi.mocked(api.getStorageTarget).mockResolvedValueOnce({
      backend: 's3', configured: true, label: 'S3 · test-bucket', bucket: 'test-bucket',
      endpoint_url: null, retention_days: 30, destination_id: 'env', can_store_credentials: true,
      destinations: [{ id: 'env', name: 'Server configuration (.env)', provider: 's3',
                       bucket: 'test-bucket', region: null, endpoint_url: null, configured: true,
                       label: 'S3 · test-bucket', editable: false, is_default: true }],
    }).mockResolvedValue({
      backend: 's3', configured: true, label: 'S3 · marketing-uploads', bucket: 'marketing-uploads',
      endpoint_url: null, retention_days: 30, destination_id: 'sd-1', can_store_credentials: true,
      destinations: [
        { id: 'env', name: 'Server configuration (.env)', provider: 's3', bucket: 'test-bucket',
          region: null, endpoint_url: null, configured: true, label: 'S3 · test-bucket',
          editable: false, is_default: false },
        { id: 'sd-1', name: 'Marketing', provider: 's3', bucket: 'marketing-uploads',
          region: 'eu-west-1', endpoint_url: null, configured: true,
          label: 'S3 · marketing-uploads', editable: true, is_default: true },
      ],
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('S3 · test-bucket')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Change'));

    await waitFor(() => expect(screen.getByText('Where uploaded files are kept')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Use new credentials'));
    fireEvent.change(screen.getByLabelText('Bucket'), { target: { value: 'marketing-uploads' } });
    fireEvent.change(screen.getByLabelText('Access key ID'), { target: { value: 'AKIAEXAMPLE' } });
    fireEvent.change(screen.getByLabelText('Secret access key'), { target: { value: 's3cr3t' } });
    fireEvent.click(screen.getByText('Save and use'));

    await waitFor(() => expect(api.createStorageDestination).toHaveBeenCalledWith(
      expect.objectContaining({ bucket: 'marketing-uploads', access_key_id: 'AKIAEXAMPLE',
                                secret_access_key: 's3cr3t' })));
    // The card now names the new destination, and the next ingest goes there.
    await waitFor(() => expect(screen.getByText('S3 · marketing-uploads')).toBeInTheDocument());

    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'support_articles', text_column: 'content', chunks_inserted: 1,
      embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'faq.txt',
      document_id: 'doc-9',
    });
    const file = new File(['hello'], 'faq.txt', { type: 'text/plain' });
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement,
                     { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));
    await waitFor(() => expect(api.ingestDocument).toHaveBeenCalledWith(
      'support_articles', file, expect.objectContaining({ storageDestinationId: 'sd-1' })));
  });

  it('credentials are tested against the real bucket before anything is saved', async () => {
    // A destination that is listed but cannot be written to is worse
    // than none: the failure then lands at the end of an upload rather
    // than in the dialog someone is already looking at.
    vi.mocked(api.testStorageDestination).mockResolvedValue({
      ok: false,
      message: "real S3 put_object failed: An error occurred (InvalidAccessKeyId)",
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('Change')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Change'));
    await waitFor(() => expect(screen.getByText('Use new credentials')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Use new credentials'));
    fireEvent.change(screen.getByLabelText('Bucket'), { target: { value: 'wrong-bucket' } });
    fireEvent.click(screen.getByText('Test connection'));

    await waitFor(() => expect(screen.getByText(/InvalidAccessKeyId/)).toBeInTheDocument());
    // Nothing stored on a failed test.
    expect(api.createStorageDestination).not.toHaveBeenCalled();
  });

  it('a server on a throwaway encryption key says so before anyone types a secret', async () => {
    // Credentials saved under an ephemeral key are unreadable after the
    // next restart — this platform has lost provider keys that way.
    vi.mocked(api.getStorageTarget).mockResolvedValue({
      backend: 's3', configured: true, label: 'S3 · test-bucket', bucket: 'test-bucket',
      endpoint_url: null, retention_days: 30, destination_id: 'env', can_store_credentials: false,
      destinations: [{ id: 'env', name: 'Server configuration (.env)', provider: 's3',
                       bucket: 'test-bucket', region: null, endpoint_url: null, configured: true,
                       label: 'S3 · test-bucket', editable: false, is_default: true }],
    });
    await openAddFile();
    await waitFor(() => expect(screen.getByText('Change')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Change'));
    await waitFor(() => expect(screen.getByText(/temporary encryption key/)).toBeInTheDocument());
  });

  it('audio is offered for ingest only when a speech provider is genuinely configured', async () => {
    // Audio has no text layer, so ingesting a .wav means transcribing
    // it — which needs a key. With one configured the picker accepts
    // audio and says what will happen to it.
    vi.mocked(api.getTranscriptionAvailability).mockResolvedValue({
      available: true, providers: ['Gemini'],
      extensions: ['wav', 'mp3', 'm4a', 'flac', 'ogg', 'oga', 'mpga'], reason: null,
    });
    await openAddFile();
    await waitFor(() => expect(document.querySelector('input[type="file"]')).toBeInTheDocument());
    await waitFor(() => expect(
      document.querySelector('input[type="file"]')!.getAttribute('accept'),
    ).toContain('.wav'));
    // Same set the server reported, read in the order people name
    // them rather than alphabetically (.flac first).
    expect(screen.getByText(/Audio \(\.wav, \.mp3, \.m4a, \.flac, \.ogg, \.oga, \.mpga\)/)).toBeInTheDocument();
    expect(screen.getByText(/transcribed on ingest via Gemini/)).toBeInTheDocument();
  });

  it('after ingesting audio, the result names the model that transcribed it', async () => {
    // The text in the pipeline came from speech recognition, not from
    // the file — retrieval quality depends on that model, so which one
    // ran is worth reporting rather than leaving implied.
    vi.mocked(api.getTranscriptionAvailability).mockResolvedValue({
      available: true, providers: ['Gemini'], extensions: ['wav'], reason: null,
    });
    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'audio_transcripts', text_column: 'content', chunks_inserted: 1,
      embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'standup.wav',
      document_id: 'doc-1', transcription: { provider: 'Gemini', model: 'gemini-flash-latest', characters: 149 },
    });
    await openAddFile();
    await waitFor(() => expect(document.querySelector('input[type="file"]')).toBeInTheDocument());
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'standup.wav')] });
    fireEvent.change(input);
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));

    await waitFor(() => expect(screen.getByText(/Transcribed by Gemini/)).toBeInTheDocument());
    expect(screen.getByText('gemini-flash-latest')).toBeInTheDocument();
    // The transcript is what retrieval will match against, so its
    // accuracy is the pipeline's accuracy — said once, plainly.
    expect(screen.getByText(/only as accurate as the transcription/)).toBeInTheDocument();
  });

  it('with no speech provider, audio is not offered and nothing claims it will be transcribed', async () => {
    // The default stub in beforeEach is "no key configured". Listing a
    // .wav here would take the upload all the way to the end before
    // failing, having explained nothing.
    await openAddFile();
    await waitFor(() => expect(document.querySelector('input[type="file"]')).toBeInTheDocument());
    expect(document.querySelector('input[type="file"]')!.getAttribute('accept'))
      .not.toContain('.wav');
    expect(screen.queryByText(/transcribed on ingest/)).not.toBeInTheDocument();
  });

  it('a source is offered only when credentials are genuinely stored, not merely when status says connected', async () => {
    // status "connected" with nothing stored is exactly the state the
    // old configure endpoints left behind; browsable is what counts.
    vi.mocked(api.listDataSources).mockResolvedValue([
      { ...connectedGithub, config: {}, browsable: false },
    ] as any);
    await openAddFile();
    await waitFor(() => expect(document.querySelector('input[type="file"]')).toBeInTheDocument());
    expect(screen.queryByRole('option', { name: 'GitHub' })).not.toBeInTheDocument();
  });

  it('a pipeline whose password cannot be decrypted says so on the card', async () => {
    // It reads "connected" and shows a masked password, while every
    // ingest fails with a crypto error naming no remedy. The remedy is
    // to retype the password, so the card has to point at that.
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB({ id: 'broken_kb', name: 'Broken KB', status: 'connected', secrets_usable: false }),
      makeKB({ id: 'fine_kb', name: 'Fine KB', status: 'connected', secrets_usable: true }),
    ] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('password unreadable')).toBeInTheDocument());
    // The healthy pipeline is unaffected.
    expect(screen.getByText('connected')).toBeInTheDocument();
  });

  it('a listing that would mislead carries the reason above the rows', async () => {
    // The OneDrive root lists every drive the app was consented to,
    // because app-only auth has no signed-in user to scope to. Whoever
    // registered the app from their own account expects to see only
    // their own files, so the listing has to say why it doesn't.
    vi.mocked(api.listDataSources).mockResolvedValue([
      { ...connectedGithub, name: 'OneDrive', auth: 'oauth' },
    ] as any);
    vi.mocked(api.browseDataSource).mockResolvedValue({
      source: 'OneDrive', path: '',
      notice: 'This is app-only access: the token carries no signed-in user, so it shows every drive the app was granted.',
      entries: [
        entry({ id: 'site:a', name: 'Alex Doe', is_dir: true, path: 'site:a' }),
        entry({ id: 'site:b', name: 'Sunil Gutta', is_dir: true, path: 'site:b' }),
      ],
    } as any);
    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'OneDrive');
    await waitFor(() => expect(screen.getByText(/no signed-in user/)).toBeInTheDocument());
    expect(screen.getByText('Alex Doe')).toBeInTheDocument();
  });

  it('no notice is rendered when the listing speaks for itself', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockResolvedValue({
      source: 'GitHub', path: '', entries: [entry({ id: 'r', name: 'repo', is_dir: true, path: 'r' })],
    } as any);
    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByText('repo')).toBeInTheDocument());
    expect(screen.queryByText(/app-only access/)).not.toBeInTheDocument();
  });

  it('picking a connected source browses it, and files from several folders stay selected across navigation', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockImplementation(async (_src: string, path = '') => {
      if (path === '') {
        return { source: 'GitHub', path: '', entries: [
          entry({ id: 'me/repo', name: 'me/repo', is_dir: true, path: 'me/repo' }),
        ] } as any;
      }
      if (path === 'me/repo') {
        return { source: 'GitHub', path, entries: [
          entry({ id: 'me/repo/docs', name: 'docs', is_dir: true, path: 'me/repo/docs' }),
          entry({ id: 'me/repo/notes', name: 'notes', is_dir: true, path: 'me/repo/notes' }),
          entry({ id: 'me/repo/logo.png', name: 'logo.png', ingestible: false, note: 'no text extractor for this type' }),
        ] } as any;
      }
      if (path === 'me/repo/docs') {
        return { source: 'GitHub', path, entries: [entry({ id: 'me/repo/docs/a.md', name: 'a.md' })] } as any;
      }
      return { source: 'GitHub', path, entries: [entry({ id: 'me/repo/notes/b.txt', name: 'b.txt' })] } as any;
    });
    vi.mocked(api.ingestFromDataSource).mockResolvedValue({
      source: 'GitHub', requested: 2, ingested: 2, failed: 0,
      results: [
        { file_id: 'me/repo/docs/a.md', filename: 'a.md', success: true, chunks_inserted: 3, embed_model: 'MiniLM L6 v2' },
        { file_id: 'me/repo/notes/b.txt', filename: 'b.txt', success: true, chunks_inserted: 5, embed_model: 'MiniLM L6 v2' },
      ],
    } as any);

    await openAddFile();
    const select = await screen.findByLabelText('Source');
    pickOption(select, 'GitHub');

    // Down into the first folder and tick a file.
    await waitFor(() => expect(screen.getByText('me/repo')).toBeInTheDocument());
    fireEvent.click(screen.getByText('me/repo'));
    await waitFor(() => expect(screen.getByText('docs')).toBeInTheDocument());
    // A file with no text extractor is listed but cannot be selected.
    expect(screen.getByLabelText(/logo\.png/)).toBeDisabled();
    fireEvent.click(screen.getByText('docs'));
    await waitFor(() => expect(screen.getByLabelText(/a\.md/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/a\.md/));

    // Back up via the breadcrumb, into a different folder, tick another.
    fireEvent.click(screen.getByText('me/repo'));
    await waitFor(() => expect(screen.getByText('notes')).toBeInTheDocument());
    fireEvent.click(screen.getByText('notes'));
    await waitFor(() => expect(screen.getByLabelText(/b\.txt/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/b\.txt/));

    // The first folder's pick survived leaving that folder, and each is
    // named by the folder path it was picked from, not a bare filename.
    expect(screen.getByText(/2 file\(s\) selected: me\/repo\/docs\/a\.md, me\/repo\/notes\/b\.txt/)).toBeInTheDocument();

    fireEvent.click(screen.getByText(/Ingest 2 file/));
    await waitFor(() => expect(api.ingestFromDataSource).toHaveBeenCalledWith('support_articles',
      expect.objectContaining({
        source: 'GitHub',
        file_ids: ['me/repo/docs/a.md', 'me/repo/notes/b.txt'],
        table: 'support_articles',
      })));
    await waitFor(() => expect(screen.getByText(/a\.md — 3 chunk\(s\) via MiniLM L6 v2/)).toBeInTheDocument());
    expect(screen.getByText(/b\.txt — 5 chunk\(s\)/)).toBeInTheDocument();
  });

  it('a folder can be selected as a whole, and travels separately from individual files', async () => {
    // Previously a folder could only be OPENED, never selected, so
    // "index this folder" meant ticking every file by hand.
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockResolvedValue({
      source: 'GitHub', path: '', entries: [
        entry({ id: 'me/repo/docs', name: 'docs', is_dir: true, path: 'me/repo/docs' }),
        entry({ id: 'me/repo/README.md', name: 'README.md' }),
      ],
    } as any);
    vi.mocked(api.ingestFromDataSource).mockResolvedValue({
      source: 'GitHub', requested: 3, ingested: 3, failed: 0, results: [],
    } as any);

    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByLabelText('Select folder docs')).toBeInTheDocument());

    // Tick the folder AND a loose file.
    fireEvent.click(screen.getByLabelText('Select folder docs'));
    fireEvent.click(screen.getByLabelText(/README\.md/));
    await waitFor(() => expect(
      screen.getByText(/1 file\(s\) and 1 folder\(s\) \(everything inside\) selected/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Ingest 2 file/));
    await waitFor(() => expect(api.ingestFromDataSource).toHaveBeenCalledWith('support_articles',
      expect.objectContaining({
        file_ids: ['me/repo/README.md'],
        folder_paths: ['me/repo/docs'],
      })));
  });

  it('clicking a folder name still opens it rather than selecting it', async () => {
    // Two actions on one row; the name must keep navigating.
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockImplementation(async (_s: string, path = '') =>
      (path === ''
        ? { source: 'GitHub', path: '', entries: [entry({ id: 'me/repo/docs', name: 'docs', is_dir: true, path: 'me/repo/docs' })] }
        : { source: 'GitHub', path, entries: [entry({ id: 'me/repo/docs/a.md', name: 'a.md' })] }) as any);
    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByText('docs')).toBeInTheDocument());
    fireEvent.click(screen.getByText('docs'));
    await waitFor(() => expect(screen.getByLabelText(/a\.md/)).toBeInTheDocument());
    // Navigating must not have queued the folder.
    expect(screen.getByText(/Click a folder name to browse it/)).toBeInTheDocument();
  });

  it('a failed file in a multi-file batch is reported individually rather than sinking the whole ingest', async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockResolvedValue({
      source: 'GitHub', path: '', entries: [
        entry({ id: 'me/repo/ok.md', name: 'ok.md' }),
        entry({ id: 'me/repo/bad.md', name: 'bad.md' }),
      ],
    } as any);
    vi.mocked(api.ingestFromDataSource).mockResolvedValue({
      source: 'GitHub', requested: 2, ingested: 1, failed: 1,
      results: [
        { file_id: 'me/repo/ok.md', filename: 'ok.md', success: true, chunks_inserted: 2, embed_model: 'MiniLM L6 v2' },
        { file_id: 'me/repo/bad.md', filename: 'bad.md', success: false, error: 'File is empty' },
      ],
    } as any);

    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByLabelText(/ok\.md/)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/ok\.md/));
    fireEvent.click(screen.getByLabelText(/bad\.md/));
    fireEvent.click(screen.getByText(/Ingest 2 file/));

    await waitFor(() => expect(screen.getByText(/ok\.md — 2 chunk\(s\) via MiniLM L6 v2/)).toBeInTheDocument());
    expect(screen.getByText(/bad\.md — File is empty/)).toBeInTheDocument();
  });

  it("a browse failure shows the provider's own message instead of an empty folder", async () => {
    vi.mocked(api.listDataSources).mockResolvedValue([connectedGithub] as any);
    vi.mocked(api.browseDataSource).mockRejectedValue(
      new Error('GitHub list repositories failed (401): Bad credentials'));
    await openAddFile();
    pickOption(await screen.findByLabelText('Source'), 'GitHub');
    await waitFor(() => expect(screen.getByText(/Bad credentials/)).toBeInTheDocument());
  });

  it('opening Add file pre-fills the target table from the real KB config', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add file'));
    await waitFor(() => expect(screen.getByDisplayValue('support_articles')).toBeInTheDocument());
  });

  it('Ingest calls the real endpoint with the real file, table, and chunking options, and shows the real result', async () => {
    vi.mocked(api.ingestDocument).mockResolvedValue({
      success: true, table: 'support_articles', text_column: 'content',
      chunks_inserted: 4, embed_model: 'local-hashing-v1', embed_dim: 384, source_filename: 'faq.txt',
    });
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add file'));
    await waitFor(() => expect(screen.getByText('Ingest')).toBeInTheDocument());

    const file = new File(['hello world'], 'faq.txt', { type: 'text/plain' });
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [file] } });

    fireEvent.click(screen.getByText('Ingest'));

    await waitFor(() => {
      expect(api.ingestDocument).toHaveBeenCalledWith(
        'support_articles', file, expect.objectContaining({ table: 'support_articles' })
      );
      expect(screen.getByText(/Inserted 4 chunk\(s\) using/)).toBeInTheDocument();
      expect(screen.getByText(/local-hashing-v1/)).toBeInTheDocument();
    });
  });

  it('Ingest is refused with a real client-side check when no file is chosen', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add file'));
    await waitFor(() => expect(screen.getByText('Ingest')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Ingest'));

    expect(api.ingestDocument).not.toHaveBeenCalled();
  });

  it('chunking & metadata options are hidden by default and can be revealed and edited', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add file'));
    await waitFor(() => expect(screen.getByText(/Show chunking & metadata options/)).toBeInTheDocument());
    expect(screen.queryByText('Max words / chunk')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/Show chunking & metadata options/));
    await waitFor(() => expect(screen.getByText('Max words / chunk')).toBeInTheDocument());
  });

  it('Back returns to the knowledge base list, not to Integrations', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(screen.getByText('Add file')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Back'));
    await waitFor(() => expect(screen.queryByText('Add file')).not.toBeInTheDocument());
    expect(screen.getByText('Support Articles')).toBeInTheDocument();
  });
});
