import { useEffect, useState } from 'react';
import { confirmAction } from '../components/ConfirmPanel';
import { api } from '../api';
import Modal from '../components/Modal';
import { Banner } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { KnowledgeBase } from '../types';

// Mirrors CONNECTION_KEYS in routers/knowledge_bases.py — the server
// rejects anything outside these, so sending a pipeline setting from
// here would be an error rather than a silent no-op.
const FIELDS: Record<string, { key: string; label: string; secret?: boolean; hint?: string }[]> = {
  pgvector: [
    { key: 'host', label: 'Host' },
    { key: 'port', label: 'Port' },
    { key: 'dbname', label: 'Database name' },
    { key: 'user', label: 'User' },
    { key: 'password', label: 'Password', secret: true },
    { key: 'table', label: 'Table' },
    { key: 'text_column', label: 'Text column' },
  ],
  pinecone: [
    { key: 'index', label: 'Index', hint: 'The Pinecone index name — not the console URL.' },
    { key: 'namespace', label: 'Namespace', hint: 'Defaults to the knowledge base id, keeping these chunks apart from anything else in the index.' },
    { key: 'url', label: 'Endpoint URL (optional)' },
    { key: 'api_key', label: 'API key', secret: true },
  ],
};
const DEFAULT_FIELDS = [
  { key: 'url', label: 'Endpoint URL' },
  { key: 'index', label: 'Index / collection' },
  { key: 'namespace', label: 'Namespace' },
  { key: 'api_key', label: 'API key', secret: true },
];

export default function EditKbConnectionModal({ kbId }: { kbId: string }) {
  const { closeModal, toast, notifyDataChanged } = useApp();
  // ModalHost carries a single string argument, so the knowledge base
  // is resolved here rather than threaded through as an object.
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const fields = kb ? (FIELDS[kb.provider] || DEFAULT_FIELDS) : [];

  useEffect(() => {
    api.listKnowledgeBases()
      .then((all) => {
        const found = all.find((k) => k.id === kbId) || null;
        setKb(found);
        if (!found) return;
        const f = FIELDS[found.provider] || DEFAULT_FIELDS;
        const init: Record<string, string> = {};
        f.forEach((x) => {
          const v = (found.config as any)?.[x.key];
          // A secret reads back masked, so it starts empty and only
          // overwrites the stored value if something is typed.
          init[x.key] = x.secret || v == null ? '' : String(v);
        });
        setCfg(init);
      })
      .catch((e) => toast(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId]);

  const save = (confirmRelocate = false) => {
    if (!kb) return;
    // Only send what was actually filled in — an empty field means
    // "leave it alone", not "clear it".
    const payload: Record<string, any> = {};
    fields.forEach((f) => { if (cfg[f.key] !== '') payload[f.key] = cfg[f.key]; });
    if (Object.keys(payload).length === 0) { toast('Nothing to change'); return; }
    setSaving(true);
    api.updateKbConnection(kb.id, payload, confirmRelocate)
      .then((updated) => {
        closeModal();
        // Saving resets the status to "untested", and the RAG page has no
        // Test button — so a re-entered password or key used to sit there
        // looking broken. Test it straight away with the same check the
        // Test button runs, and refresh the lists once the real status is in.
        const moved = updated.relocated?.length ? ' (now points at different storage)' : '';
        toast(`Connection updated${moved} — testing it…`);
        api.testKnowledgeBase(kb.id)
          .then((r) => toast(r.test_result.ok
            ? `${kb.name}: connected`
            : `${kb.name}: saved, but the test failed — ${(r.test_result.error || r.test_result.detail || '').slice(0, 140)}`))
          .catch((e) => toast(`${kb.name}: saved, but the test could not run — ${e.message}`))
          .finally(() => notifyDataChanged());
      })
      .catch((e) => {
        // 409 means this would repoint a knowledge base that already
        // lists documents; the user is the one who can judge that.
        if (String(e.message).includes('confirm_relocate')) {
          confirmAction({ title: 'Repoint this knowledge base?', confirmLabel: 'Proceed anyway', body: e.message })
            .then((ok) => { if (ok) save(true); });
        } else {
          toast(e.message);
        }
      })
      .finally(() => setSaving(false));
  };

  if (!kb) {
    return (
      <Modal title="Edit connection" onClose={closeModal} footer={<button className="btn ghost" onClick={closeModal}>Close</button>}>
        <div className="small muted">Loading…</div>
      </Modal>
    );
  }

  return (
    <Modal title={`Edit connection — ${kb.name}`}
      sub={`Provider is fixed at ${kb.provider}; the fields below are its connection details.`}
      onClose={closeModal}
      footer={<>
        <button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
        <button className="btn" onClick={() => save(false)} disabled={saving}>{saving ? 'Saving…' : 'Save connection'}</button>
      </>}>
      {fields.map((f) => (
        <div className="field" key={f.key}>
          <label>{f.label}</label>
          <input
            type={f.secret ? 'password' : 'text'}
            placeholder={f.secret ? 'Leave blank to keep the stored one' : ''}
            value={cfg[f.key]}
            onChange={(e) => setCfg((c) => ({ ...c, [f.key]: e.target.value }))}
          />
          {f.hint && <div className="small muted" style={{ marginTop: 4 }}>{f.hint}</div>}
        </div>
      ))}
      <Banner kind="info">
        Saving resets this connection's status to untested — whatever the last Test proved was about
        the previous details. Run Test afterwards.
      </Banner>
    </Modal>
  );
}
