import ConfirmPanel from '../components/ConfirmPanel';
import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import { PageHead, Banner, SearchNote } from '../components/ui';
import { announceSearchCleared } from '../searchEvents';
import KnowledgeBaseDetail from '../components/KnowledgeBaseDetail';
import { useApp } from '../context/AppContext';
import type { KnowledgeBase } from '../types';

const CHUNK_LABELS: Record<string, string> = { general: 'General', qa: 'Q&A', parent_child: 'Parent-Child' };
const RETRIEVAL_LABELS: Record<string, string> = { vector: 'Vector Search', fulltext: 'Full-Text Search', hybrid: 'Hybrid Search' };

/** What this pipeline is connected to, in terms that suit its provider.
 *  pgvector has a user/host/database; a hosted index has a URL. */
function connectionSummary(kb: KnowledgeBase): string {
  const c = kb.config as Record<string, any>;
  if (kb.provider === 'pgvector') {
    return `${c.user}@${c.host}:${c.port}/${c.dbname}`;
  }
  try {
    return c.url ? new URL(String(c.url)).host : `${kb.provider} index`;
  } catch {
    return String(c.url || `${kb.provider} index`);
  }
}

/** Where the chunks live: a table for pgvector, a collection/index for
 *  everything else. */
function storageSummary(kb: KnowledgeBase): string {
  const c = kb.config as Record<string, any>;
  if (kb.provider === 'pgvector') return c.table || '(no table yet)';
  return c.index || c.namespace || '(no collection yet)';
}

export default function RAG() {
  const { openModal, toast, dataVersion } = useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [detailKb, setDetailKb] = useState<KnowledgeBase | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [search, setSearch] = useState(searchParams.get('q') ?? '');
  // The top-bar search lands here with ?q=; follow it while already on this page too.
  useEffect(() => { const q = searchParams.get('q'); if (q !== null) setSearch(q); }, [searchParams]);
  // Clears the filter and the ?q= that set it, so a reload doesn't bring it back.
  const clearSearch = () => {
    setSearch('');
    setSearchParams((p) => { const n = new URLSearchParams(p); n.delete('q'); return n; }, { replace: true });
    announceSearchCleared();
  };
  // Grid or list, remembered in this browser only (storage can be blocked,
  // so every read and write is guarded and grid is the fallback).
  const [view, setViewState] = useState<'grid' | 'list'>(() => {
    try { return localStorage.getItem('rag.view') === 'list' ? 'list' : 'grid'; } catch { return 'grid'; }
  });
  const setView = (v: 'grid' | 'list') => {
    setViewState(v);
    try { localStorage.setItem('rag.view', v); } catch { /* per-browser convenience only */ }
  };
  const load = () => api.listKnowledgeBases().then(setKbs).catch(() => toast('Could not reach the real backend to list knowledge bases'));
  // dataVersion: saving credentials in the Edit connection panel bumps it, so
  // the card's status ("password unreadable" → connected) updates without a reload.
  useEffect(() => { load(); }, [dataVersion]);

  // The card itself opens the pipeline on click, so this has to
  // stopPropagation or deleting would also navigate into the detail view.
  // Note what the API actually does: it removes the pipeline registration
  // and its local document/metadata/retrieval-test bookkeeping, but
  // deliberately does NOT drop the remote pgvector table — the vectors
  // survive, so the wording here must not promise otherwise.
  const [deletingKb, setDeletingKb] = useState<KnowledgeBase | null>(null);

  const deletePipeline = (e: React.MouseEvent, kb: KnowledgeBase) => {
    e.stopPropagation();
    if (deletingId) return;
    setDeletingKb(kb);
  };

  const reallyDeletePipeline = (kb: KnowledgeBase) => {
    setDeletingId(kb.id);
    api.deleteKnowledgeBase(kb.id)
      .then(() => {
        setKbs((prev) => prev.filter((k) => k.id !== kb.id));
        toast(`Deleted ${kb.name}`);
      })
      .catch((err) => toast(err.message))
      .finally(() => setDeletingId(null));
  };

  // A freshly created pipeline (New RAG Pipeline modal) lands here via
  // ?open=<id> — the Studio "create, then jump straight into the editor"
  // pattern, applied to knowledge bases. Reacts to searchParams itself
  // (not kbs) and does its own fresh fetch: navigating from "/rag" to
  // "/rag?open=X" doesn't remount this component (same matched route),
  // so a mount-only effect would never see it, and the just-created
  // pipeline isn't in this component's existing `kbs` state yet either.
  useEffect(() => {
    const openId = searchParams.get('open');
    if (!openId) return;
    api.listKnowledgeBases()
      .then((all) => {
        setKbs(all);
        const kb = all.find((k) => k.id === openId);
        if (kb) setDetailKb(kb);
        setSearchParams({}, { replace: true });
      })
      .catch(() => toast('Could not open the new pipeline — try selecting it from the list below'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  if (detailKb) {
    return (
      <div className="wrap wide">
        <KnowledgeBaseDetail kb={detailKb} onBack={() => { setDetailKb(null); load(); }} />
      </div>
    );
  }

  // Split by what a pipeline can actually DO, as reported by the
  // backend, rather than by provider name. The old `=== 'pgvector'`
  // test put Pinecone, Qdrant, Weaviate and Milvus in a second-class
  // list under a caption saying ingestion "isn't wired" for them —
  // which stopped being true once it was.
  const pipelineKbs = kbs.filter((k) => k.ingest_supported !== false);
  const otherKbs = kbs.filter((k) => k.ingest_supported === false);
  // Matches what each card/row shows: name, id, provider, where it
  // connects, status, and its description.
  const q = search.trim().toLowerCase();
  const shownKbs = !q ? pipelineKbs : pipelineKbs.filter((k) =>
    [k.name, k.id, k.provider, connectionSummary(k), k.status, k.config.description as string]
      .some((v) => (v || '').toLowerCase().includes(q)));

  const statusChip = (kb: KnowledgeBase) => (kb.secrets_usable === false
    ? <span className="chip amber" title="Re-enter this pipeline's credentials with the edit button">password unreadable</span>
    : <span className={`chip ${kb.status === 'connected' ? 'green' : kb.status === 'failed' ? 'amber' : 'line'}`}>{kb.status}</span>);
  const retrievalLabel = (kb: KnowledgeBase) => (kb.supports_fulltext === false
    ? 'Vector Search'
    : RETRIEVAL_LABELS[kb.config.retrieval_method as string] || 'Vector Search');

  return (
    <div className="wrap">
      <PageHead
        title="RAG" eyebrow="Retrieval-Augmented Generation"
        subtitle="Every real RAG pipeline you can ingest into, configure, and query — click one to open it, or query it directly over its own real Retrieval API."
        actions={<button className="btn" onClick={() => openModal('newRagPipeline')}><Icon name="i-plus" /> New RAG Pipeline</button>}
      />
      <Banner kind="good">
        Each pipeline's real connection (host/credentials/provider) is cataloged under{' '}
        <Link to="/integrations" className="link">Integrations → KB Connections</Link> too — connections created
        there show up here automatically, with their
        real document pipeline (ingestion, chunking, embedding, Documents/Chunks/Metadata, Retrieval Testing) and
        a real external Retrieval API, callable with its own API key from outside this platform.
      </Banner>

      {pipelineKbs.length > 0 && (
        <div className="sticky-head row-split rag-toolbar">
          <span className="small muted">
            {q ? `${shownKbs.length} of ${pipelineKbs.length} pipelines` : `${pipelineKbs.length} pipeline${pipelineKbs.length === 1 ? '' : 's'}`}
          </span>
          <div className="row-split" style={{ gap: 10 }}>
          <SearchNote term={search} onClear={clearSearch} />
          <div className="view-toggle" role="group" aria-label="Layout">
            <button type="button" className={view === 'list' ? 'active' : ''} aria-pressed={view === 'list'}
              title="List view" aria-label="List view" onClick={() => setView('list')}><Icon name="i-list" /></button>
            <button type="button" className={view === 'grid' ? 'active' : ''} aria-pressed={view === 'grid'}
              title="Grid view" aria-label="Grid view" onClick={() => setView('grid')}><Icon name="i-grid" /></button>
          </div>
          </div>
        </div>
      )}

      {pipelineKbs.length > 0 && q && shownKbs.length === 0 && (
        <div className="empty-cfg" style={{ padding: 40 }}>
          No pipeline matches "{search.trim()}".{' '}
          <button type="button" className="icon-action sm" title="Clear search" aria-label="Clear search" onClick={clearSearch}><Icon name="i-x" /></button>
        </div>
      )}

      {shownKbs.length > 0 && view === 'list' && (
        <div className="card rag-list">
          <table>
            <thead><tr>
              <th>Pipeline</th><th>Status</th><th>Connection</th><th>Chunking</th><th>Retrieval</th><th>API</th><th />
            </tr></thead>
            <tbody>
              {shownKbs.map((kb) => (
                <tr key={kb.id} className="click" onClick={() => setDetailKb(kb)}>
                  <td>
                    <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                      <span className="studio-card-icon sm" style={{ background: 'var(--teal)' }}>
                        {kb.config.icon || <Icon name="i-book" />}
                      </span>
                      <div style={{ minWidth: 0 }}>
                        <b>{kb.name}</b>
                        <div className="small muted" style={{ textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5 }}>
                          {kb.provider === 'pgvector' ? 'pgvector' : kb.provider}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>{statusChip(kb)}</td>
                  <td className="mono small muted rag-list-conn" title={connectionSummary(kb)}>{connectionSummary(kb)}</td>
                  <td><span className="chip line sm">{CHUNK_LABELS[kb.config.chunk_structure as string] || 'General'}</span></td>
                  <td><span className="chip blue sm">{retrievalLabel(kb)}</span></td>
                  <td>{kb.config.retrieval_api_enabled
                    ? <span className="chip teal sm"><Icon name="i-plug" /> enabled</span>
                    : <span className="small muted">—</span>}</td>
                  <td>
                    <div className="row-split" style={{ gap: 6, justifyContent: 'flex-end' }}>
                      <button className="studio-card-edit visible" title={`Edit the "${kb.name}" connection credentials`}
                        aria-label={`Edit the ${kb.name} connection credentials`}
                        onClick={(e) => { e.stopPropagation(); openModal('editKbConnection', kb.id); }}>
                        <Icon name="i-pencil" />
                      </button>
                      <button className="studio-card-delete visible" title={`Delete the "${kb.name}" pipeline`}
                        aria-label={`Delete the ${kb.name} pipeline`} disabled={deletingId === kb.id}
                        onClick={(e) => deletePipeline(e, kb)}>
                        <Icon name="i-trash" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {shownKbs.length > 0 && view === 'grid' && (
        <div className="studio-grid">
          {shownKbs.map((kb) => (
            <div key={kb.id} className="studio-card" onClick={() => setDetailKb(kb)}>
              <div className="row-split" style={{ alignItems: 'flex-start' }}>
                <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                  <span className="studio-card-icon" style={{ background: 'var(--teal)', fontSize: 18 }}>
                    {kb.config.icon || <Icon name="i-book" />}
                  </span>
                  <div>
                    <div className="studio-card-title">{kb.name}</div>
                    <div className="small muted" style={{ textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5 }}>
                      {kb.provider === 'pgvector' ? 'RAG Pipeline' : `RAG Pipeline · ${kb.provider}`}
                    </div>
                  </div>
                </div>
                {/* "connected" over a password nothing can decrypt is the
                    same lie the provider and data-source lists told: the
                    card reads as ready and every ingest fails. */}
                {statusChip(kb)}
              </div>
              <div className="small muted studio-card-desc">
                {/* A hosted index has no user@host/db to show, and
                    rendering one produced "undefined@undefined:...". */}
                {kb.config.description || connectionSummary(kb)}
              </div>
              <div className="row-split" style={{ marginTop: 10, gap: 6, flexWrap: 'wrap', justifyContent: 'flex-start' }}>
                <span className="chip line sm">{CHUNK_LABELS[kb.config.chunk_structure as string] || 'General'}</span>
                <span className="chip blue sm">
                  {retrievalLabel(kb)}
                </span>
                {kb.config.retrieval_api_enabled && <span className="chip teal sm"><Icon name="i-plug" /> API enabled</span>}
              </div>
              <div className="studio-card-footer row-split">
                <span className="mono small muted">{storageSummary(kb)}</span>
                <div className="row-split" style={{ gap: 6 }}>
                {/* Appears on hover. Opens the same Edit connection panel as
                    Integrations > KB Connections, so an unreadable password or
                    API key can be re-entered from the card that reports it. */}
                <button
                  className="studio-card-edit"
                  title={`Edit the "${kb.name}" connection credentials`}
                  aria-label={`Edit the ${kb.name} connection credentials`}
                  onClick={(e) => { e.stopPropagation(); openModal('editKbConnection', kb.id); }}
                >
                  <Icon name="i-pencil" />
                </button>
                <button
                  className="studio-card-delete visible"
                  title={`Delete the "${kb.name}" pipeline`}
                  aria-label={`Delete the ${kb.name} pipeline`}
                  disabled={deletingId === kb.id}
                  onClick={(e) => deletePipeline(e, kb)}
                >
                  <Icon name="i-trash" />
                </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {otherKbs.length > 0 && (
        <>
          <div className="row-split" style={{ margin: '20px 0 14px' }}>
            <span className="small muted" style={{ textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Connected, but no ingestion adapter</span>
          </div>
          {otherKbs.map((kb) => (
            <div className="card pad" key={kb.id} style={{ marginBottom: 10 }}>
              <div className="row-split">
                <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                  <b>{kb.name}</b><span className="chip line">{kb.provider}</span>
                </div>
                <Link to="/integrations" className="btn sm ghost"><Icon name="i-cog" /> Manage connection</Link>
              </div>
              <p className="small muted" style={{ marginTop: 8, marginBottom: 0 }}>
                {/* Only providers the backend reports as having no
                    ingest path reach this list, so the text no longer
                    names pgvector as the sole supported one. */}
                The connection is stored and reachability is checked, but there is no ingestion adapter
                for provider '{kb.provider}', so documents cannot be written to it from here. pgvector,
                Pinecone, Qdrant, Weaviate and Milvus each have one.
              </p>
            </div>
          ))}
        </>
      )}

      {kbs.length === 0 && (
        <div className="empty-cfg" style={{ padding: '60px 10px' }}>
          <p style={{ marginTop: 0 }}>No RAG pipelines yet.</p>
          <button className="btn sm" onClick={() => openModal('newRagPipeline')}><Icon name="i-plus" /> Create your first pipeline</button>
        </div>
      )}
    {deletingKb && (
      <ConfirmPanel
        title={`Delete the ${deletingKb.name} pipeline?`} confirmLabel="Delete pipeline"
        body={<>
          This removes the pipeline and its document, metadata and retrieval-test records.
          <p style={{ margin: '10px 0 0' }}>
            The pgvector table <span className="mono">
              {(deletingKb.config.table as string) || 'its table'}
            </span> and the vectors already in it are <b>left untouched</b>, so you can
            reconnect to them later.
          </p>
        </>}
        onConfirm={() => { reallyDeletePipeline(deletingKb); setDeletingKb(null); }}
        onClose={() => setDeletingKb(null)} />
    )}
    </div>
  );
}
