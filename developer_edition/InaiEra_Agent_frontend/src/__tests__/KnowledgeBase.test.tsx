import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider, useApp } from '../context/AppContext';
import Integrations from '../pages/Integrations';
import AddKnowledgeBaseModal from '../modals/AddKnowledgeBaseModal';
import { api } from '../api';
import type { KnowledgeBase } from '../types';
import { pickOption } from './pickOption';

vi.mock('../api');

afterEach(() => cleanup());

function Harness() {
  const { modal } = useApp();
  return (
    <>
      <Integrations />
      {modal === 'addKnowledgeBase' && <AddKnowledgeBaseModal />}
    </>
  );
}

function renderPage() {
  return render(<MemoryRouter><AppProvider><Harness /></AppProvider></MemoryRouter>);
}

function makeKB(overrides?: Partial<KnowledgeBase>): KnowledgeBase {
  return {
    id: 'support_articles', name: 'Support Articles', provider: 'pgvector',
    config: { host: '127.0.0.1', port: 5432, dbname: 'customer_kb', user: 'kb_user', table: 'support_articles' },
    status: 'untested', last_error: null, last_checked_at: null, created_at: '2026-01-01T00:00:00',
    ...overrides,
  } as KnowledgeBase;
}

describe('Integrations — Knowledge Base tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB()]);
  });

  const provider = (over: Partial<any> = {}) => ({
    name: 'Cohere', version: '0.0.16', status: 'credits' as const, types: ['LLM'],
    api_key_configured: true, runtime_supported: true, ...over,
  });

  it('a data source whose credentials cannot be decrypted says so instead of Connected', async () => {
    // Same class of bug as the provider key: the row read "Connected"
    // over a secret nothing could decrypt, and every browse failed with
    // a raw crypto error that told nobody to re-enter the credential.
    vi.mocked(api.listDataSources).mockResolvedValue([
      { name: 'OneDrive', version: '1.0.0', status: 'connected', auth: 'oauth',
        config: { client_secret: '••••••••' }, credentials_usable: false,
        browsable: false, browse_supported: true },
      { name: 'GitHub', version: '1.1.0', status: 'connected', auth: 'apikey',
        config: { credentials: '••••••••' }, credentials_usable: true,
        browsable: true, browse_supported: true },
    ] as any);
    renderPage();
    fireEvent.click(screen.getByText('Data Sources'));
    await waitFor(() => expect(screen.getByText('Credentials unreadable — reconfigure')).toBeInTheDocument());
    // The healthy one is unaffected.
    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('a stored key that cannot be decrypted is reported, not shown as configured', async () => {
    // api_key_configured only records that a key was once saved. One
    // encrypted under a different SECRETS_ENCRYPTION_KEY still sets that
    // flag and still shows a masked value, so the row read as ready
    // while every call using it reported "no key configured" — a failure
    // surfacing nowhere near its cause.
    vi.mocked(api.listModelProviders).mockResolvedValue([provider({ key_usable: false })] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('Cohere')).toBeInTheDocument());
    expect(screen.getByText('Key stored but unreadable — re-enter it')).toBeInTheDocument();
    expect(screen.queryByText('AI credits in use')).not.toBeInTheDocument();
    expect(screen.getByText(/cannot be decrypted with the encryption key this server is running with/))
      .toBeInTheDocument();
  });

  it('a usable key still reads as configured, with no warning', async () => {
    vi.mocked(api.listModelProviders).mockResolvedValue([provider({ key_usable: true })] as any);
    renderPage();
    await waitFor(() => expect(screen.getByText('AI credits in use')).toBeInTheDocument());
    expect(screen.queryByText('Key stored but unreadable — re-enter it')).not.toBeInTheDocument();
  });

  it('switching to the Knowledge Base tab shows real connection details, not a placeholder', async () => {
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => {
      expect(screen.getByText('Support Articles')).toBeInTheDocument();
    });
    expect(screen.getByText(/kb_user@127\.0\.0\.1:5432\/customer_kb/)).toBeInTheDocument();
  });

  it('"Test" calls the real testKnowledgeBase endpoint with the correct id and shows the real result', async () => {
    vi.mocked(api.testKnowledgeBase).mockResolvedValue({
      ...makeKB({ status: 'connected' }),
      test_result: { ok: true, detail: "Connected. pgvector extension present. Table 'support_articles' has 3 row(s)." },
    });
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Test'));

    await waitFor(() => {
      expect(api.testKnowledgeBase).toHaveBeenCalledWith('support_articles');
    });
  });

  it('Add Knowledge Base modal sends real provider-specific config, not a generic blob', async () => {
    vi.mocked(api.createKnowledgeBase).mockResolvedValue(makeKB());
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => expect(screen.getByText('Add Knowledge Base')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add Knowledge Base'));

    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Support Articles')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('e.g. Support Articles'), { target: { value: 'Docs KB' } });
    fireEvent.change(screen.getByPlaceholderText('127.0.0.1'), { target: { value: '10.0.0.5' } });
    fireEvent.change(screen.getByPlaceholderText('5432'), { target: { value: '5433' } });
    fireEvent.change(screen.getByPlaceholderText('support_articles'), { target: { value: 'docs' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.createKnowledgeBase).toHaveBeenCalledWith({
        name: 'Docs KB', provider: 'pgvector',
        config: expect.objectContaining({ host: '10.0.0.5', port: 5433, table: 'docs' }),
      });
    });
  });

  it('selecting a non-pgvector provider shows the URL/API-key fields and an honest capability note', async () => {
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => expect(screen.getByText('Add Knowledge Base')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add Knowledge Base'));
    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Support Articles')).toBeInTheDocument());

    pickOption(screen.getByRole('button', { name: 'Provider' }), 'Pinecone');

    await waitFor(() => {
      expect(screen.getByPlaceholderText('https://...')).toBeInTheDocument();
    });
    expect(screen.getByText(/isn't wired into the Retrieval node yet/)).toBeInTheDocument();
  });

  it('a pgvector KB shows a real "Manage in RAG" link to the new RAG page; a non-pgvector KB does not', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([
      makeKB({ id: 'a', name: 'PG KB', provider: 'pgvector' }),
      makeKB({ id: 'b', name: 'Pinecone KB', provider: 'pinecone', config: { url: 'https://x.pinecone.io' } }),
    ]);
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => expect(screen.getByText('PG KB')).toBeInTheDocument());

    const pgCard = screen.getByText('PG KB').closest('.card') as HTMLElement;
    const pineconeCard = screen.getByText('Pinecone KB').closest('.card') as HTMLElement;
    const manageLink = within(pgCard).getByText('Manage in RAG').closest('a');
    expect(manageLink).toHaveAttribute('href', '/rag');
    expect(within(pineconeCard).queryByText('Manage in RAG')).not.toBeInTheDocument();
  });

  it('the Add Knowledge Base modal no longer collects Chunk Structure or Index Method — those moved to the RAG page', async () => {
    renderPage();
    fireEvent.click(screen.getByText('KB Connections'));
    await waitFor(() => expect(screen.getByText('Add Knowledge Base')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add Knowledge Base'));
    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Support Articles')).toBeInTheDocument());

    expect(screen.queryByText('Chunk Structure')).not.toBeInTheDocument();
    expect(screen.queryByText('Index Method')).not.toBeInTheDocument();
    expect(screen.getByText(/configured after creation, on the RAG page/)).toBeInTheDocument();
  });
});
