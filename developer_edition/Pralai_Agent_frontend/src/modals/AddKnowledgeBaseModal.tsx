import { useState } from 'react';
import { api } from '../api';
import Modal from '../components/Modal';
import SearchSelect from '../components/SearchSelect';
import { useApp } from '../context/AppContext';

const PROVIDERS = [
  { value: 'pgvector', label: 'pgvector (Postgres)' },
  { value: 'pinecone', label: 'Pinecone' },
  { value: 'weaviate', label: 'Weaviate' },
  { value: 'qdrant', label: 'Qdrant' },
  { value: 'milvus', label: 'Milvus' },
  { value: 'other', label: 'Other (generic REST)' },
];

export default function AddKnowledgeBaseModal() {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [name, setName] = useState('');
  const [icon, setIcon] = useState('🤖');
  const [description, setDescription] = useState('');
  const [provider, setProvider] = useState('pgvector');
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const set = (k: string, v: string) => setCfg((c) => ({ ...c, [k]: v }));

  const save = () => {
    if (!name.trim()) { toast('Name is required'); return; }
    setSaving(true);
    const config: Record<string, any> = { ...cfg, icon, description: description.trim() };
    if (config.port) config.port = Number(config.port);
    api.createKnowledgeBase({ name: name.trim(), provider, config })
      .then((kb) => {
        toast(provider === 'pgvector' ? `${kb.name} added — manage its real document pipeline on the RAG page` : `${kb.name} added — click Test to verify the connection`);
        notifyDataChanged();
        closeModal();
      })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal title="Add Knowledge Base" sub="This catalogs a real connection to your vector store — document ingestion, chunking, and retrieval settings for it live on the RAG page." onClose={closeModal} footer={
      <><button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
        <button className="btn" onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button></>
    }>
      <div className="row-split" style={{ gap: 10, alignItems: 'flex-end' }}>
        <div className="field" style={{ flex: 0, margin: 0 }}>
          <label>Icon</label>
          <input value={icon} onChange={(e) => setIcon(e.target.value)} style={{ width: 56, textAlign: 'center', fontSize: 18 }} />
        </div>
        <div className="field" style={{ flex: 1, margin: 0 }}>
          <label>Name</label>
          <input placeholder="e.g. Support Articles" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      </div>
      <div className="field">
        <label>Description</label>
        <textarea rows={2} placeholder="Describe what is in this data set. A detailed description helps agents pick the right knowledge base." value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="field">
        <label id="add-kb-provider-label">Provider</label>
        <SearchSelect labelledBy="add-kb-provider-label" noun="provider" value={provider}
          options={PROVIDERS.map((p) => ({ value: p.value, label: p.label }))}
          onChange={(v) => { setProvider(v); setCfg({}); }} />
      </div>

      {provider === 'pgvector' && (
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
            <div className="field" style={{ flex: 1 }}><label>Password</label><input type="password" value={cfg.password || ''} onChange={(e) => set('password', e.target.value)} /></div>
          </div>
          <div className="field"><label>Table</label><input placeholder="support_articles" value={cfg.table || ''} onChange={(e) => set('table', e.target.value)} /></div>
          <div className="field"><label>Text column</label><input placeholder="content" value={cfg.text_column || ''} onChange={(e) => set('text_column', e.target.value)} /></div>
          <p className="small muted">Chunk Structure, Index Method, and Retrieval Setting are configured after creation, on the RAG page — click this knowledge base there once it's connected.</p>
        </>
      )}

      {provider !== 'pgvector' && (
        <>
          <div className="field"><label>Endpoint URL</label><input placeholder="https://..." value={cfg.url || ''} onChange={(e) => set('url', e.target.value)} /></div>
          <div className="field"><label>API key</label><input type="password" value={cfg.api_key || ''} onChange={(e) => set('api_key', e.target.value)} /></div>
          <p className="small muted">Connection is real (a genuine reachability check), but query execution for this provider isn't wired into the Retrieval node yet — pgvector is the only provider with a real document pipeline today.</p>
        </>
      )}
    </Modal>
  );
}
