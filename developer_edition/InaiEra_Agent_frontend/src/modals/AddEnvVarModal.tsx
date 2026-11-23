import { useState } from 'react';
import { api } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';

const TYPES: { value: string; label: string; hint?: string }[] = [
  { value: 'string', label: 'String' },
  { value: 'number', label: 'Number' },
  { value: 'secret', label: 'Secret', hint: 'Masked after saving — never shown in plaintext again' },
];

export default function AddEnvVarModal({ agentId }: { agentId: string }) {
  const { closeModal, openDrawer, toast } = useApp();
  const [type, setType] = useState('string');
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);

  const save = () => {
    if (!key.trim()) { toast('Name is required'); return; }
    if (!/^[A-Za-z0-9_]+$/.test(key.trim())) { toast('Name must be alphanumeric/underscore, e.g. API_BASE_URL'); return; }
    setSaving(true);
    api.createEnvVar(agentId, { key: key.trim(), value, type, description })
      .then(() => {
        toast(`${key.trim()} saved — reference it as {{env.${key.trim()}}} in any node`);
        closeModal();
        openDrawer('envVars', agentId);
      })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal title="Add Environment Variable" onClose={closeModal} footer={
      <><button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
        <button className="btn" onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button></>
    }>
      <div className="field">
        <label>Type</label>
        <div className="radio-cards" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
          {TYPES.map((t) => (
            <div key={t.value} className={`rc${type === t.value ? ' on' : ''}`} onClick={() => setType(t.value)} title={t.hint}>
              <div className="nm" style={{ display: 'flex', alignItems: 'center', gap: 4, justifyContent: 'center' }}>
                {t.label}{t.hint && <Icon name="i-alert" style={{ width: 12, height: 12, opacity: 0.6 }} />}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="field">
        <label>Name</label>
        <input className="mono" placeholder="env name" value={key} onChange={(e) => setKey(e.target.value.toUpperCase())} />
      </div>
      <div className="field">
        <label>Value</label>
        {type === 'secret'
          ? <input type="password" placeholder="env value" value={value} onChange={(e) => setValue(e.target.value)} />
          : <textarea placeholder="env value" rows={3} value={value} onChange={(e) => setValue(e.target.value)} />}
      </div>
      <div className="field">
        <label>Description</label>
        <textarea placeholder="Describe the variable" rows={3} value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
    </Modal>
  );
}
