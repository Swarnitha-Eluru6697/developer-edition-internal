import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';

const APP_TYPES: { value: string; label: string }[] = [
  { value: 'workflow', label: 'Workflow' },
  { value: 'chatflow', label: 'Chatflow' },
];

export default function GenerateAgentModal() {
  const { closeModal, toast } = useApp();
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState('');
  const [name, setName] = useState('');
  const [appType, setAppType] = useState('workflow');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generate = () => {
    if (!prompt.trim()) { setError('Describe what you want this agent to do first.'); return; }
    setLoading(true);
    setError(null);
    api.generateAgent({ prompt, name: name || 'Untitled generated agent', app_type: appType })
      .then((a) => {
        toast('Generated a real draft — opening it in the Builder for you to review');
        closeModal();
        navigate(`/builder?agent=${a.id}`);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  return (
    <Modal
      title="Create with AI"
      sub="Generates a real draft in the Builder — nothing is ever published automatically. Review and edit it like any hand-built agent before publishing."
      onClose={closeModal}
      footer={<>
        <button className="btn ghost" onClick={closeModal} disabled={loading}>Cancel</button>
        <button className="btn" onClick={generate} disabled={loading}>{loading ? 'Generating…' : 'Generate'}</button>
      </>}
    >
      <div className="field">
        <label>What should this agent do?</label>
        <textarea
          rows={4}
          placeholder="e.g. Classify support tickets by urgency, and page a human for high-urgency ones."
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          disabled={loading}
        />
      </div>
      <div className="field"><label>Name</label><input placeholder="e.g. Support Ticket Triage" value={name} onChange={(e) => setName(e.target.value)} disabled={loading} /></div>
      <div className="field">
        <label>App type</label>
        <div className="trigger-type-list">
          {APP_TYPES.map((t) => (
            <div key={t.value} className={`trigger-type-row${appType === t.value ? ' sel' : ''}`} onClick={() => !loading && setAppType(t.value)}>
              <span className="tt-label">{t.label}</span>
            </div>
          ))}
        </div>
      </div>
      {error && (
        <div className="small" style={{ color: 'var(--red, #C0392B)', marginTop: 8 }}>
          <Icon name="i-alert" /> {error}
        </div>
      )}
    </Modal>
  );
}
