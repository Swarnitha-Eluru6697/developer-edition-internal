import { useEffect, useRef, useState } from 'react';
import { confirmAction } from './ConfirmPanel';
import Icon from './Icon';
import SearchSelect from './SearchSelect';
import UploadDestinationModal from './UploadDestinationModal';
import { Banner } from './ui';
import { ChunkStructurePicker, IndexMethodPicker, RetrievalSettingPicker } from './PipelineControls';
import type { EmbeddingModelOption } from './PipelineControls';
import { api, BASE } from '../api';
import { useApp } from '../context/AppContext';
import { useFitToViewport } from '../hooks/useFitToViewport';
import type {
  KnowledgeBase, KnowledgeDocument, KnowledgeChunk, KnowledgeMetadataField,
  KnowledgeRetrievalTestResult, KnowledgeRetrievalTestHistoryEntry, KBIngestResult, KBSettings, KBRetrieveResult,
  DataSource, DataSourceEntry, SourceIngestResult, StorageTarget,
} from '../types';

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return <div className={`tog${on ? ' on' : ''}`} onClick={() => onChange(!on)} />;
}

/** A readable size. Rounding up to "1 KB" reported a 0-byte .gitkeep as
 *  though it held something, and an empty file is worth knowing about —
 *  it produces no chunks and the ingest reports it as a failure. */
function fileSizeLabel(size: number | null | undefined): string {
  if (size == null) return '';
  if (size === 0) return 'empty';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/** Splits a selection into individual files and whole folders. Folders
 *  travel as `folder_paths` and are expanded server-side; sending a
 *  folder id as a file id would try to fetch a directory as a document. */
export function splitPicks(picked: Map<string, SourcePick>) {
  const file_ids: string[] = [];
  const folder_paths: string[] = [];
  picked.forEach((pick, id) => {
    if (pick.isDir) folder_paths.push(id); else file_ids.push(id);
  });
  return { file_ids, folder_paths };
}

/** A file queued for ingest. `path` is the readable location the
 *  breadcrumb trail walked to — for GitHub and Google Cloud Storage it
 *  is also the id the API addresses, which is what lets the Upload API
 *  snippet show a complete, hand-writable path. */
export type SourcePick = {
  name: string;
  path: string;
  // A folder is sent as a folder_path and expanded server-side into its
  // files; only files can actually be fetched.
  isDir?: boolean;
};

/** One level of a Data Source, with a breadcrumb trail and a selection
 *  that survives navigation — so files from several different folders
 *  can be queued in a single ingest. */
function SourceBrowser({ source, selected, onToggle, onToggleMany, multiple = true }: {
  source: string;
  selected: Map<string, SourcePick>;
  onToggle: (entry: DataSourceEntry, path: string) => void;
  onToggleMany?: (picks: [string, SourcePick][], select: boolean) => void;
  multiple?: boolean;
}) {
  // [{ path, label }] — index 0 is the source root.
  const [trail, setTrail] = useState<{ path: string; label: string }[]>([{ path: '', label: source }]);
  const [entries, setEntries] = useState<DataSourceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // Why this listing looks the way it does, when that needs saying.
  const [notice, setNotice] = useState<string | null>(null);
  const here = trail[trail.length - 1];

  // Reset to the root whenever the chosen source changes, otherwise the
  // trail would keep paths that mean nothing to the new source.
  useEffect(() => { setTrail([{ path: '', label: source }]); }, [source]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    api.browseDataSource(source, here.path)
      .then((r) => { if (!cancelled) { setEntries(r.entries); setNotice(r.notice || null); } })
      // The provider's own message is shown verbatim — "Bad
      // credentials" is far more useful than "could not browse".
      .catch((e) => { if (!cancelled) { setEntries([]); setNotice(null); setError(e.message); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [source, here.path]);

  const open = (entry: DataSourceEntry) => setTrail((t) => [...t, { path: entry.path, label: entry.name }]);
  const jump = (i: number) => setTrail((t) => t.slice(0, i + 1));

  // The folder chain below the source root, which is the readable
  // location a picked file sits at.
  const dirPath = trail.slice(1).map((c) => c.label).join('/');
  const pathOf = (entry: DataSourceEntry) => (dirPath ? `${dirPath}/${entry.name}` : entry.name);

  const files = entries.filter((e) => !e.is_dir && e.ingestible);
  const allPicked = files.length > 0 && files.every((e) => selected.has(e.id));

  return (
    <div className="ds-browser">
      <div className="ds-crumbs small">
        {trail.map((c, i) => (
          <span key={`${c.path}-${i}`}>
            {i > 0 && <span className="muted"> / </span>}
            {i === trail.length - 1
              ? <b>{c.label}</b>
              : <span className="link" style={{ cursor: 'pointer' }} onClick={() => jump(i)}>{c.label}</span>}
          </span>
        ))}
      </div>
      {/* Shown above the rows, because it explains what the rows ARE. */}
      {notice && !loading && !error && (
        <div className="small muted" style={{ padding: '6px 0', borderBottom: '1px solid var(--line)', marginBottom: 6 }}>
          {notice}
        </div>
      )}
      {loading && <div className="small muted" style={{ padding: '8px 0' }}>Loading…</div>}
      {error && !loading && <div className="small" style={{ padding: '8px 0', color: 'var(--danger, #b4231d)' }}>{error}</div>}
      {!loading && !error && entries.length === 0 && (
        <div className="small muted" style={{ padding: '8px 0' }}>This folder is empty.</div>
      )}
      {/* Taking a whole folder is the common case, and ticking thirty
          boxes by hand is not a reasonable way to ask for it. */}
      {!loading && !error && multiple && onToggleMany && files.length > 1 && (
        <label className="small muted ds-row" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 4 }}>
          <span className="ds-name">
            <input
              type="checkbox"
              checked={allPicked}
              onChange={() => onToggleMany(files.map((e) => [e.id, { name: e.name, path: pathOf(e) }]), !allPicked)}
            />
            <span className="ds-label">{allPicked ? 'Clear' : 'Select'} all {files.length} files in this folder</span>
          </span>
        </label>
      )}
      {!loading && !error && entries.length > 0 && (
        <ul className="ds-list">
          {entries.map((e) => (
            <li key={e.id} className="ds-row">
              {e.is_dir ? (
                /* Two actions on one row, deliberately separate: the box
                   takes the whole folder, the name opens it. Without the
                   box a folder could only be browsed, never selected. */
                <span className="ds-name">
                  {multiple && (
                    <input
                      type="checkbox"
                      title={`Select everything in ${e.name}`}
                      aria-label={`Select folder ${e.name}`}
                      checked={selected.has(e.id)}
                      onChange={() => onToggle(e, pathOf(e))}
                    />
                  )}
                  <span className="link ds-label" style={{ cursor: 'pointer' }} onClick={() => open(e)}>
                    {e.name}
                  </span>
                </span>
              ) : (
                <label className={`ds-name${e.ingestible ? '' : ' muted'}`}>
                  <input
                    type={multiple ? 'checkbox' : 'radio'}
                    name={multiple ? undefined : 'ds-single-pick'}
                    disabled={!e.ingestible}
                    checked={selected.has(e.id)}
                    onChange={() => onToggle(e, pathOf(e))}
                  />
                  <span className="ds-label">{e.name}</span>
                </label>
              )}
              <span className="small muted">
                {e.note || fileSizeLabel(e.size)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AddFilePanel({ kb, onClose, onIngested }: { kb: KnowledgeBase; onClose: () => void; onIngested: () => void }) {
  const { toast } = useApp();
  const [file, setFile] = useState<File | null>(null);
  // Prefilled from whichever field this provider actually uses, so a
  // hosted pipeline does not open with an empty box it then refuses.
  const [table, setTable] = useState(
    String(kb.config.table || kb.config.index || ''));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [maxWords, setMaxWords] = useState(180);
  const [overlapWords, setOverlapWords] = useState(30);
  const [metadataText, setMetadataText] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<KBIngestResult | null>(null);
  const speech = useTranscription();
  // Where a kept file would actually go — asked of the server, because
  // only it knows whether that is a real bucket or this machine's disk.
  const [storage, setStorage] = useState<StorageTarget | null>(null);
  // The destination THIS upload goes to. Starts as whatever is default
  // and is only changed by the dialog, so an upload never quietly
  // retargets itself.
  const [destinationId, setDestinationId] = useState<string | null>(null);
  const [destOpen, setDestOpen] = useState(false);
  // Ticked by default: this is the point of the option, and the box is
  // right here to untick. The API's own default is the opposite, so an
  // existing integration does not start filling a bucket silently.
  const [keepOriginal, setKeepOriginal] = useState(true);
  useEffect(() => {
    let alive = true;
    api.getStorageTarget()
      .then((r) => { if (alive) { setStorage(r); setDestinationId(r.destination_id || null); } })
      .catch(() => { if (alive) setStorage(null); });
    return () => { alive = false; };
  }, []);

  /** The destination this upload will use, out of what the server
   *  reported — never a label assembled in the browser. */
  const chosenDestination = (storage?.destinations || []).find((d) => d.id === destinationId);
  const destinationLabel = chosenDestination?.label || storage?.label || '';
  const destinationUsable = chosenDestination ? chosenDestination.configured : !!storage?.configured;

  // Data sources that can actually be browsed: connected, credentials
  // genuinely stored, and a browse adapter implemented for them.
  const [sources, setSources] = useState<DataSource[]>([]);
  // '' means the local machine — always available, and the only option
  // when nothing is configured on the Integrations page.
  const [origin, setOrigin] = useState('');
  // file id -> {name, path}, kept across folder navigation so a
  // selection can span multiple folders in one ingest.
  const [picked, setPicked] = useState<Map<string, SourcePick>>(new Map());
  const [sourceResult, setSourceResult] = useState<SourceIngestResult | null>(null);

  useEffect(() => {
    api.listDataSources()
      .then((rows) => setSources(rows.filter((r) => r.browsable)))
      .catch(() => setSources([]));
  }, []);

  const togglePick = (entry: DataSourceEntry, path: string) => setPicked((prev) => {
    const next = new Map(prev);
    if (next.has(entry.id)) next.delete(entry.id);
    else next.set(entry.id, { name: entry.name, path, isDir: entry.is_dir });
    return next;
  });
  const toggleMany = (picks: [string, SourcePick][], select: boolean) => setPicked((prev) => {
    const next = new Map(prev);
    picks.forEach(([id, pick]) => { if (select) next.set(id, pick); else next.delete(id); });
    return next;
  });

  const parsedMetadata = (): Record<string, any> | undefined | false => {
    if (!metadataText.trim()) return undefined;
    try { return JSON.parse(metadataText); }
    catch { toast('Metadata must be valid JSON, e.g. {"tier": "premium"}'); return false; }
  };

  // pgvector writes into a named table and cannot invent one; a hosted
  // store falls back to its configured index, or the pipeline id.
  const storageRequired = kb.provider === 'pgvector';
  const storageMissing = storageRequired && !table.trim();

  const runLocal = () => {
    if (!file) { toast('Choose a file first'); return; }
    if (storageMissing) { toast('A target table name is required'); return; }
    const metadata = parsedMetadata();
    if (metadata === false) return;
    setBusy(true);
    setResult(null);
    api.ingestDocument(kb.id, file, { table: table.trim(), maxWords, overlapWords, metadata,
                                      keepOriginal: keepOriginal && destinationUsable,
                                      storageDestinationId: destinationId || undefined })
      .then((r) => {
        setResult(r);
        // An audio file's text came from a speech model, not from the
        // file — worth saying, because the transcript is what retrieval
        // will search and it is only as good as that model was.
        toast(r.transcription
          ? `Transcribed by ${r.transcription.provider} — ${r.chunks_inserted} chunk(s) inserted using ${r.embed_model}`
          : `${r.chunks_inserted} chunk(s) inserted using ${r.embed_model}`);
        onIngested();
      })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  const runFromSource = () => {
    if (picked.size === 0) { toast('Select at least one file'); return; }
    if (storageMissing) { toast('A target table name is required'); return; }
    const metadata = parsedMetadata();
    if (metadata === false) return;
    setBusy(true);
    setSourceResult(null);
    api.ingestFromDataSource(kb.id, {
      source: origin,
      ...splitPicks(picked),
      table: table.trim(),
      max_words: maxWords,
      overlap_words: overlapWords,
      metadata: metadata ? JSON.stringify(metadata) : undefined,
      keep_original: keepOriginal && destinationUsable,
      storage_destination_id: destinationId || undefined,
    })
      .then((r) => {
        setSourceResult(r);
        // Partial success is the normal case with a folder of mixed
        // files, so both counts are reported rather than just a win.
        toast(r.failed === 0
          ? `Ingested ${r.ingested} file(s) from ${origin}`
          : `Ingested ${r.ingested} of ${r.requested} file(s) from ${origin} — ${r.failed} failed`);
        if (r.ingested > 0) { setPicked(new Map()); onIngested(); }
      })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="card pad" style={{ marginBottom: 14 }}>
      <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Add file</b><button className="btn sm ghost" onClick={onClose}><Icon name="i-x" /></button></div>
      {sources.length > 0 ? (
        <div className="field" style={{ marginBottom: 8 }}>
          <label id="kb-ingest-origin-label" htmlFor="kb-ingest-origin">Source</label>
          <SearchSelect
            id="kb-ingest-origin" labelledBy="kb-ingest-origin-label" noun="source"
            value={origin}
            options={[
              { value: '', label: 'My computer (upload a file)' },
              ...sources.map((s) => ({ value: s.name, label: s.name })),
            ]}
            onChange={(v) => { setOrigin(v); setPicked(new Map()); setSourceResult(null); }}
          />
        </div>
      ) : (
        // Nothing configured — say where sources come from rather than
        // silently offering only local upload.
        <p className="small muted" style={{ marginTop: 0 }}>
          Connect Google Drive, SharePoint, OneDrive, Google Cloud Storage, Notion or GitHub on the
          Integrations page to ingest straight from them.
        </p>
      )}
      {origin === '' ? (
        <div className="field" style={{ marginBottom: 8 }}>
          <label>Document</label>
          <input
            type="file"
            accept={'.txt,.md,.docx,.pdf,.csv,.json,.html,.xml' + (speech?.available ? `,${AUDIO_ACCEPT}` : '')}
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          {speech?.available && (
            <p className="hint">
              Audio ({audioExtensionList(speech.extensions)}) is transcribed on ingest
              via {speech.providers.join(' or ')}, and the transcript is what gets chunked and embedded.
            </p>
          )}
        </div>
      ) : (
        <div className="field" style={{ marginBottom: 8 }}>
          <label>Files in {origin}</label>
          <SourceBrowser source={origin} selected={picked} onToggle={togglePick} onToggleMany={toggleMany} />
          <div className="small muted" style={{ marginTop: 6 }}>
            {picked.size === 0
              ? 'Click a folder name to browse it, or tick its box to take everything inside. Selections are kept as you navigate, so you can combine files from several folders.'
              : (() => {
                  const dirs = Array.from(picked.values()).filter((p) => p.isDir);
                  const files = Array.from(picked.values()).filter((p) => !p.isDir);
                  const parts = [
                    files.length ? `${files.length} file(s)` : '',
                    dirs.length ? `${dirs.length} folder(s) (everything inside)` : '',
                  ].filter(Boolean);
                  return `${parts.join(' and ')} selected: ${Array.from(picked.values()).map((p) => p.path).join(', ')}`;
                })()}
          </div>
        </div>
      )}
      <div className="field" style={{ marginBottom: 8 }}>
        {/* A hosted store has a collection/index, not a table, and the
            ingest endpoint ignores `table` for those providers. */}
        <label>{kb.provider === 'pgvector' ? 'Target table' : 'Collection'}</label>
        <input
          placeholder={kb.provider === 'pgvector' ? 'e.g. kb_support_docs' : 'e.g. support_docs'}
          value={table}
          onChange={(e) => setTable(e.target.value)}
        />
        {kb.provider !== 'pgvector' && (
          <p className="hint">
            Created in {kb.provider} on first ingest if it does not exist, sized to this pipeline&apos;s
            embedding model. Leave as-is to keep using{' '}
            <span className="mono">{String(kb.config.index || kb.id)}</span>.
          </p>
        )}
      </div>
      <div className="small link" onClick={() => setAdvancedOpen(!advancedOpen)} style={{ marginBottom: 8, display: 'inline-block' }}>
        {advancedOpen ? 'Hide' : 'Show'} chunking &amp; metadata options
      </div>
      {advancedOpen && (
        <>
          <div className="row-split" style={{ gap: 10, marginBottom: 8 }}>
            <div className="field" style={{ flex: 1, margin: 0 }}>
              <label>Max words / chunk</label>
              <input type="number" value={maxWords} onChange={(e) => setMaxWords(Number(e.target.value) || 180)} />
            </div>
            <div className="field" style={{ flex: 1, margin: 0 }}>
              <label>Overlap words</label>
              <input type="number" value={overlapWords} onChange={(e) => setOverlapWords(Number(e.target.value) || 0)} />
            </div>
          </div>
          <div className="field" style={{ marginBottom: 8 }}>
            <label>Metadata (JSON, applied to every chunk from this file)</label>
            <textarea rows={2} className="mono" placeholder='{"tier": "premium"}' value={metadataText} onChange={(e) => setMetadataText(e.target.value)} />
          </div>
        </>
      )}
      {storage && (
        <div className="kb-option">
          <div className="kb-option-h">
            <span className="kb-option-title">
              <b className="small">Also keep the original file</b>
              {/* The destination as its own chip rather than inside the
                  sentence: it is the part that varies, it is what
                  someone checks before ticking the box, and "in S3"
                  versus "on this server's disk" do not share a
                  preposition. */}
              <span className="chip line">{destinationLabel}</span>
              <button className="link-btn small" onClick={() => setDestOpen(true)}>Change</button>
            </span>
            <label className="switch">
              <input
                type="checkbox"
                aria-label="Also keep the original file"
                checked={keepOriginal && destinationUsable}
                disabled={!destinationUsable}
                onChange={(e) => setKeepOriginal(e.target.checked)}
              />
              <span />
            </label>
          </div>
          <p className="hint">
            {destinationUsable
              ? <>Ingestion keeps only the text it extracts. This keeps the file itself too, in the
                 same place the Upload API tab&apos;s <b>Stage on the platform</b> puts it —
                 downloadable from its row in this list, deleted with the document, and held for{' '}
                 {storage.retention_days} days.</>
              /* An S3 backend with no bucket configured cannot store
                 anything — better to say so than to offer a control
                 whose upload would fail. */
              /* No bucket configured is a real state, and it has a
                 remedy that does not involve editing a file on the
                 server any more — Change opens the dialog. */
              : <>No storage destination is configured, so the file itself cannot be kept — only the
                 text extracted from it. Use <b>Change</b> to add a bucket and its credentials.</>}
          </p>
        </div>
      )}
      {destOpen && storage && (
        <UploadDestinationModal
          target={storage}
          onClose={() => setDestOpen(false)}
          onChosen={(id, refreshed) => { setStorage(refreshed); setDestinationId(id); }}
        />
      )}
      {origin === '' ? (
        <button className="btn sm" disabled={busy} onClick={runLocal}><Icon name="i-plus" /> {busy ? 'Ingesting…' : 'Ingest'}</button>
      ) : (
        <button className="btn sm" disabled={busy || picked.size === 0} onClick={runFromSource}>
          <Icon name="i-plus" /> {busy ? 'Ingesting…' : picked.size === 0 ? 'Ingest' : `Ingest ${picked.size} file(s)`}
        </button>
      )}
      {result && (
        <div className="small muted" style={{ marginTop: 8 }}>
          {/* The claim about which KIND of embedding this is has to come
              from the model actually used — it is no longer always the
              local hashing one. */}
          Inserted {result.chunks_inserted} chunk(s) using <span className="mono">{result.embed_model}</span> ({result.embed_dim}-dim
          {result.embed_model === 'local-hashing-v1'
            ? ' — local feature hashing, which matches on shared words rather than meaning'
            : ''}).
          {result.original_file && (
            <>
              {' '}Original kept in {result.original_file.storage_backend === 's3' ? 'S3' : 'local storage'} as{' '}
              <span className="mono">{result.original_file.object_key}</span> —{' '}
              <a href={result.original_file.url} target="_blank" rel="noreferrer">download</a>.
            </>
          )}
          {result.original_file_error && (
            <>
              {' '}The document ingested, but the original file could NOT be kept:{' '}
              {result.original_file_error}
            </>
          )}
          {result.transcription && (
            <>
              {' '}Transcribed by {result.transcription.provider}{' '}
              (<span className="mono">{result.transcription.model}</span>,{' '}
              {result.transcription.characters.toLocaleString()} characters) — retrieval searches
              that transcript, so it is only as accurate as the transcription was.
            </>
          )}
        </div>
      )}
      {sourceResult && (sourceResult.truncated_folders?.length ?? 0) > 0 && (
        <div className="small" style={{ marginTop: 8, color: 'var(--amber-text, #8a5a00)' }}>
          Stopped at {sourceResult.folder_file_cap} files for{' '}
          {sourceResult.truncated_folders!.join(', ')} — that folder holds more. Ingest its
          sub-folders separately to cover the rest.
        </div>
      )}
      {sourceResult && (
        <div className="small" style={{ marginTop: 8 }}>
          {/* Per-file outcomes, because one unreadable file in a folder
              should not hide the ones that worked. */}
          {sourceResult.results.map((r) => (
            <div key={r.file_id} className={r.success ? 'muted' : ''} style={r.success ? undefined : { color: 'var(--danger, #b4231d)' }}>
              {r.success
                ? `${r.filename} — ${r.chunks_inserted} chunk(s) via ${r.embed_model}`
                : `${r.filename || r.file_id} — ${r.error}`}
              {r.original_file && <> · <a href={r.original_file.url} target="_blank" rel="noreferrer">original kept</a></>}
              {r.original_file_error && <> · original NOT kept: {r.original_file_error}</>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ChunksView({ kb, doc, onBack, onDocDeleted }: { kb: KnowledgeBase; doc: KnowledgeDocument; onBack: () => void; onDocDeleted: () => void }) {
  const { toast } = useApp();
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState('');

  const load = () => api.listKbChunks(kb.id, doc.id).then(setChunks).catch((e) => toast(e.message));
  useEffect(() => { load(); }, [doc.id]);

  const toggleChunk = (c: KnowledgeChunk) => {
    api.updateKbChunk(kb.id, c.id, { enabled: !c.enabled })
      .then((updated) => setChunks((prev) => prev.map((x) => (x.id === c.id ? updated : x))))
      .catch((e) => toast(e.message));
  };
  const saveChunk = (c: KnowledgeChunk) => {
    api.updateKbChunk(kb.id, c.id, { content: draft })
      .then((updated) => { setChunks((prev) => prev.map((x) => (x.id === c.id ? updated : x))); setEditingId(null); toast('Chunk updated — embedding recomputed for real'); })
      .catch((e) => toast(e.message));
  };
  const deleteChunk = (c: KnowledgeChunk) => {
    api.deleteKbChunk(kb.id, c.id).then(() => setChunks((prev) => prev.filter((x) => x.id !== c.id))).catch((e) => toast(e.message));
  };
  const deleteDoc = async () => {
    if (!(await confirmAction({ title: 'Delete document',
      body: `Delete "${doc.filename}" and all ${doc.chunk_count} of its chunks?` }))) return;
    api.deleteKbDocument(kb.id, doc.id).then(() => { toast('Document deleted'); onDocDeleted(); }).catch((e) => toast(e.message));
  };

  return (
    <div>
      <div className="row-split inner-sticky" style={{ marginBottom: 14 }}>
        <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
          <button className="btn sm ghost" onClick={onBack}><Icon name="i-chev" /> Back to Documents</button>
          <b>{doc.filename}</b><span className="chip line">{chunks.length} chunk(s)</span>
        </div>
        <button className="icon-action danger" title="Delete document" aria-label="Delete document" onClick={deleteDoc}><Icon name="i-trash" /></button>
      </div>
      {chunks.map((c) => (
        <div key={c.id} className="card pad" style={{ marginBottom: 10, opacity: c.enabled ? 1 : 0.55 }}>
          <div className="row-split" style={{ marginBottom: 8 }}>
            <span className="small muted mono">#{c.chunk_index} · {c.word_count} words</span>
            <div className="row-split" style={{ gap: 8 }}>
              {Object.keys(c.metadata || {}).length > 0 && (
                <span className="small muted">{Object.entries(c.metadata).map(([k, v]) => `${k}: ${v}`).join(' · ')}</span>
              )}
              <span className="small">{c.enabled ? 'Enabled' : 'Disabled'}</span>
              <Toggle on={c.enabled} onChange={() => toggleChunk(c)} />
              <button className="icon-action danger" title="Delete chunk" aria-label="Delete chunk" onClick={() => deleteChunk(c)}><Icon name="i-trash" /></button>
            </div>
          </div>
          {editingId === c.id ? (
            <>
              <textarea rows={3} value={draft} onChange={(e) => setDraft(e.target.value)} />
              <div className="row-split" style={{ marginTop: 8, justifyContent: 'flex-start', gap: 8 }}>
                <button className="btn sm" onClick={() => saveChunk(c)}>Save</button>
                <button className="btn sm ghost" onClick={() => setEditingId(null)}>Cancel</button>
              </div>
            </>
          ) : (
            <div className="small" style={{ cursor: 'text' }} onClick={() => { setEditingId(c.id); setDraft(c.content); }}>{c.content}</div>
          )}
        </div>
      ))}
      {chunks.length === 0 && <div className="empty-cfg" style={{ padding: 30 }}>No chunks in this document.</div>}
    </div>
  );
}

function DocumentsTab({ kb }: { kb: KnowledgeBase }) {
  const { toast } = useApp();
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [addFileOpen, setAddFileOpen] = useState(false);
  const [openDoc, setOpenDoc] = useState<KnowledgeDocument | null>(null);
  // The table's column headers pin just under the pinned toolbar, so the
  // toolbar's real height is published to the scroll frame as a variable.
  const toolbarRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const bar = toolbarRef.current;
    const frame = bar?.closest('.inner-scroll') as HTMLElement | null;
    if (!bar || !frame) return;
    const sync = () => frame.style.setProperty('--inner-sticky-h', `${bar.offsetHeight}px`);
    sync();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(sync);
    ro.observe(bar);
    return () => ro.disconnect();
  }, [openDoc]);
  // Which document's metadata is being edited, and the draft values.
  const [metaDocId, setMetaDocId] = useState<string | null>(null);
  const [metaDraft, setMetaDraft] = useState<Record<string, string>>({});
  const [metaFields, setMetaFields] = useState<KnowledgeMetadataField[]>([]);
  const [savingMeta, setSavingMeta] = useState(false);

  const load = () => api.listKbDocuments(kb.id).then(setDocs).catch((e) => toast(e.message));
  useEffect(() => { load(); }, [kb.id]);
  // The Metadata tab's fields define the form. Free-text keys are still
  // possible via the ingest-time JSON box; this covers the normal case
  // of filling in fields that already exist.
  useEffect(() => { api.listKbMetadataFields(kb.id).then(setMetaFields).catch(() => {}); }, [kb.id]);

  const openMeta = (d: KnowledgeDocument) => {
    const current = d.metadata || {};
    const draft: Record<string, string> = {};
    metaFields.forEach((f) => { draft[f.name] = current[f.name] != null ? String(current[f.name]) : ''; });
    // Keep any keys the document already has that aren't registered fields.
    Object.entries(current).forEach(([k, v]) => { if (!(k in draft)) draft[k] = String(v); });
    setMetaDraft(draft);
    setMetaDocId(d.id);
  };

  const saveMeta = (d: KnowledgeDocument) => {
    // Blank inputs are dropped rather than stored as "" — an empty
    // string is a value that metadata filters would match on.
    const payload: Record<string, any> = {};
    Object.entries(metaDraft).forEach(([k, v]) => { if (v.trim()) payload[k] = v.trim(); });
    setSavingMeta(true);
    api.setKbDocumentMetadata(kb.id, d.id, payload)
      .then((updated) => {
        setDocs((prev) => prev.map((x) => (x.id === d.id ? updated : x)));
        toast(`Metadata saved for ${d.filename} (${d.chunk_count} chunks)`);
        setMetaDocId(null);
        api.listKbMetadataFields(kb.id).then(setMetaFields).catch(() => {});
      })
      .catch((e) => toast(e.message))
      .finally(() => setSavingMeta(false));
  };

  if (openDoc) {
    return <ChunksView kb={kb} doc={openDoc} onBack={() => setOpenDoc(null)} onDocDeleted={() => { setOpenDoc(null); load(); }} />;
  }

  const toggleStatus = (d: KnowledgeDocument) => {
    api.setKbDocumentStatus(kb.id, d.id, d.status === 'enabled' ? 'disabled' : 'enabled')
      .then((updated) => setDocs((prev) => prev.map((x) => (x.id === d.id ? updated : x))))
      .catch((e) => toast(e.message));
  };
  const remove = async (d: KnowledgeDocument) => {
    if (!(await confirmAction({ title: 'Delete document',
      body: `Delete "${d.filename}" and all ${d.chunk_count} of its chunks?` }))) return;
    api.deleteKbDocument(kb.id, d.id).then(() => { toast('Document deleted'); load(); }).catch((e) => toast(e.message));
  };

  return (
    <>
      <div className="inner-sticky" ref={toolbarRef}>
      <p className="small muted" style={{ marginTop: 0 }}>All files ingested into this knowledge base — each row is a real, independent upload, its chunks queryable and manageable as a unit.</p>
      <div className="row-split" style={{ marginBottom: 14 }}>
        <span className="small muted">{docs.length} document(s)</span>
        <button className="btn sm" onClick={() => setAddFileOpen(!addFileOpen)}><Icon name="i-plus" /> Add file</button>
      </div>
      </div>
      {addFileOpen && <AddFilePanel kb={kb} onClose={() => setAddFileOpen(false)} onIngested={load} />}
      {docs.length > 0 ? (
        <table className="kb-docs-table">
          <thead><tr><th>Name</th><th>Chunking</th><th>Words</th><th>Metadata</th><th>Retrieved</th><th>Uploaded</th><th>Status</th><th /></tr></thead>
          <tbody>
            {docs.map((d) => [
              <tr key={d.id}>
                <td>
                  {/* Opening a document lists its chunks out of Postgres,
                      which a hosted index cannot serve — the vectors are
                      there, but there is no row-by-row read for them. A
                      dead link would be worse than plain text. */}
                  {kb.provider === 'pgvector'
                    ? <span className="link" style={{ cursor: 'pointer' }} onClick={() => setOpenDoc(d)}>{d.filename}</span>
                    : <span title={`Chunk-level browsing is available for pgvector pipelines; this one stores its vectors in ${kb.provider}`}>{d.filename}</span>}
                  {/* Only when the file itself was kept. Every document
                      ingested without that option has no bytes to
                      offer, and a download link that 404s would be
                      worse than none. */}
                  {d.original_file && (
                    <a
                      href={d.original_file.url}
                      target="_blank"
                      rel="noreferrer"
                      /* A chip, not bare text: beside a teal filename that
                         opens the chunk list, an unstyled word read as a
                         stray label rather than something to click. */
                      className="chip line file-chip"
                      style={{ marginLeft: 8 }}
                      title={`Original file (${fileSizeLabel(d.original_file.size)}), kept in ${d.original_file.storage_backend === 's3' ? 'S3' : 'local storage'} as ${d.original_file.object_key}`}
                    >
                      <Icon name="i-file" /> original
                    </a>
                  )}
                </td>
                <td><span className="chip line">{d.chunking_mode}</span></td>
                <td className="mono small">{d.word_count.toLocaleString()}</td>
                <td>
                  <button className="link-btn" onClick={() => (metaDocId === d.id ? setMetaDocId(null) : openMeta(d))}>
                    {Object.keys(d.metadata || {}).length > 0
                      ? `${Object.keys(d.metadata || {}).length} field(s)`
                      : 'Add metadata'}
                  </button>
                </td>
                <td className="mono small">{d.retrieval_count}</td>
                <td className="small muted">{new Date(d.created_at).toLocaleString()}</td>
                <td>
                  <div className="row-split" style={{ gap: 6, justifyContent: 'flex-start' }}>
                    <span className="small">{d.status === 'enabled' ? 'Enabled' : 'Disabled'}</span>
                    <Toggle on={d.status === 'enabled'} onChange={() => toggleStatus(d)} />
                  </div>
                </td>
                <td><button className="icon-action danger" title="Delete" aria-label={`Delete ${d.filename}`} onClick={() => remove(d)}><Icon name="i-trash" /></button></td>
              </tr>,
              metaDocId === d.id ? (
                <tr key={`${d.id}-meta`}>
                  <td colSpan={8} style={{ background: 'var(--paper)' }}>
                    {metaFields.length === 0 && Object.keys(metaDraft).length === 0 ? (
                      <div className="small muted" style={{ padding: '6px 0' }}>
                        No metadata fields defined yet — add them on the Metadata tab first.
                      </div>
                    ) : (
                      <>
                        <div className="doc-meta-grid">
                          {Object.keys(metaDraft).map((k) => (
                            <div className="field" key={k} style={{ margin: 0 }}>
                              <label>{k}</label>
                              <input
                                value={metaDraft[k]}
                                placeholder="Leave blank to omit"
                                onChange={(e) => setMetaDraft((prev) => ({ ...prev, [k]: e.target.value }))}
                              />
                            </div>
                          ))}
                        </div>
                        <div className="row-split" style={{ marginTop: 10 }}>
                          <span className="small muted">
                            Applies to all {d.chunk_count} chunks of this document.
                          </span>
                          <div className="row-split" style={{ gap: 8, justifyContent: 'flex-end' }}>
                            <button className="btn sm ghost" onClick={() => setMetaDocId(null)} disabled={savingMeta}>Cancel</button>
                            <button className="btn sm" onClick={() => saveMeta(d)} disabled={savingMeta}>
                              {savingMeta ? 'Saving…' : 'Save metadata'}
                            </button>
                          </div>
                        </div>
                      </>
                    )}
                  </td>
                </tr>
              ) : null,
            ]).flat().filter(Boolean)}
          </tbody>
        </table>
      ) : (
        <div className="empty-cfg" style={{ padding: 30 }}>No documents yet — click "Add file" to ingest one.</div>
      )}
    </>
  );
}

function MetadataTab({ kb }: { kb: KnowledgeBase }) {
  const { toast } = useApp();
  const [fields, setFields] = useState<KnowledgeMetadataField[]>([]);
  const [newName, setNewName] = useState('');
  const [newType, setNewType] = useState('string');
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState('');

  const load = () => api.listKbMetadataFields(kb.id).then(setFields).catch((e) => toast(e.message));
  useEffect(() => { load(); }, [kb.id]);

  const add = () => {
    if (!newName.trim()) return;
    api.addKbMetadataField(kb.id, newName.trim(), newType)
      .then((f) => { setFields((prev) => [...prev, f]); setNewName(''); toast('Metadata field added'); })
      .catch((e) => toast(e.message));
  };
  const saveRename = (f: KnowledgeMetadataField) => {
    api.renameKbMetadataField(kb.id, f.id, renameDraft.trim(), f.type)
      .then((updated) => { setFields((prev) => prev.map((x) => (x.id === f.id ? updated : x))); setRenamingId(null); })
      .catch((e) => toast(e.message));
  };
  const remove = async (f: KnowledgeMetadataField) => {
    if (!(await confirmAction({ title: 'Delete metadata field',
      body: `Delete field "${f.name}"? This removes it and its values from all documents in this knowledge base.` }))) return;
    api.deleteKbMetadataField(kb.id, f.id).then(() => setFields((prev) => prev.filter((x) => x.id !== f.id))).catch((e) => toast(e.message));
  };

  return (
    <>
      <div className="inner-sticky">
      <p className="small muted" style={{ marginTop: 0 }}>Real, named, typed fields defined at the knowledge base level — the Knowledge Retrieval node's Metadata Filtering reads from these. New fields are also auto-created the first time you use them during ingestion.</p>
      <div className="card pad" style={{ marginBottom: 14 }}>
        <b className="small">Add Metadata</b>
        <div className="row-split" style={{ gap: 8, marginTop: 10 }}>
          <input placeholder="field_name (lowercase, numbers, underscores)" value={newName} onChange={(e) => setNewName(e.target.value)} style={{ flex: 1 }} />
          <SearchSelect ariaLabel="Metadata field type" noun="type" value={newType}
            style={{ width: 130, flex: 'none' }}
            options={[
              { value: 'string', label: 'String' }, { value: 'number', label: 'Number' }, { value: 'time', label: 'Time' },
            ]}
            onChange={(v) => setNewType(v)} />
          <button className="btn sm" onClick={add}><Icon name="i-plus" /> Add</button>
        </div>
      </div>
      </div>
      {fields.map((f) => (
        <div key={f.id} className="card pad" style={{ marginBottom: 8 }}>
          <div className="row-split">
            {renamingId === f.id ? (
              <input value={renameDraft} onChange={(e) => setRenameDraft(e.target.value)} style={{ flex: 1, marginRight: 8 }} />
            ) : (
              <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}><span className="mono">{f.name}</span><span className="chip line">{f.type}</span></div>
            )}
            <div className="row-split" style={{ gap: 6 }}>
              {renamingId === f.id ? (
                <>
                  <button className="btn sm" onClick={() => saveRename(f)}>Save</button>
                  <button className="btn sm ghost" onClick={() => setRenamingId(null)}>Cancel</button>
                </>
              ) : (
                <>
                  <button className="icon-action" title="Rename" aria-label={`Rename ${f.name}`} onClick={() => { setRenamingId(f.id); setRenameDraft(f.name); }}><Icon name="i-pencil" /></button>
                  <button className="icon-action danger" title="Delete" aria-label={`Delete ${f.name}`} onClick={() => remove(f)}><Icon name="i-trash" /></button>
                </>
              )}
            </div>
          </div>
        </div>
      ))}
      {fields.length === 0 && <div className="empty-cfg" style={{ padding: 30 }}>No metadata fields yet — add one above, or ingest a document with metadata to create fields automatically.</div>}
    </>
  );
}

function RetrievalTestingTab({ kb, onOpenSettings }: { kb: KnowledgeBase; onOpenSettings: () => void }) {
  const { toast } = useApp();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<KnowledgeRetrievalTestResult[] | null>(null);
  const [rerankNote, setRerankNote] = useState<string | null>(null);
  const [history, setHistory] = useState<KnowledgeRetrievalTestHistoryEntry[]>([]);
  const [busy, setBusy] = useState(false);
  // Which Records row is being viewed, and when that run happened. null
  // means the right pane is showing the run just executed here, not a
  // record pulled from history.
  const [openRecord, setOpenRecord] = useState<KnowledgeRetrievalTestHistoryEntry | null>(null);

  const loadHistory = () => api.getKbRetrievalTestHistory(kb.id).then(setHistory).catch(() => {});
  useEffect(() => { loadHistory(); }, [kb.id]);

  const run = () => {
    if (!query.trim()) return;
    setBusy(true);
    api.runKbRetrievalTest(kb.id, query.trim(), kb.config.top_k || 5)
      .then((r) => { setResults(r.results); setRerankNote(r.rerank_note || null); setOpenRecord(null); loadHistory(); })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  // Shows the snapshot this run stored. Nothing is re-queried: the
  // chunks are the ones that actually scored at the time, which is the
  // point of a history record.
  const openHistoryRecord = (h: KnowledgeRetrievalTestHistoryEntry) => {
    setOpenRecord(h);
    setResults(h.results || []);
    setRerankNote(null);
    setQuery(h.query.replace(/^\[API\] /, ''));
  };

  // Only offered for rows recorded before results were stored. This is a
  // NEW run against today's documents and settings, so it is labelled as
  // such rather than presented as the old record's results.
  const rerunHistoryRecord = (h: KnowledgeRetrievalTestHistoryEntry) => {
    const q = h.query.replace(/^\[API\] /, '');
    setQuery(q);
    setBusy(true);
    api.runKbRetrievalTest(kb.id, q, kb.config.top_k || 5)
      .then((r) => { setResults(r.results); setRerankNote(r.rerank_note || null); setOpenRecord(null); loadHistory(); })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  const method = kb.config.retrieval_method || 'vector';
  const methodLabel = { vector: 'Vector Search', fulltext: 'Full-Text Search', hybrid: 'Hybrid Search' }[method];

  return (
    <div className="retrieval-testing-grid">
      <div>
        <p className="small muted" style={{ marginTop: 0 }}>
          Test the hitting effect of this knowledge base based on the given query text — the same real scoring path
          (<span className="mono small">{methodLabel}</span>) a live Retrieval node uses, set on the Settings tab.
        </p>
        <div className="card pad retrieval-source-card" style={{ marginBottom: 14 }}>
          <div className="row-split" style={{ marginBottom: 8 }}>
            <b className="small" style={{ textTransform: 'uppercase', letterSpacing: '.03em' }}>Source Text</b>
            <span className="chip blue retrieval-method-chip" onClick={onOpenSettings} title="Set on the Settings tab">
              <Icon name="i-grid" /> {methodLabel.toUpperCase()} <Icon name="i-cog" />
            </span>
          </div>
          <textarea rows={4} maxLength={200} placeholder="Please enter a text, a short declarative sentence is recommended." value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="row-split" style={{ marginTop: 8 }}>
            <span className="small muted">{query.length}/200</span>
            <button className="btn sm" disabled={busy || !query.trim()} onClick={run}><Icon name="i-test" /> {busy ? 'Testing…' : 'Test'}</button>
          </div>
        </div>
        <b className="small">Records</b>
        <table className="kb-docs-table" style={{ marginTop: 8 }}>
          <thead><tr><th>Query Content</th><th>Results</th><th>Time</th></tr></thead>
          <tbody>
            {history.map((h) => (
              <tr
                key={h.id}
                className={`kb-record-row${openRecord?.id === h.id ? ' on' : ''}`}
                onClick={() => openHistoryRecord(h)}
                title="Show what this run returned"
              >
                <td>{h.query}</td>
                <td className="mono small">{h.result_count}</td>
                <td className="small muted">{new Date(h.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {history.length === 0 && <div className="empty-cfg" style={{ padding: 20 }}>No retrieval tests run yet.</div>}
      </div>
      <div>
        {rerankNote && <Banner kind="warn">{rerankNote}</Banner>}
        {openRecord && (
          <div className="row-split kb-record-head" style={{ marginBottom: 10 }}>
            <div style={{ minWidth: 0 }}>
              <b className="small">Record</b>
              <div className="small muted" style={{ overflowWrap: 'anywhere' }}>
                {openRecord.query} · ran {new Date(openRecord.created_at).toLocaleString()}
              </div>
            </div>
            <button className="btn sm ghost" onClick={() => { setOpenRecord(null); setResults(null); }}>
              <Icon name="i-x" /> Close
            </button>
          </div>
        )}
        {!results && <div className="empty-cfg" style={{ padding: 40 }}>Run a test, or click a row under Records to see what that run returned.</div>}
        {/* A stored record with no snapshot: recorded before results were
            kept. result_count proves it DID match, so this is missing
            history rather than an empty result — say so instead of
            showing "no chunks matched". */}
        {results && results.length === 0 && openRecord && openRecord.result_count > 0 && (
          <div className="empty-cfg" style={{ padding: 28 }}>
            This run returned {openRecord.result_count} chunk(s), but it predates results being stored, so
            the originals were never kept.
            <div style={{ marginTop: 12 }}>
              <button className="btn sm" disabled={busy} onClick={() => rerunHistoryRecord(openRecord)}>
                <Icon name="i-refresh" /> {busy ? 'Running…' : 'Run this query again'}
              </button>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>
              A re-run scores against today&apos;s documents and settings, so it may not match what this record shows.
            </div>
          </div>
        )}
        {results && results.length === 0 && !(openRecord && openRecord.result_count > 0) && <div className="empty-cfg" style={{ padding: 40 }}>No chunks matched — nothing enabled in this knowledge base, or nothing close enough to this query.</div>}
        {results && results.map((r) => (
          <div key={r.id} className="card pad" style={{ marginBottom: 10 }}>
            <div className="row-split" style={{ marginBottom: 6 }}><span className="small muted mono">{r.source_filename}</span><span className="chip green">score {r.score}</span></div>
            <div className="small">{r.content}</div>
            {Object.keys(r.metadata || {}).length > 0 && (
              <div className="small muted" style={{ marginTop: 6 }}>{Object.entries(r.metadata).map(([k, v]) => `${k}: ${v}`).join(' · ')}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function PipelineTab({ kb, onSaved }: { kb: KnowledgeBase; onSaved: (kb: KnowledgeBase) => void }) {
  const { toast } = useApp();
  const [icon, setIcon] = useState(kb.config.icon || '🤖');
  const [description, setDescription] = useState(kb.config.description || '');
  const [settings, setSettings] = useState<KBSettings>({
    chunk_structure: kb.config.chunk_structure || 'general',
    index_method: kb.config.index_method || 'high_quality',
    keyword_count: kb.config.keyword_count ?? 10,
    retrieval_method: kb.config.retrieval_method || 'vector',
    reranking_enabled: kb.config.reranking_enabled ?? false,
    semantic_weight: kb.config.semantic_weight ?? 0.7,
    keyword_weight: kb.config.keyword_weight ?? 0.3,
    top_k: kb.config.top_k ?? 3,
    score_threshold_enabled: kb.config.score_threshold_enabled ?? false,
    score_threshold: kb.config.score_threshold ?? 0,
  });
  const [hasDocuments, setHasDocuments] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [embedModels, setEmbedModels] = useState<EmbeddingModelOption[]>([]);

  useEffect(() => { api.listEmbeddingModels().then((r) => setEmbedModels(r.models)).catch(() => {}); }, []);

  useEffect(() => { api.listKbDocuments(kb.id).then((docs) => setHasDocuments(docs.length > 0)).catch(() => {}); }, [kb.id]);

  const patch = (p: Partial<KBSettings>) => { setSettings((s) => ({ ...s, ...p })); setDirty(true); };

  const save = () => {
    setSaving(true);
    api.updateKbSettings(kb.id, { ...settings, description: description.trim(), icon })
      .then((updated) => { onSaved(updated); setDirty(false); toast('Settings saved'); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <div>
      <div className="small muted" style={{ marginBottom: 14, textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Name &amp; Icon</div>
      <div className="row-split" style={{ gap: 10, alignItems: 'flex-end', marginBottom: 14 }}>
        <div className="field" style={{ margin: 0 }}>
          <label>Icon</label>
          <input value={icon} onChange={(e) => { setIcon(e.target.value); setDirty(true); }} style={{ width: 56, textAlign: 'center', fontSize: 18 }} />
        </div>
        <div style={{ flex: 1 }}>
          <b>{kb.name}</b>
          <div className="small muted mono">{kb.id}</div>
        </div>
      </div>
      <div className="field">
        <label>Description</label>
        <textarea rows={2} placeholder="Describe what is in this data set." value={description} onChange={(e) => { setDescription(e.target.value); setDirty(true); }} />
      </div>

      <div className="small muted" style={{ margin: '20px 0 10px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Chunk Structure</div>
      <ChunkStructurePicker value={settings.chunk_structure || 'general'} onChange={(v) => patch({ chunk_structure: v })} locked={hasDocuments} />

      <div className="small muted" style={{ margin: '20px 0 10px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Index Method</div>
      <IndexMethodPicker
        value={settings.index_method || 'high_quality'} keywordCount={settings.keyword_count ?? 10}
        onChange={(v) => patch({ index_method: v })} onKeywordCountChange={(n) => patch({ keyword_count: n })}
        embedModel={settings.embedding_model || (kb.config.embedding_model as string) || 'local-hashing-v1'}
        embedModels={embedModels}
        onEmbedModelChange={(name) => patch({ embedding_model: name })}
        embedModelLocked={hasDocuments}
      />

      <div className="small muted" style={{ margin: '20px 0 10px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Retrieval Setting</div>
      <RetrievalSettingPicker settings={settings} onChange={patch} />

      <div className="row-split" style={{ marginTop: 16 }}>
        <Banner kind="info">Chunk Structure and Index Method only affect documents ingested from now on — existing chunks aren't reprocessed. Retrieval Setting applies immediately to every query, including this KB's live Retrieval nodes.</Banner>
      </div>
      <button className="btn" style={{ marginTop: 10 }} disabled={saving || !dirty} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
    </div>
  );
}

function CodeBlock({ code, label = 'request' }: { code: string; label?: string }) {
  const { toast } = useApp();
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(code).then(
      () => {
        setCopied(true);
        toast(`Example ${label} copied`);
        // Reverts the tick to the copy glyph; purely visual, so a stale
        // timer after unmount is harmless.
        setTimeout(() => setCopied(false), 1600);
      },
      () => toast('Could not copy — your browser blocked clipboard access'),
    );
  };
  return (
    <div className="codeblock-wrap">
      <pre className="codeblock">{code}</pre>
      <button
        className="codeblock-copy"
        title={`Copy this ${label}`}
        aria-label={`Copy this ${label}`}
        onClick={copy}
      >
        <Icon name={copied ? 'i-check' : 'i-copy'} />
      </button>
    </div>
  );
}

function RetrievalApiPanel({ kb, onSaved }: { kb: KnowledgeBase; onSaved: (kb: KnowledgeBase) => void }) {
  const { toast } = useApp();
  const [regenerating, setRegenerating] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [testQuery, setTestQuery] = useState('');
  const [testResults, setTestResults] = useState<KBRetrieveResult[] | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const enabled = !!kb.config.retrieval_api_enabled;
  const hasKey = !!kb.config.retrieval_api_key;

  const toggle = (v: boolean) => {
    api.updateKbSettings(kb.id, { retrieval_api_enabled: v })
      .then((updated) => { onSaved(updated); toast(v ? 'Retrieval API enabled' : 'Retrieval API disabled — the key stops working immediately'); })
      .catch((e) => toast(e.message));
  };

  const regenerate = async () => {
    if (hasKey && !(await confirmAction({ title: 'Regenerate retrieval key', confirmLabel: 'Regenerate',
      body: 'Regenerating invalidates the current key immediately — any caller using it will start getting 401s.' }))) return;
    setRegenerating(true);
    api.regenerateKbApiKey(kb.id)
      .then((updated) => {
        setRevealedKey(updated.config.retrieval_api_key || null);
        onSaved(updated);
        toast('New API key generated — copy it now, it won\'t be shown again');
      })
      .catch((e) => toast(e.message))
      .finally(() => setRegenerating(false));
  };

  const copyKey = () => {
    if (!revealedKey) return;
    navigator.clipboard.writeText(revealedKey).then(() => toast('Copied'), () => toast('Could not copy — your browser blocked clipboard access'));
  };

  const runTest = () => {
    if (!revealedKey || !testQuery.trim()) return;
    setTestBusy(true);
    api.callKbRetrievalApi(kb.id, revealedKey, testQuery.trim(), 5)
      .then((r) => setTestResults(r.results))
      .catch((e) => toast(e.message))
      .finally(() => setTestBusy(false));
  };

  const curlSnippet = `curl -X POST ${BASE}/api/integrations/knowledge-bases/${kb.id}/api/retrieve \\
  -H "Authorization: Bearer ${revealedKey || '<your-retrieval-api-key>'}" \\
  -H "Content-Type: application/json" \\
  -d '{"query": "how do I reset my password", "top_k": 5}'`;

  return (
    <div>
      <p className="small muted" style={{ marginTop: 0 }}>
        A real, externally-callable Retrieval API for this pipeline — genuinely the same scoring path
        (Chunk Structure / Index Method / Retrieval Setting from the Settings tab) a live Retrieval node uses,
        reachable over plain HTTP with its own key instead of requiring a builder session.
      </p>

      <div className="card pad" style={{ marginBottom: 16 }}>
        <div className="row-split">
          <div><b>Retrieval API</b><div className="small muted">Real bearer-token auth — disabling stops working immediately, not just hiding the toggle.</div></div>
          <Toggle on={enabled} onChange={toggle} />
        </div>
      </div>

      {enabled && (
        <div className="card pad" style={{ marginBottom: 16 }}>
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">API Key</b></div>
          {revealedKey ? (
            <div className="row-split" style={{ gap: 8 }}>
              <input className="mono small" readOnly value={revealedKey} style={{ flex: 1 }} onFocus={(e) => e.target.select()} />
              <button className="btn sm ghost" onClick={copyKey}><Icon name="i-copy" /> Copy</button>
            </div>
          ) : (
            <p className="small muted" style={{ margin: 0 }}>
              {hasKey ? 'A key exists but is only ever shown once, right after generation — regenerate to see (and use) a new one.' : 'No key generated yet.'}
            </p>
          )}
          <button className="btn sm ghost" style={{ marginTop: 10 }} disabled={regenerating} onClick={regenerate}>
            <Icon name="i-refresh" /> {regenerating ? 'Generating…' : hasKey ? 'Regenerate key' : 'Generate key'}
          </button>
        </div>
      )}

      {enabled && (
        <div className="card pad" style={{ marginBottom: 16 }}>
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Example request</b></div>
          <CodeBlock code={curlSnippet} />
          {!revealedKey && <p className="small muted" style={{ marginTop: 8, marginBottom: 0 }}>Generate a key above to see a real, working example with it filled in.</p>}
        </div>
      )}

      {enabled && revealedKey && (
        <div className="card pad">
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Try it</b></div>
          <div className="row-split" style={{ gap: 8 }}>
            <input placeholder="Test query…" value={testQuery} onChange={(e) => setTestQuery(e.target.value)} style={{ flex: 1 }} />
            <button className="btn sm" disabled={testBusy || !testQuery.trim()} onClick={runTest}><Icon name="i-play" /> {testBusy ? 'Calling…' : 'Call API'}</button>
          </div>
          {testResults && (
            <div style={{ marginTop: 12 }}>
              {testResults.length === 0 && <div className="small muted">No results.</div>}
              {testResults.map((r) => (
                <div key={r.id} className="small" style={{ marginBottom: 8, paddingBottom: 8, borderBottom: '1px solid var(--line-2)' }}>
                  <span className="chip green" style={{ marginRight: 6 }}>score {r.score}</span>{r.content}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {!enabled && <Banner kind="info">Enable the Retrieval API above to generate a key and call this pipeline from outside this platform.</Banner>}
    </div>
  );
}

// The extensions app/ingestion.py can genuinely turn into text. Kept in
// step with data_sources.INGESTIBLE on the server side.
// A denylist of formats with no text layer, mirroring
// data_sources.NO_TEXT_LAYER on the server. Not an allowlist: the
// ingestion pipeline decodes any unrecognised extension as text and
// rejects real binaries itself, so an allowlist here was narrower than
// the pipeline — a folder of .sql files filtered down to nothing.
const NO_TEXT_LAYER_EXT = new RegExp(
  '\\.(png|jpe?g|gif|bmp|tiff?|webp|ico|heic|heif|raw'
  + '|mp3|m4a|aac|flac|ogg|oga|opus|wma|wav|aiff'
  + '|mp4|m4v|mov|avi|mkv|webm|wmv|flv|mpe?g'
  + '|zip|tar|gz|tgz|bz2|xz|7z|rar|iso|dmg|pkg|jar|war'
  + '|exe|dll|so|dylib|bin|o|a|class|pyc|pyo|wasm'
  + '|ttf|otf|woff2?|eot'
  + '|psd|ai|sketch|fig|xd|blend|db|sqlite3?|mdb|accdb'
  + '|parquet|avro|orc|pdb|pkl|npy|npz|h5|hdf5|safetensors|onnx)$',
  'i',
);
// Excluded by NAME, mirroring data_sources._EXCLUDED_NAMES. OS metadata
// is noise in every folder pick, and a .env or credentials file is plain
// text the denylist above would happily offer — ingesting one would copy
// live credentials into a vector store, to come back later in retrieval
// results.
const EXCLUDED_NAMES = new Set([
  '.ds_store', 'thumbs.db', 'desktop.ini', '.localized',
  '.env', '.env.local', '.env.production', '.env.development', '.env.test',
  'credentials', 'credentials.json', 'id_rsa', 'id_ed25519', '.netrc', '.pgpass',
  '.htpasswd', 'secrets.yaml', 'secrets.yml',
]);
// Audio the server can transcribe, mirroring transcription.AUDIO_EXTENSIONS.
// These ARE in the denylist above, correctly — audio genuinely has no
// text layer — but ingestion.parse_file turns them into a transcript
// when a speech provider is configured, so they are offered when (and
// only when) it actually is. The caller passes that in rather than
// assuming it: a .wav listed on a deployment with no key behind it
// would be accepted and then fail at the end of the upload.
const AUDIO_EXT = /\.(wav|mp3|m4a|flac|ogg|oga|mpga)$/i;
/** The server reports its audio formats alphabetically, which puts
 *  `.flac` and `.mpga` in front of the two anyone recognises. Same set,
 *  read in the order people would name them; anything the server adds
 *  later still appears, after these. */
const AUDIO_ORDER = ['wav', 'mp3', 'm4a', 'flac', 'ogg', 'oga', 'mpga'];
function audioExtensionList(exts: string[]): string {
  const rank = (e: string) => {
    const i = AUDIO_ORDER.indexOf(e);
    return i === -1 ? AUDIO_ORDER.length : i;
  };
  return [...exts].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((e) => `.${e}`).join(', ');
}
export const AUDIO_ACCEPT = '.wav,.mp3,.m4a,.flac,.ogg,.oga,.mpga';
const isIngestibleName = (name: string, audioOk = false) => {
  const lowered = (name || '').toLowerCase();
  if (EXCLUDED_NAMES.has(lowered) || lowered.startsWith('.env.')) return false;
  if (audioOk && AUDIO_EXT.test(name)) return true;
  return !NO_TEXT_LAYER_EXT.test(name);
};

/** Whether audio can genuinely be ingested right now, asked of the
 *  server because only it knows which speech keys are configured.
 *  Null while the answer is still in flight — treated as "no" by
 *  callers, so nothing is offered on an optimistic guess. */
function useTranscription() {
  const [state, setState] = useState<
    { available: boolean; providers: string[]; extensions: string[]; reason: string | null } | null
  >(null);
  useEffect(() => {
    let alive = true;
    api.getTranscriptionAvailability()
      .then((r) => { if (alive) setState(r); })
      // An older API build has no such endpoint; audio simply stays
      // unoffered rather than the page showing an error for it.
      .catch(() => { if (alive) setState(null); });
    return () => { alive = false; };
  }, []);
  return state;
}

/** The directory user home folders live under on this OS, read from the
 *  browser's own platform string. This is the one piece of an absolute
 *  path a web page CAN know, so it is pre-filled: pick your home folder
 *  and the command's path is then exact with nothing typed. */
function osUserRoot(): string {
  const p = `${navigator.platform || ''} ${navigator.userAgent || ''}`;
  // Apple first, and matched on whole tokens. A loose /Win/ test is a
  // trap: "darwin" — which is exactly what a Mac reports in
  // navigator.platform — contains "win", so a Mac would be handed
  // C:\Users.
  if (/\b(mac|darwin|iphone|ipad|ipod)/i.test(p)) return '/Users';
  if (/\b(win32|win64|windows|wow64)\b/i.test(p)) return 'C:\\Users';
  return '/home';  // Linux and everything else that follows the FHS
}

// Where the absolute root of a local pick is remembered. Not per-KB:
// it describes the caller's own machine, which does not change between
// pipelines.
// Two separate remembered roots, deliberately NOT one shared value.
// A single prefix leaked across modes: folder mode seeds it with
// osUserRoot() and remembers it, then file mode joined that root to a
// bare filename and printed `/Users/_make_audio.py` — a path that
// looks absolute and points nowhere. A folder pick supplies real path
// structure to hang a root on; a single-file pick supplies none, so
// its root can only ever come from the person typing it.
const FOLDER_ROOT_KEY = 'inaiera.uploadApi.folderRoot';
const FILE_DIR_KEY = 'inaiera.uploadApi.fileDir';

function UploadApiPanel({ kb, onSaved }: { kb: KnowledgeBase; onSaved: (kb: KnowledgeBase) => void }) {
  const { toast } = useApp();
  const [regenerating, setRegenerating] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{ document_id: string; chunks_inserted: number; table: string } | null>(null);
  // 'file' picks one file (name only); 'folder' picks a directory, which
  // is the ONE browser API that hands back real path structure — see the
  // comment on localPath below.
  // Back to 'file', the control people expect for "upload a file". It
  // briefly defaulted to 'folder' because only a directory pick exposes
  // path structure to the BROWSER — but the path no longer comes from
  // the browser at all: the server looks the file up on its own disk
  // (see resolveRoot), so a single pick resolves too. The folder picker
  // remains for browsing, and for when a filename is ambiguous.
  const [localMode, setLocalMode] = useState<'file' | 'folder'>('file');
  const [folderFiles, setFolderFiles] = useState<File[]>([]);
  // Audio is only listed from a folder pick when the server can
  // actually transcribe it — see useTranscription.
  const speech = useTranscription();
  // Where a staged file is put. Same dialog and same destinations as
  // the Documents tab, because this button writes to the same store.
  const [storage, setStorage] = useState<StorageTarget | null>(null);
  const [destinationId, setDestinationId] = useState<string | null>(null);
  const [destOpen, setDestOpen] = useState(false);
  useEffect(() => {
    let alive = true;
    api.getStorageTarget()
      .then((r) => { if (alive) { setStorage(r); setDestinationId(r.destination_id || null); } })
      .catch(() => { if (alive) setStorage(null); });
    return () => { alive = false; };
  }, []);
  const stagingLabel = (storage?.destinations || []).find((d) => d.id === destinationId)?.label
    || storage?.label || '';
  // The absolute location of whatever was picked. Typed once and
  // remembered, rather than re-entered for every file.
  const remembered = (key: string, fallback = '') => {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  };
  // Where the PICKED FOLDER sits. Seeded with the OS home root, because
  // that is the one piece of an absolute path a browser genuinely knows.
  const [folderRoot, setFolderRoot] = useState(() => remembered(FOLDER_ROOT_KEY) || osUserRoot());
  // The folder holding a single picked FILE. Starts empty and stays
  // empty until typed — there is nothing to infer it from.
  const [fileDir, setFileDir] = useState(() => remembered(FILE_DIR_KEY));
  // Whether the folder root was typed by a person or is osUserRoot()'s
  // assumption. A guessed root still yields an absolute-LOOKING path,
  // and calling that correct would be worse than admitting it is
  // assumed, so the two are never presented the same way.
  const [rootConfirmed, setRootConfirmed] = useState(() => !!remembered(FOLDER_ROOT_KEY));
  // 'verified' means the server found the folder on its own filesystem,
  // which is stronger than a person typing a root and far stronger than
  // osUserRoot()'s guess.
  const [rootVerified, setRootVerified] = useState(false);
  const [resolving, setResolving] = useState(false);
  // Several real files matched a bare filename — the person picks which,
  // because guessing would put a plausible but wrong path in a command
  // they are about to run.
  const [pathChoices, setPathChoices] = useState<{ root: string; absolute_path: string; size: number }[]>([]);
  // The API is containerised, so it cannot see this machine's files and
  // no amount of picking will produce an absolute path through it.
  const [resolverBlind, setResolverBlind] = useState(false);
  // A file put into the platform's own upload store, so the command can
  // name it by id and carry no path at all.
  const [staged, setStaged] = useState<{ id: string; filename: string; expires_at: string | null } | null>(null);
  const [staging, setStaging] = useState(false);
  // localStorage throws outright in some contexts (private windows,
  // blocked site data), and a remembered convenience must never be able
  // to break the panel.
  const remember = (key: string, v: string) => {
    try { localStorage.setItem(key, v); } catch { /* not worth surfacing */ }
  };
  const setFolderRemembered = (v: string) => {
    setFolderRoot(v);
    setRootConfirmed(true);
    setRootVerified(false);  // typed, not checked against the filesystem
    remember(FOLDER_ROOT_KEY, v);
  };
  const setFileDirRemembered = (v: string) => {
    setFileDir(v);
    remember(FILE_DIR_KEY, v);
  };
  const enabled = !!kb.config.upload_api_enabled;
  const hasKey = !!kb.config.upload_api_key;

  // The same connected Data Sources the Documents tab offers. Naming a
  // file where it already lives is what makes a complete path
  // expressible in the snippet — see the comment on uploadTarget.
  const [sources, setSources] = useState<DataSource[]>([]);
  const [origin, setOrigin] = useState('');
  const [picked, setPicked] = useState<Map<string, SourcePick>>(new Map());
  const [sourceResult, setSourceResult] = useState<SourceIngestResult | null>(null);

  useEffect(() => {
    api.listDataSources()
      .then((rows) => setSources(rows.filter((r) => r.browsable)))
      .catch(() => setSources([]));
  }, []);

  /** Ask the API where a picked file really is. The browser will not say
   *  — File exposes only `name`, and a directory pick only a relative
   *  path — but the API runs on the same machine in local dev, so it can
   *  look. Not found is normal for a container or remote deployment, and
   *  the wording then stays honestly "assumed". */
  const resolveRoot = (relative: string, size?: number) => {
    setResolving(true);
    setRootVerified(false);
    setPathChoices([]);
    setResolverBlind(false);
    api.resolveLocalPath(relative, size)
      .then((r) => {
        if (r.found && r.root) {
          setFolderRoot(r.root);
          setFileDir(r.root);
          setRootConfirmed(true);
          setRootVerified(true);
          remember(FOLDER_ROOT_KEY, r.root);
          remember(FILE_DIR_KEY, r.root);
        } else if (r.ambiguous && r.matches?.length) {
          setPathChoices(r.matches);
        } else if (r.containerised) {
          setResolverBlind(true);
        }
      })
      .catch(() => { /* keep the assumed root and say so */ })
      .finally(() => setResolving(false));
  };

  const chooseResolvedPath = (root: string) => {
    setFileDir(root);
    setFolderRoot(root);
    setRootConfirmed(true);
    setRootVerified(true);
    setPathChoices([]);
    remember(FOLDER_ROOT_KEY, root);
    remember(FILE_DIR_KEY, root);
  };


  const togglePick = (entry: DataSourceEntry, path: string) => setPicked((prev) => {
    const next = new Map(prev);
    if (next.has(entry.id)) next.delete(entry.id);
    else next.set(entry.id, { name: entry.name, path, isDir: entry.is_dir });
    setSourceResult(null);
    return next;
  });
  const toggleMany = (picks: [string, SourcePick][], select: boolean) => setPicked((prev) => {
    const next = new Map(prev);
    picks.forEach(([id, pick]) => { if (select) next.set(id, pick); else next.delete(id); });
    return next;
  });

  const toggle = (v: boolean) => {
    api.updateKbSettings(kb.id, { upload_api_enabled: v })
      .then((updated) => { onSaved(updated); toast(v ? 'Upload API enabled' : 'Upload API disabled — the key stops working immediately'); })
      .catch((e) => toast(e.message));
  };

  const regenerate = async () => {
    if (hasKey && !(await confirmAction({ title: 'Regenerate upload key', confirmLabel: 'Regenerate',
      body: 'Regenerating invalidates the current upload key immediately — any caller using it will start getting 401s.' }))) return;
    setRegenerating(true);
    api.regenerateKbUploadApiKey(kb.id)
      .then((updated) => {
        setRevealedKey((updated.config.upload_api_key as string) || null);
        onSaved(updated);
        toast('Upload API key generated — copy it now, it is only shown once');
      })
      .catch((e) => toast(e.message))
      .finally(() => setRegenerating(false));
  };

  const copyKey = () => {
    if (!revealedKey) return;
    navigator.clipboard.writeText(revealedKey).then(() => toast('Copied'), () => toast('Could not copy — your browser blocked clipboard access'));
  };

  const runUpload = () => {
    if (!revealedKey || !file) return;
    setUploading(true);
    setUploadResult(null);
    api.callKbUploadApi(kb.id, revealedKey, file)
      .then((r) => { setUploadResult(r); toast(`Ingested ${r.chunks_inserted} chunk(s)`); })
      .catch((e) => toast(e.message))
      .finally(() => setUploading(false));
  };

  const stage = () => {
    if (!file) return;
    setStaging(true);
    api.stageUpload(file, destinationId || undefined)
      .then((r) => {
        setStaged({ id: r.id, filename: r.filename, expires_at: r.expires_at });
        toast(`${r.filename} staged in ${stagingLabel} — the command below now needs no local path`);
      })
      .catch((e) => toast(e.message))
      .finally(() => setStaging(false));
  };

  const runStagedUpload = () => {
    if (!revealedKey || !staged) return;
    setUploading(true);
    setUploadResult(null);
    api.callKbUploadFromStoreApi(kb.id, revealedKey, { upload_id: staged.id })
      .then((r) => { setUploadResult(r); toast(`Ingested ${r.chunks_inserted} chunk(s)`); })
      .catch((e) => toast(e.message))
      .finally(() => setUploading(false));
  };

  const runSourceUpload = () => {
    if (!revealedKey || picked.size === 0) return;
    setUploading(true);
    setSourceResult(null);
    api.callKbUploadFromSourceApi(kb.id, revealedKey, {
      source: origin, ...splitPicks(picked),
    })
      .then((r) => {
        setSourceResult(r);
        toast(r.failed === 0
          ? `Ingested ${r.ingested} file(s) from ${origin}`
          : `Ingested ${r.ingested} of ${r.requested} file(s) — ${r.failed} failed`);
      })
      .catch((e) => toast(e.message))
      .finally(() => setUploading(false));
  };

  // ---- the snippet ----
  //
  // Local file: only the NAME can ever be filled in, because no browser
  // exposes a selected file's real location. `input.value` yields the
  // literal "C:\fakepath\<name>" in Chrome and File carries only
  // `name`, by deliberate design — a page that could read your
  // filesystem layout would be a real privacy leak. So the folder is
  // typed by hand; with it the copied command runs from anywhere,
  // without it curl resolves the bare name against the current
  // directory (which is how you get `curl: (26) Failed to open/read
  // local data`).
  //
  // Data source file: the whole path IS knowable, because the file does
  // not live on the caller's disk — the server fetches it. So the
  // snippet targets /api/upload-from-source and carries the file's real
  // location, with nothing left for the reader to fill in.
  //
  // What the browser will and will not tell us:
  //   * A single-file pick exposes only `name`. `input.value` is the
  //     literal "C:\fakepath\<name>" and File carries no path, by
  //     deliberate design — a page that could read your filesystem
  //     layout would be a real privacy leak.
  //   * A DIRECTORY pick (webkitdirectory) exposes
  //     `webkitRelativePath` for every file inside — a real, browser-
  //     supplied path like "Downloads/reports/q3/summary.pdf", nested
  //     folders included. That is the only path information a browser
  //     ever hands over, and it is what makes the command below
  //     complete for a local file.
  // So the absolute root is asked for once (and remembered), and the
  // rest of the path comes from the picker itself.
  // Each mode reads its OWN root, so a folder root can never be joined
  // to a bare filename (which is how `/Users/_make_audio.py` happened).
  const dir = (localMode === 'folder' ? folderRoot : fileDir).trim().replace(/\/+$/, '');
  const relPath = file ? ((file as any).webkitRelativePath || file.name) : '';
  const uploadTarget = file ? (dir ? `${dir}/${relPath}` : relPath) : '/path/to/document.pdf';
  // Complete only when the whole path is real: a folder pick supplies
  // the nested part and a person vouched for the root. In file mode the
  // typed folder IS the person's own statement, so it counts as
  // confirmed on its own.
  const localPathComplete = !!file && !!dir
    && (localMode === 'folder' ? rootConfirmed : true);
  const keyOrPlaceholder = revealedKey || '<your-upload-api-key>';
  const picks = Array.from(picked.entries());
  // For GitHub and Google Cloud Storage the id IS the path, so the body
  // reads as a location; for the id-addressed providers the readable
  // path is shown alongside as a comment rather than pretending the
  // opaque id is a path.
  const idsArePaths = picks.every(([id, p]) => id === p.path || id.endsWith(`/${p.path}`) || id.endsWith(p.path));
  const sourceBody = JSON.stringify({ source: origin, ...splitPicks(picked) });
  const pathComment = picks.length === 0 || idsArePaths
    ? ''
    : `\n# path(s): ${picks.map(([, p]) => `${origin}/${p.path}`).join(', ')}`;
  const curlSnippet = origin === ''
    ? (staged
      // No path in the command at all: the file is already on the
      // platform, so this runs unchanged from any directory on any
      // machine — which a `-F file=@…` command can never do.
      ? `curl -X POST ${BASE}/api/integrations/knowledge-bases/${kb.id}/api/upload-from-store \\
  -H "Authorization: Bearer ${keyOrPlaceholder}" \\
  -H "Content-Type: application/json" \\
  -d '${JSON.stringify({ upload_id: staged.id, max_words: 180, overlap_words: 30 })}'
# ${staged.filename}, held by the platform${staged.expires_at ? ` until ${new Date(staged.expires_at).toLocaleDateString()}` : ''}`
      : `curl -X POST ${BASE}/api/integrations/knowledge-bases/${kb.id}/api/upload \\
  -H "Authorization: Bearer ${keyOrPlaceholder}" \\
  -F "file=@${uploadTarget}" \\
  -F "max_words=180" \\
  -F "overlap_words=30"`)
    : `curl -X POST ${BASE}/api/integrations/knowledge-bases/${kb.id}/api/upload-from-source \\
  -H "Authorization: Bearer ${keyOrPlaceholder}" \\
  -H "Content-Type: application/json" \\
  -d '${picks.length ? sourceBody : JSON.stringify({ source: origin, file_ids: ['<file-id-or-path>'], folder_paths: [] })}'${pathComment}`;

  return (
    <div>
      <p className="small muted" style={{ marginTop: 0 }}>
        A real, externally-callable Upload API for this pipeline — either a POST of the file itself
        (multipart/form-data) or, for a file that already lives in a connected Data Source, a POST naming
        where it lives. Both are parsed, chunked, embedded and inserted through exactly the same ingestion
        path a file dropped on the Documents tab takes, so an external upload is indistinguishable from a
        UI one and appears in the Documents list like any other.
      </p>

      <div className="card pad" style={{ marginBottom: 16 }}>
        <div className="row-split">
          <div><b>Upload API</b><div className="small muted">Real bearer-token auth, on its own key — this surface writes, so it is deliberately not the retrieval key.</div></div>
          <Toggle on={enabled} onChange={toggle} />
        </div>
      </div>

      {enabled && (
        <div className="card pad" style={{ marginBottom: 16 }}>
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Upload API Key</b></div>
          {revealedKey ? (
            <div className="row-split" style={{ gap: 8 }}>
              <input className="mono small" readOnly value={revealedKey} style={{ flex: 1 }} onFocus={(e) => e.target.select()} />
              <button className="btn sm ghost" onClick={copyKey}><Icon name="i-copy" /> Copy</button>
            </div>
          ) : (
            <p className="small muted" style={{ margin: 0 }}>
              {hasKey ? 'A key exists but is only ever shown once, right after generation — regenerate to see (and use) a new one.' : 'No upload key generated yet.'}
            </p>
          )}
          <button className="btn sm ghost" style={{ marginTop: 10 }} disabled={regenerating} onClick={regenerate}>
            <Icon name="i-refresh" /> {regenerating ? 'Generating…' : hasKey ? 'Regenerate key' : 'Generate key'}
          </button>
        </div>
      )}

      {enabled && (
        <div className="card pad" style={{ marginBottom: 16 }}>
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Example request</b></div>
          <CodeBlock code={curlSnippet} label="upload request" />
          {/* Several real files share this name, so the person chooses.
              Picking one for them would put a plausible-but-wrong path in
              a command they are about to run. */}
          {revealedKey && origin === '' && file && !staged && pathChoices.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="small muted" style={{ marginBottom: 4 }}>
                <b>{pathChoices.length} files named <span className="mono">{file.name}</span></b> on this
                machine{file.size ? ' with the same size' : ''} — which one did you pick?
              </div>
              <div className="ds-browser">
                <ul className="ds-list">
                  {pathChoices.map((m) => (
                    <li key={m.absolute_path} className="ds-row">
                      <span
                        className="link ds-name mono small"
                        style={{ cursor: 'pointer' }}
                        onClick={() => chooseResolvedPath(m.root)}
                      >
                        <span className="ds-label">{m.absolute_path}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
          {revealedKey && origin === '' && file && !staged && (
            <div style={{ marginTop: 10 }}>
              <label className="small muted" htmlFor="kb-upload-prefix">
                {localMode === 'folder'
                  ? <>Where <span className="mono">{relPath.split('/')[0]}</span> lives on your machine</>
                  : <>Folder holding <span className="mono">{file.name}</span></>}
                {' '}— entered once and remembered
              </label>
              <input
                id="kb-upload-prefix"
                className="mono small"
                style={{ width: '100%', marginTop: 4 }}
                placeholder={localMode === 'folder'
                  ? `e.g. ${osUserRoot()}`
                  : `e.g. ${osUserRoot()}/you/Desktop/reports`}
                value={localMode === 'folder' ? folderRoot : fileDir}
                onChange={(e) => (localMode === 'folder'
                  ? setFolderRemembered(e.target.value)
                  : setFileDirRemembered(e.target.value))}
              />
              {/* The one arrangement that needs no typing at all, spelled
                  out: a home-folder pick makes the first path segment the
                  account name, and the OS root above completes it exactly. */}
              {localMode === 'folder' && !rootConfirmed && (
                <div className="small muted" style={{ marginTop: 4 }}>
                  Tip: pick your <b>home folder</b> and this root is already correct, giving the exact
                  path with nothing typed. Pick something deeper (Desktop, say) and set the root to{' '}
                  <span className="mono">{osUserRoot()}/&lt;your-account&gt;</span> once.
                </div>
              )}
            </div>
          )}
          <p className="small muted" style={{ marginTop: 8, marginBottom: 0 }}>
            {!revealedKey
              ? 'Generate a key above to see a real, working example with it filled in.'
              : origin !== ''
                ? picked.size > 0
                  ? <>
                      Filled in with {picked.size === 1 ? 'the file' : `${picked.size} files`} you picked from{' '}
                      <span className="mono">{origin}</span>, at{' '}
                      <span className="mono">{picks.map(([, p]) => p.path).join(', ')}</span>. The server fetches
                      {picked.size === 1 ? ' it' : ' them'} from {origin} itself, so the command is complete as
                      shown — nothing to fill in and nothing to download first.
                    </>
                  : <>Pick {origin} file(s) under &quot;Try it&quot; below and the command fills in with their full location.</>
                : staged
                  ? <>
                      <span className="mono">{staged.filename}</span> is held by the platform, so the command carries no
                      file path at all and runs unchanged from any directory, on any machine — which is not something a{' '}
                      <span className="mono">-F &quot;file=@…&quot;</span> command can do, since that always resolves
                      against the caller&apos;s own disk.
                    </>
                  : file
                  ? <>
                      Filled in with <span className="mono">{uploadTarget}</span>.
                      {resolving
                        ? <> Asking the server where <span className="mono">{relPath.split('/')[0]}</span> lives&hellip;</>
                        : rootVerified
                        ? <> That is the whole absolute path, <b>verified on disk</b> — the server found{' '}
                            <span className="mono">{relPath.split('/')[0]}</span> at{' '}
                            <span className="mono">{dir}</span>. Copy the command and it runs from any directory.</>
                        : localPathComplete
                        ? <> That is the whole absolute path — copy the command and it runs from any directory.</>
                        : localMode === 'folder'
                          ? <> <span className="mono">{relPath}</span> came from the folder you picked, sub-folders
                              included. <span className="mono">{dir}</span> is this machine&apos;s usual home root —
                              <b> assumed, not known</b>. A browser will not say where a picked folder sits, and the
                              server could not find it either, which means it is running on a different machine or in a
                              container. Correct the root above and the path is exact; or <b>Stage on the platform</b>
                              below, and the command needs no path at all.</>
                          : pathChoices.length > 0
                          ? <> Several files on this machine are named <span className="mono">{file.name}</span> —
                              choose the right one above and the path becomes exact.</>
                          : resolverBlind
                          ? <> A single-file picker hands over the file&apos;s <i>name</i> and nothing else, and the
                              API answering this page <b>runs in a container</b> — it has its own filesystem, so it
                              cannot look your file up and no absolute path can come from it. Start the local dev
                              server to get one, or use <b>Pick a folder</b> below, whose paths come from the browser
                              rather than the server.</>
                          : <> A single-file picker hands over the file&apos;s <i>name</i> and nothing else, and the
                              server found no file by that name. Type the folder above, use <b>Pick a folder</b>,
                              or <b>Stage on the platform</b>, which drops the path from the command entirely.</>}
                    </>
                  : 'Choose a file under "Try it" below and it is filled in here automatically. Accepted formats match the Documents tab: pdf, docx, pptx, xlsx, csv, html, epub, msg, txt and markdown.'}
          </p>
        </div>
      )}

      {enabled && revealedKey && (
        <div className="card pad">
          <div className="row-split" style={{ marginBottom: 10 }}><b className="small">Try it</b></div>
          {sources.length > 0 && (
            <div className="field" style={{ marginBottom: 10 }}>
              <label id="kb-upload-origin-label" htmlFor="kb-upload-origin">Source</label>
              <SearchSelect
                id="kb-upload-origin" labelledBy="kb-upload-origin-label" noun="source"
                value={origin}
                options={[
                  { value: '', label: 'My computer (multipart upload)' },
                  ...sources.map((s) => ({ value: s.name, label: s.name })),
                ]}
                onChange={(v) => { setOrigin(v); setPicked(new Map()); setSourceResult(null); setUploadResult(null); }}
              />
            </div>
          )}
          {origin === '' ? (
            <>
              <div className="row-split" style={{ gap: 14, justifyContent: 'flex-start', marginBottom: 10 }}>
                {(['file', 'folder'] as const).map((m) => (
                  <label key={m} className="small" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <input
                      type="radio"
                      name="kb-local-mode"
                      checked={localMode === m}
                      onChange={() => {
                        setLocalMode(m);
                        setFile(null);
                        setFolderFiles([]);
                        setUploadResult(null);
                        setStaged(null);
                        // A directory pick yields everything below the
                        // chosen folder, so the only unknown left is
                        // where that folder sits. The OS's home root is
                        // the likeliest answer and the one thing about
                        // an absolute path a browser does know, so it
                        // is offered rather than left blank.
                        // Nothing to seed any more: each mode keeps its
                        // own root, so neither can contaminate the other.
                      }}
                    />
                    {m === 'file'
                      ? 'Pick one file'
                      : 'Pick a folder — browse it, and same-named files stay unambiguous'}
                  </label>
                ))}
              </div>
              {localMode === 'file' ? (
                <div className="row-split" style={{ gap: 8 }}>
                  <input
                    type="file"
                    onChange={(e) => {
                      const picked = e.target.files?.[0] || null;
                      setFile(picked);
                      setUploadResult(null);
                      setStaged(null);
                      // A bare name is all this picker gives, so the size
                      // goes too — it is what makes the name resolvable.
                      if (picked) resolveRoot(picked.name, picked.size);
                    }}
                    style={{ flex: 1 }}
                  />
                  <button className="btn sm" disabled={uploading || !file} onClick={staged ? runStagedUpload : runUpload}>
                    <Icon name="i-upload" /> {uploading ? 'Uploading…' : 'Call API'}
                  </button>
                </div>
              ) : (
                <>
                  {/* webkitdirectory is not in React's typings and is
                      non-standard, but it is what Chrome, Edge, Safari
                      and Firefox all implement; there is no standard
                      equivalent that returns paths. */}
                  <div className="small muted" style={{ marginBottom: 6 }}>
                    Choose the folder containing your file — every file inside is listed with its real
                    path, and you pick which one the command uses. This is the only way a browser will
                    reveal a path at all.
                  </div>
                  <input
                    type="file"
                    multiple
                    {...({ webkitdirectory: '', directory: '' } as any)}
                    onChange={(e) => {
                      const all = Array.from(e.target.files || []);
                      // Directory picks sweep up .DS_Store, .git and
                      // everything else in the tree, so only files this
                      // pipeline can actually parse are offered.
                      const usable = all.filter((f) => isIngestibleName(f.name, !!speech?.available));
                      setFolderFiles(usable);
                      setFile(usable[0] || null);
                      setUploadResult(null);
                      // The browser will not say where this folder sits,
                      // so ask the API to look on its own filesystem.
                      // Works in local dev (same machine); a container
                      // or remote deployment reports not-found and the
                      // wording stays honestly "assumed".
                      const probe = (usable[0] as any)?.webkitRelativePath;
                      if (probe) resolveRoot(probe, usable[0]?.size);
                    }}
                    style={{ width: '100%' }}
                  />
                  {folderFiles.length > 0 && (
                    <>
                      <div className="ds-browser" style={{ marginTop: 10 }}>
                        <ul className="ds-list">
                          {folderFiles.map((f) => {
                            const rel = (f as any).webkitRelativePath || f.name;
                            return (
                              <li key={rel} className="ds-row">
                                <label className="ds-name">
                                  <input
                                    type="radio"
                                    name="kb-local-folder-file"
                                    checked={file === f}
                                    onChange={() => {
                                      setFile(f);
                                      setUploadResult(null);
                                      resolveRoot(rel, f.size);
                                    }}
                                  />
                                  <span className="mono small ds-label">{rel}</span>
                                </label>
                                <span className="small muted">{Math.max(1, Math.round(f.size / 1024))} KB</span>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                      <div className="row-split" style={{ marginTop: 10 }}>
                        <span className="small muted">
                          {folderFiles.length} ingestible file(s) found — the selected one is filled into the command above.
                        </span>
                        <button className="btn sm" disabled={uploading || !file} onClick={runUpload}>
                          <Icon name="i-upload" /> {uploading ? 'Uploading…' : 'Call API'}
                        </button>
                      </div>
                    </>
                  )}
                </>
              )}
              {file && (
                <div className="row-split" style={{ marginTop: 10, gap: 8 }}>
                  <span className="small muted">
                    {staged
                      ? <>Held by the platform as <span className="mono">{staged.id}</span> — the command needs no path.</>
                      : <>Or hand the file to the platform once, and the command stops needing a local path at all.</>}
                    {/* Named before the click, not after: staging writes
                        the file to real storage, and which bucket that
                        is belongs in front of the person doing it. */}
                    {storage && (
                      <>
                        {' '}Kept in <span className="chip line">{stagingLabel}</span>{' '}
                        <button className="link-btn small" onClick={() => setDestOpen(true)}>Change</button>
                      </>
                    )}
                  </span>
                  <button className="btn sm ghost" disabled={staging} onClick={stage}>
                    <Icon name="i-plus" /> {staging ? 'Staging…' : staged ? 'Re-stage' : 'Stage on the platform'}
                  </button>
                </div>
              )}
              {destOpen && storage && (
                <UploadDestinationModal
                  target={storage}
                  onClose={() => setDestOpen(false)}
                  onChosen={(id, refreshed) => { setStorage(refreshed); setDestinationId(id); }}
                />
              )}
            </>
          ) : (
            <>
              <SourceBrowser source={origin} selected={picked} onToggle={togglePick} onToggleMany={toggleMany} />
              <div className="row-split" style={{ marginTop: 10 }}>
                <span className="small muted">
                  {picked.size === 0
                    ? 'Tick files, or a folder to take everything inside it.'
                    : `${picked.size} item(s) selected`}
                </span>
                <button className="btn sm" disabled={uploading || picked.size === 0} onClick={runSourceUpload}>
                  <Icon name="i-upload" /> {uploading ? 'Uploading…' : 'Call API'}
                </button>
              </div>
            </>
          )}
          {uploadResult && (
            <div className="small" style={{ marginTop: 12 }}>
              <span className="chip green" style={{ marginRight: 6 }}>{uploadResult.chunks_inserted} chunks</span>
              inserted into <span className="mono">{uploadResult.table}</span> as document{' '}
              <span className="mono">{uploadResult.document_id}</span>
            </div>
          )}
          {sourceResult && (
            <div className="small" style={{ marginTop: 12 }}>
              {sourceResult.results.map((r) => (
                <div key={r.file_id} style={r.success ? undefined : { color: 'var(--danger, #b4231d)' }}>
                  {r.success
                    ? <><span className="chip green" style={{ marginRight: 6 }}>{r.chunks_inserted} chunks</span>
                        <span className="mono">{r.filename}</span> → <span className="mono">{r.table}</span> as{' '}
                        <span className="mono">{r.document_id}</span></>
                    : `${r.filename || r.file_id} — ${r.error}`}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {!enabled && <Banner kind="info">Enable the Upload API above to generate a key and push documents into this pipeline from outside this platform.</Banner>}
    </div>
  );
}

// Labelled without the word "API" — the parent tab already says it, and
// "Retrieval API" here would collide with the toggle card heading of the
// same name inside the panel.
const API_SUBTABS: Array<{ id: 'retrieval' | 'upload'; label: string }> = [
  { id: 'retrieval', label: 'Retrieval' },
  { id: 'upload', label: 'Upload' },
];

function ApiTab({ kb, onSaved }: { kb: KnowledgeBase; onSaved: (kb: KnowledgeBase) => void }) {
  const [sub, setSub] = useState<'retrieval' | 'upload'>('retrieval');
  return (
    <div>
      <div className="tabs api-subtabs inner-sticky">
        {API_SUBTABS.map((t) => (
          <div key={t.id} className={`tab${sub === t.id ? ' on' : ''}`} onClick={() => setSub(t.id)}>{t.label}</div>
        ))}
      </div>
      {sub === 'retrieval' ? <RetrievalApiPanel kb={kb} onSaved={onSaved} /> : <UploadApiPanel kb={kb} onSaved={onSaved} />}
    </div>
  );
}

const TAB_LABELS: Record<string, string> = { documents: 'Documents', metadata: 'Metadata', retrieval: 'Retrieval Testing', pipeline: 'Settings', api: 'API' };

export default function KnowledgeBaseDetail({ kb: kbProp, onBack }: { kb: KnowledgeBase; onBack: () => void }) {
  const [tab, setTab] = useState('documents');
  const [kb, setKb] = useState(kbProp);
  // Every tab is offered for every provider with an ingest adapter. The
  // API tab was briefly hidden for hosted stores because its endpoints
  // demanded pgvector — the honest fix was to make them dispatch per
  // provider, not to remove the control.
  const tabs = Object.entries(TAB_LABELS);
  // The header and tabs stay put; each tab scrolls inside this frame,
  // sized to the room left on screen (scrollbar hidden, as on Traces).
  const scrollRef = useFitToViewport<HTMLDivElement>([tab]);
  return (
    <div>
      <div className="row-split" style={{ marginBottom: 14 }}>
        <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
          <button className="btn sm ghost" onClick={onBack}><Icon name="i-chev" /> Back</button>
          <b style={{ fontSize: 16 }}>{kb.config.icon ? `${kb.config.icon} ` : ''}{kb.name}</b><span className="chip line">{kb.provider}</span>
        </div>
      </div>
      <div className="tabs" style={{ marginBottom: 16 }}>
        {tabs.map(([t, l]) => (
          <div key={t} className={`tab${tab === t ? ' on' : ''}`} onClick={() => setTab(t)}>{l}</div>
        ))}
      </div>
      <div className="inner-scroll" ref={scrollRef} key={tab}>
        {tab === 'documents' && <DocumentsTab kb={kb} />}
        {tab === 'metadata' && <MetadataTab kb={kb} />}
        {tab === 'retrieval' && <RetrievalTestingTab kb={kb} onOpenSettings={() => setTab('pipeline')} />}
        {tab === 'pipeline' && <PipelineTab kb={kb} onSaved={setKb} />}
        {tab === 'api' && <ApiTab kb={kb} onSaved={setKb} />}
      </div>
    </div>
  );
}
