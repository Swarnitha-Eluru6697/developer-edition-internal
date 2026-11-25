import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Modal from '../components/Modal';
import { Banner } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { KnowledgeBase } from '../types';
import SearchSelect, { type SelectOption } from '../components/SearchSelect';

const PROVIDERS = [
  { value: 'pgvector', label: 'pgvector (Postgres)' },
  { value: 'pinecone', label: 'Pinecone' },
  { value: 'weaviate', label: 'Weaviate' },
  { value: 'qdrant', label: 'Qdrant' },
  { value: 'milvus', label: 'Milvus' },
  { value: 'other', label: 'Other (generic REST)' },
];

// Which config keys each provider actually uses. pgvector is the only
// one with a real document pipeline; the rest store a URL and a key,
// matching what Integrations → KB Connections already writes.
const CONNECTION_KEYS: Record<string, string[]> = {
  pgvector: ['host', 'port', 'dbname', 'user', 'password', 'table', 'text_column'],
};
const DEFAULT_KEYS = ['url', 'api_key'];

export default function NewRagPipelineModal() {
  const { closeModal, toast } = useApp();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [icon, setIcon] = useState('🤖');
  const [description, setDescription] = useState('');
  const [provider, setProvider] = useState('pgvector');
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  // The knowledge base this pipeline borrows its connection from. Kept
  // so the server can reuse the stored secret, which reads only ever
  // return masked.
  const [sourceKb, setSourceKb] = useState('');
  const set = (k: string, v: string) => setCfg((c) => ({ ...c, [k]: v }));

  useEffect(() => { api.listKnowledgeBases().then(setKbs).catch(() => {}); }, []);

  const keysFor = (p: string) => CONNECTION_KEYS[p] || DEFAULT_KEYS;

  const pickSource = (id: string) => {
    setSourceKb(id);
    if (!id) return;
    const src = kbs.find((k) => k.id === id);
    if (!src) return;
    const c: any = src.config || {};
    // The provider drives which fields exist, so it has to switch first.
    setProvider(src.provider);
    const next: Record<string, string> = {};
    keysFor(src.provider).forEach((k) => {
      // A masked secret is not a value that can be copied client-side —
      // the server does that from copy_credentials_from.
      const v = c[k];
      next[k] = v == null || String(v).startsWith('•') ? '' : String(v);
    });
    // pgvector pipelines each need their own table; everything else can
    // legitimately point at the same collection.
    if (src.provider === 'pgvector') next.table = '';
    setCfg(next);
    if (c.icon) setIcon(String(c.icon));
    // The id is derived from the name, so it must be unique — offer an
    // editable suggestion rather than a value that will be rejected.
    if (!name.trim()) setName(`${src.name} copy`);
  };

  const changeProvider = (p: string) => {
    setProvider(p);
    setSourceKb('');
    setCfg({});
  };

  const create = () => {
    if (!name.trim()) { toast('Name is required'); return; }
    setSaving(true);
    const config: Record<string, any> = { ...cfg, icon, description: description.trim() };
    if (config.port) config.port = Number(config.port);
    const secretKey = provider === 'pgvector' ? 'password' : 'api_key';
    api.createKnowledgeBase({
      name: name.trim(), provider, config,
      ...(sourceKb && !cfg[secretKey] ? { copy_credentials_from: sourceKb } : {}),
    })
      .then((kb) => {
        toast(provider === 'pgvector'
          ? 'RAG pipeline created — add a document to get started'
          : `${kb.name} created — connection cataloged, but only pgvector has a document pipeline`);
        closeModal();
        navigate(`/rag?open=${kb.id}`);
      })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  const secretLabel = provider === 'pgvector' ? 'Password' : 'API key';
  const reusingSecret = !!sourceKb;

  return (
    <Modal title="New RAG Pipeline"
      sub="Name it and enter its connection, or start from a knowledge base you have already configured."
      onClose={closeModal} footer={
        <><button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
          <button className="btn" onClick={create} disabled={saving}>{saving ? 'Creating…' : 'Create'}</button></>
      }>
      <div className="row-split" style={{ gap: 10, alignItems: 'flex-end' }}>
        <div className="field" style={{ flex: 0, margin: 0 }}>
          <label>Icon</label>
          <input value={icon} onChange={(e) => setIcon(e.target.value)} style={{ width: 56, textAlign: 'center', fontSize: 18 }} />
        </div>
        <div className="field" style={{ flex: 1, margin: 0 }}>
          <label>Name *</label>
          <input placeholder="e.g. Support Articles" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      </div>
      <div className="field">
        <label>Description</label>
        <textarea rows={2} placeholder="Describe what is in this data set." value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>

      {kbs.length > 0 && (
        <div className="field">
          <label id="rag-source-kb-label">Start from an existing knowledge base</label>
          {/* Searchable and grouped by provider: the name on top, where it
              connects on a quieter second line, and its last known status. */}
          <SearchSelect id="rag-source-kb" labelledBy="rag-source-kb-label"
            value={sourceKb} onChange={pickSource} noun="connection"
            searchPlaceholder="Search by name, host or provider…" emptyText="No connection matches"
            options={[
              { value: '', label: 'Enter connection details manually', description: 'Start from an empty form' },
              ...PROVIDERS.filter((p) => kbs.some((k) => k.provider === p.value)).flatMap((p) =>
                kbs.filter((k) => k.provider === p.value).map((k): SelectOption => ({
                  value: k.id,
                  label: k.name,
                  group: p.label,
                  description: k.config?.host
                    ? `${k.config.user}@${k.config.host}:${k.config.port}/${k.config.dbname}`
                    : (k.config?.url ? String(k.config.url).replace(/^https?:\/\//, '') : undefined),
                  hint: k.status,
                }))),
            ]} />
          <div className="small muted" style={{ marginTop: 4 }}>
            {sourceKb
              ? `Copied from that knowledge base — the ${secretLabel.toLowerCase()} is reused, so leave it blank.`
              : 'Grouped by provider. Picking one switches the fields below to that provider and fills them in.'}
          </div>
        </div>
      )}

      <div className="field">
        <label id="new-rag-provider-label">Provider</label>
        <SearchSelect labelledBy="new-rag-provider-label" noun="provider" value={provider}
          options={PROVIDERS.map((p) => ({ value: p.value, label: p.label }))}
          onChange={(v) => changeProvider(v)} />
      </div>

      {provider === 'pgvector' ? (
        <>
          <div className="row-split" style={{ gap: 10 }}>
            <div className="field" style={{ flex: 3 }}>
              <label>Host</label>
              <input placeholder="127.0.0.1" value={cfg.host || ''} onChange={(e) => set('host', e.target.value)} />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>Port</label>
              <input placeholder="5432" value={cfg.port || ''} onChange={(e) => set('port', e.target.value)} />
            </div>
          </div>
          <div className="field"><label>Database name</label><input value={cfg.dbname || ''} onChange={(e) => set('dbname', e.target.value)} /></div>
          <div className="row-split" style={{ gap: 10 }}>
            <div className="field" style={{ flex: 1 }}><label>User</label><input value={cfg.user || ''} onChange={(e) => set('user', e.target.value)} /></div>
            <div className="field" style={{ flex: 1 }}>
              <label>Password{reusingSecret ? ' (optional)' : ''}</label>
              <input type="password" placeholder={reusingSecret ? 'Reusing the stored one' : ''}
                value={cfg.password || ''} onChange={(e) => set('password', e.target.value)} />
            </div>
          </div>
          <div className="field">
            <label>Table *</label>
            <input placeholder="support_articles" value={cfg.table || ''} onChange={(e) => set('table', e.target.value)} />
            {sourceKb && <div className="small muted" style={{ marginTop: 4 }}>Give this pipeline its own table — two pipelines sharing one table would share every document.</div>}
          </div>
          <div className="field"><label>Text column</label><input placeholder="content" value={cfg.text_column || ''} onChange={(e) => set('text_column', e.target.value)} /></div>
        </>
      ) : (
        <>
          <div className="field"><label>Endpoint URL</label><input placeholder="https://..." value={cfg.url || ''} onChange={(e) => set('url', e.target.value)} /></div>
          <div className="field">
            <label>API key{reusingSecret ? ' (optional)' : ''}</label>
            <input type="password" placeholder={reusingSecret ? 'Reusing the stored one' : ''}
              value={cfg.api_key || ''} onChange={(e) => set('api_key', e.target.value)} />
          </div>
          <Banner kind="info">
            The connection is cataloged and reachability is genuinely checked, but only pgvector has a
            document pipeline today — a {PROVIDERS.find((p) => p.value === provider)?.label} entry cannot
            ingest documents or answer Retrieval nodes yet.
          </Banner>
        </>
      )}
    </Modal>
  );
}
