import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import RAG from '../pages/RAG';
import { api } from '../api';
import type { KnowledgeBase } from '../types';
import { openSelect, optionTexts } from './pickOption';

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

describe('Settings tab (RAG page) — real editable pipeline settings, not a static info panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // DocumentsTab builds its per-document metadata editor from the
    // KB's registered fields, so this call happens on every render.
    vi.mocked(api.listKbMetadataFields).mockResolvedValue([]);
    // The Settings tab populates its embedding-model dropdown from
    // every configured provider, so this call happens on render.
    vi.mocked(api.listEmbeddingModels).mockResolvedValue({ models: [] });
    // The Rerank box reads its state from the server instead of
    // hardcoding it, so every render of the Settings tab asks.
    vi.mocked(api.getRerankAvailability).mockResolvedValue({
      available: true, source: 'Model Providers', kind: 'llm', reason: null,
      catalogued_rerank_models: [],
      models: [
        { name: '', provider: null, builtin: true, available: true, note: 'Platform default (Claude Sonnet 5).' },
        { name: 'Claude Haiku 4.5', provider: 'Anthropic', builtin: true, available: true, note: null },
        { name: 'Llama 3.1 8B Instruct', provider: 'Hugging Face Hub', builtin: false, available: true, note: null },
        { name: 'GPT-4o', provider: 'OpenAI', builtin: true, available: false, note: 'Add an API key for OpenAI to use this model.' },
      ],
    });
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB()]);
    vi.mocked(api.listKbDocuments).mockResolvedValue([]);
    vi.mocked(api.getKbRetrievalTestHistory).mockResolvedValue([]);
  });

  async function openSettingsTab() {
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(document.querySelector('.tab')).toBeInTheDocument());
    // Scoped to the KB-detail tab strip specifically — the sidebar also
    // has its own top-level "Settings" nav item, so an unscoped
    // getByText('Settings') would find both and throw.
    const settingsTab = [...document.querySelectorAll('.tab')].find((t) => t.textContent === 'Settings');
    expect(settingsTab).toBeTruthy();
    fireEvent.click(settingsTab!);
    await waitFor(() => expect(screen.getByText('Vector Search')).toBeInTheDocument());
  }

  it('shows the real Chunk Structure, Index Method, and Retrieval Setting sections, not a static KV list', async () => {
    await openSettingsTab();
    expect(screen.getByText('General')).toBeInTheDocument();
    expect(screen.getByText('High Quality')).toBeInTheDocument();
    expect(screen.getByText('Hybrid Search')).toBeInTheDocument();
  });

  async function enableRerank() {
    await openSettingsTab();
    fireEvent.click(screen.getByText('Hybrid Search'));
    await waitFor(() => expect(screen.getByText('Rerank Model')).toBeInTheDocument());
    const rerankToggle = [...document.querySelectorAll('.tog')].find(
      (t) => t.parentElement?.textContent?.includes('Rerank Model'));
    fireEvent.click(rerankToggle!);
  }

  it('enabling Rerank Model shows a dropdown of models from every configured provider, and saves the choice', async () => {
    vi.mocked(api.updateKbSettings).mockResolvedValue(makeKB());
    await enableRerank();
    const select = await screen.findByRole('button', { name: 'Rerank model' });
    openSelect(select);
    expect(screen.getByRole('option', { name: /Platform default/ })).toBeTruthy();
    const labels = optionTexts();
    // Drawn from Anthropic, Hugging Face and OpenAI — not the single
    // hardcoded provider the old copy named.
    expect(labels.some((l) => l.includes('Claude Haiku 4.5') && l.includes('Anthropic'))).toBe(true);
    expect(labels.some((l) => l.includes('Llama 3.1 8B Instruct') && l.includes('Hugging Face Hub'))).toBe(true);
    // A provider with no key is listed but not selectable, which is how
    // that state is conveyed now that the banner beside it is gone.
    const noKey = screen.getAllByRole('option').find((o) => o.textContent?.includes('GPT-4o'));
    expect(noKey!.getAttribute('aria-disabled')).toBe('true');
    expect(noKey!.textContent).toContain('needs an API key');

    fireEvent.click(screen.getByRole('option', { name: /Llama 3\.1 8B Instruct/ }));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.updateKbSettings).toHaveBeenCalledWith(
      'support_articles', expect.objectContaining({ rerank_model: 'Llama 3.1 8B Instruct' })));
  });

  it('the dropdown is the whole control — no status banner or chip beside it', async () => {
    // Deliberate: the old box was a hardcoded amber warning plus a dead
    // "Check config" chip, and the replacement prose was still noise.
    await enableRerank();
    openSelect(await screen.findByRole('button', { name: 'Rerank model' }));
    expect(screen.getByRole('option', { name: /Platform default/ })).toBeTruthy();
    expect(screen.queryByText('Ready')).not.toBeInTheDocument();
    expect(screen.queryByText('Not configured')).not.toBeInTheDocument();
    expect(screen.queryByText('Check config')).not.toBeInTheDocument();
    expect(screen.queryByText(/Reranking is active/)).not.toBeInTheDocument();
  });

  it('when nothing configured can rerank, one line says so rather than leaving a dead dropdown', async () => {
    vi.mocked(api.getRerankAvailability).mockResolvedValue({
      available: false, source: null, kind: null, reason: 'No Anthropic key is configured.',
      catalogued_rerank_models: [],
      models: [
        { name: '', provider: null, builtin: true, available: false, note: 'Add an API key for Anthropic to use this model.' },
        { name: 'GPT-4o', provider: 'OpenAI', builtin: true, available: false, note: 'Add an API key for OpenAI to use this model.' },
      ],
    });
    await enableRerank();
    await waitFor(() => expect(screen.getByText(/No configured provider can rerank yet/)).toBeInTheDocument());
    const link = screen.getByText('Add a model provider key');
    expect(link.closest('a')).toHaveAttribute('href', '/integrations');
  });


  it('Save persists real settings via the real PATCH .../settings endpoint', async () => {
    vi.mocked(api.updateKbSettings).mockResolvedValue(makeKB({ config: { ...makeKB().config, retrieval_method: 'hybrid' } }));
    await openSettingsTab();
    fireEvent.click(screen.getByText('Hybrid Search'));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => {
      expect(api.updateKbSettings).toHaveBeenCalledWith('support_articles', expect.objectContaining({ retrieval_method: 'hybrid' }));
    });
  });

  it('warns that Chunk Structure changes only affect future ingests once this KB already has documents', async () => {
    vi.mocked(api.listKbDocuments).mockResolvedValue([{
      id: 'doc-1', kb_id: 'support_articles', filename: 'a.txt', chunking_mode: 'General',
      max_words: 180, overlap_words: 30, word_count: 100, chunk_count: 3, retrieval_count: 0,
      status: 'enabled', created_at: '2026-01-01T00:00:00',
    }]);
    await openSettingsTab();
    await waitFor(() => expect(screen.getByText(/This knowledge base already has documents/)).toBeInTheDocument());
  });

  it('the retrieval-method badge on Retrieval Testing reflects the real saved setting and jumps back to Settings', async () => {
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([makeKB({ config: { ...makeKB().config, retrieval_method: 'hybrid' } })]);
    renderPage();
    await waitFor(() => expect(screen.getByText('Support Articles')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Support Articles'));
    await waitFor(() => expect(document.querySelector('.tab')).toBeInTheDocument());
    const retrievalTab = [...document.querySelectorAll('.tab')].find((t) => t.textContent === 'Retrieval Testing');
    fireEvent.click(retrievalTab!);
    await waitFor(() => expect(screen.getByText('HYBRID SEARCH')).toBeInTheDocument());

    fireEvent.click(screen.getByText('HYBRID SEARCH').closest('.retrieval-method-chip')!);
    await waitFor(() => expect(screen.getByText('Vector Search')).toBeInTheDocument());
  });

  it('offers every embedding model the backend reports, including one whose type was never set, and labels why', async () => {
    // The backend qualifies a model either by its declared "Text
    // Embedding" type or by its model id; the dropdown must render both
    // and make the difference visible, since an inferred one also means
    // nobody has confirmed the provider serves it.
    vi.mocked(api.listEmbeddingModels).mockResolvedValue({
      models: [
        { name: 'local-hashing-v1', provider: null, api_model_id: null, embedding_dimension: 384,
          builtin: true, available: true, note: 'Local feature hashing.' },
        { name: 'E5 Large 1024', provider: 'Hugging Face Hub', api_model_id: 'intfloat/multilingual-e5-large',
          embedding_dimension: 1024, builtin: false, available: true, inferred: false, note: null },
        { name: 'embedding model', provider: 'OpenAI', api_model_id: 'intfloat/e5-base-v2',
          embedding_dimension: null, builtin: false, available: true, inferred: true,
          types: ['LLM'], note: 'Listed from its model id — confirm OpenAI serves it.' },
      ],
    });
    await openSettingsTab();

    const select = await screen.findByRole('button', { name: 'Embedding Model' });
    openSelect(select);
    expect(screen.getByRole('option', { name: /E5 Large 1024/ })).toBeTruthy();
    const labels = optionTexts();

    // All three offered, the mis-typed one included.
    expect(labels.some((l) => l.includes('local-hashing-v1'))).toBe(true);
    expect(labels.some((l) => l.includes('E5 Large 1024') && l.includes('1024 dims'))).toBe(true);
    const inferredLabel = labels.find((l) => l.includes('embedding model'));
    expect(inferredLabel).toBeTruthy();
    // ...and flagged in the list itself, not only once selected.
    expect(inferredLabel).toContain('type not set to Text Embedding');
    // The declared one carries no such caveat.
    expect(labels.find((l) => l.includes('E5 Large 1024'))).not.toContain('type not set');
  });
});
