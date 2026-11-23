import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import SearchSelect from '../components/SearchSelect';
import { useApp } from '../context/AppContext';
import type { Template } from '../types';

const APP_TYPES: { value: string; label: string; icon: string; desc: string }[] = [
  { value: 'workflow', label: 'Workflow', icon: 'i-flow', desc: 'End-user UI is a form: fields in, one structured result out. No chat history.' },
  { value: 'chatflow', label: 'Chatflow', icon: 'i-comment', desc: 'End-user UI is a real chat, backed by this agent\'s own flow graph.' },
  { value: 'chatbot', label: 'Chatbot', icon: 'i-comment', desc: 'End-user UI is a plain chat — simplest conversational surface.' },
  { value: 'agent', label: 'Agent', icon: 'i-agent', desc: 'End-user UI is a chat that also shows the real reasoning steps (ReAct/Plan/Function-calling), if the flow has an Agent node.' },
  { value: 'text_generator', label: 'Text Generator', icon: 'i-file', desc: 'End-user UI is one prompt in, one generated result out — no history at all.' },
];

export default function NewAgentModal() {
  const { closeModal, toast } = useApp();
  const navigate = useNavigate();
  const [appType, setAppType] = useState('chatbot');
  const [name, setName] = useState('');
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateId, setTemplateId] = useState('');

  // Only App templates — Data agents are provisioned by a separate
  // service, so they were never something this modal built fresh; the
  // "Data" branch here just filtered templates for a type toggle that no
  // longer exists.
  useEffect(() => { api.listTemplates().then((ts) => setTemplates(ts.filter((t) => t.type === 'App' || t.name === 'Blank canvas'))).catch(() => {}); }, []);

  const create = () => {
    // No template unless one was actually chosen. This used to fall back
    // to `templates[0]`, so an empty templateId silently applied
    // whichever template happened to be first — the "Start from" select
    // had no empty option either, so it *displayed* that template while
    // the state said none was picked. Creating an agent without choosing
    // anything therefore produced a full template flow, including its
    // retrieval node and that node's knowledge base, rather than the
    // empty canvas the person asked for.
    const tmpl = templateId ? templates.find((t) => t.id === templateId) : undefined;
    api.createAgent({
      name: name || 'Untitled agent', subtype: tmpl?.name || 'Custom', owner: 'you',
      ...(tmpl ? { template_id: tmpl.id } : {}), app_type: tmpl?.app_type || appType,
    })
      .then((a) => { toast('Agent created — opening the Builder'); closeModal(); navigate(`/builder?agent=${a.id}`); })
      .catch((e) => toast(e.message));
  };

  return (
    <Modal title="New agent" sub="Data Agents are provisioned by a separate service — this builds a new App Agent (§3.4)." onClose={closeModal} footer={
      <><button className="btn ghost" onClick={closeModal}>Cancel</button><button className="btn" onClick={create}>Create</button></>
    }>
      <div className="field">
        <label>App type — determines the real end-user UI built post-publish</label>
        <div className="trigger-type-list">
          {APP_TYPES.map((t) => (
            <div key={t.value} className={`trigger-type-row${appType === t.value ? ' sel' : ''}`} onClick={() => setAppType(t.value)}>
              <div className="tt-icon"><Icon name={t.icon} /></div>
              <div style={{ flex: 1 }}>
                <span className="tt-label">{t.label}</span>
                <div className="small muted" style={{ marginTop: 2 }}>{t.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="field"><label>Name</label><input placeholder="e.g. Renewal Risk Analysis Agent" value={name} onChange={(e) => setName(e.target.value)} /></div>
      <div className="field">
        <label id="new-agent-template-label">Start from</label>
        {/* The real default, and a real option rather than an absent
            one: with no empty entry the select showed a template that
            had not been chosen. */}
        <SearchSelect labelledBy="new-agent-template-label" noun="template" value={templateId}
          options={[
            { value: '', label: 'Empty canvas — just the trigger node' },
            ...templates.map((t) => ({ value: t.id, label: `${t.name}${t.version ? ` · v${t.version}` : ''}` })),
          ]}
          onChange={(v) => { setTemplateId(v); const t = templates.find((x) => x.id === v); if (t?.app_type) setAppType(t.app_type); }} />
      </div>
    </Modal>
  );
}
