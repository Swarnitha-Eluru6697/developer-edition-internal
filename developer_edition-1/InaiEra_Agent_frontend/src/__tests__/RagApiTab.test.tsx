import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import RAG from '../pages/RAG';
import { api } from '../api';
import type { KnowledgeBase } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function renderPage() {
  return render(<MemoryRouter><AppProvider><RAG /></AppProvider></MemoryRouter>);
}

function makeKB(overrides?: Partial<KnowledgeBase>): KnowledgeBase {
  return {
    id: 'support_articles', name: 'Support Articles', provider: 'pgvector',
    config: { host: '127.0.0.1', port: 5432, dbname: 'customer_kb', user: 'kb_user', table: 'support_articles' },
    status: 'connected', last_error: null, last_checked_at: null, created_at: '2026-01-01T00:00:00',
    ...overrides,
  } as KnowledgeBase;
}

async function openApiTab() {
  renderPage();
  await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
  fireEvent.click(screen.getByText('Support Articles'));
  await waitFor(() => expect(document.querySelector('.tab')).toBeInTheDocument());
  const apiTab = [...document.querySelectorAll('.tab')].find((t) => t.textContent === 'API');
  expect(apiTab).toBeTruthy();
  fireEvent.click(apiTab!);
  await waitFor(() => expect(screen.getByText('Retrieval API')).toBeInTheDocument());
}

describe('API tab — a real, externally-callable Retrieval API per pipeline', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // DocumentsTab builds its per-document metadata editor from the
    // KB's registered fields, so this call happens on every render.
    vi.mocked(api.listKbMetadataFields).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB()]);
    vi.mocked(api.listKbDocuments).mockResolvedValue([]);
  });

  it('is disabled by default and offers no key/curl example until enabled', async () => {
    await openApiTab();
    expect(screen.queryByText('Example request')).not.toBeInTheDocument();
    expect(screen.getByText(/Enable the Retrieval API above/)).toBeInTheDocument();
  });

  it('enabling calls the real settings endpoint, not a local-only toggle', async () => {
    vi.mocked(api.updateKbSettings).mockResolvedValue(makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true } }));
    await openApiTab();
    fireEvent.click(document.querySelector('.tog')!);
    await waitFor(() => {
      expect(api.updateKbSettings).toHaveBeenCalledWith('support_articles', { retrieval_api_enabled: true });
    });
  });

  it('generating a key calls the real regenerate endpoint and reveals the key once, with a real working curl example', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true } })]);
    vi.mocked(api.regenerateKbApiKey).mockResolvedValue(makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true, retrieval_api_key: 'real-token-abc123' } }));
    await openApiTab();

    fireEvent.click(screen.getByText('Generate key'));
    await waitFor(() => {
      expect(api.regenerateKbApiKey).toHaveBeenCalledWith('support_articles');
      expect(screen.getByDisplayValue('real-token-abc123')).toBeInTheDocument();
      expect(screen.getByText(/real-token-abc123/)).toBeInTheDocument(); // in the curl snippet too
    });
  });

  it('regenerating an existing key asks for real confirmation first, since it invalidates the old one immediately', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true, retrieval_api_key: 'old-key' } })]);
    await openApiTab();
    fireEvent.click(screen.getByText('Regenerate key'));
    // Asked in the right-side panel, not a browser popup — and Cancel means no call.
    await waitFor(() => expect(screen.getByText('Regenerate retrieval key')).toBeInTheDocument());
    expect(api.regenerateKbApiKey).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Cancel'));
    await waitFor(() => expect(screen.queryByText('Regenerate retrieval key')).not.toBeInTheDocument());
    expect(api.regenerateKbApiKey).not.toHaveBeenCalled();
  });

  it('Try It calls the real retrieval API with the real revealed key as a bearer token, and shows real scored results', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true } })]);
    vi.mocked(api.regenerateKbApiKey).mockResolvedValue(makeKB({ config: { ...makeKB().config, retrieval_api_enabled: true, retrieval_api_key: 'live-key-xyz' } }));
    vi.mocked(api.callKbRetrievalApi).mockResolvedValue({ results: [{ id: 1, content: 'Reset your password from Settings.', source_filename: 'faq.txt', document_id: null, score: 0.91, metadata: {} }] });
    await openApiTab();

    fireEvent.click(screen.getByText('Generate key'));
    await waitFor(() => expect(screen.getByPlaceholderText('Test query…')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Test query…'), { target: { value: 'how do I reset my password' } });
    fireEvent.click(screen.getByText('Call API'));

    await waitFor(() => {
      expect(api.callKbRetrievalApi).toHaveBeenCalledWith('support_articles', 'live-key-xyz', 'how do I reset my password', 5);
      expect(screen.getByText('Reset your password from Settings.')).toBeInTheDocument();
      expect(screen.getByText('score 0.91')).toBeInTheDocument();
    });
  });
});
