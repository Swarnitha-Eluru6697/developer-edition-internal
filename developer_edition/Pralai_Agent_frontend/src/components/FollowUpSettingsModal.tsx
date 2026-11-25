import { useState } from 'react';
import Modal from './Modal';
import SearchSelect from './SearchSelect';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, AgentFeatures } from '../types';

const MODELS = ['Claude Sonnet 5', 'Claude Opus 4.8', 'Claude Haiku 4.5'];
const DEFAULT_PROMPT = 'Suggest exactly 3 short, natural follow-up questions the user might ask next, grounded in the real conversation that just happened.';

export default function FollowUpSettingsModal({ agent, onClose, onSaved }: { agent: Agent; onClose: () => void; onSaved: (a: Agent) => void }) {
  const { toast } = useApp();
  const existing = agent.features?.follow_up;
  const [model, setModel] = useState(existing?.model || agent.model || MODELS[0]);
  const [mode, setMode] = useState<'default' | 'custom'>(existing?.prompt_mode || 'default');
  const [customPrompt, setCustomPrompt] = useState(existing?.custom_prompt || '');

  const save = () => {
    if (mode === 'custom' && !customPrompt.trim()) { toast('Enter a custom prompt, or switch back to the system default'); return; }
    const next: AgentFeatures = {
      ...(agent.features || {}),
      follow_up: { enabled: true, prompt_mode: mode, custom_prompt: customPrompt, model },
    };
    api.setAgentFeatures(agent.id, next).then((a) => { onSaved(a); toast('Follow-up settings saved'); onClose(); }).catch((e) => toast(e.message));
  };

  return (
    <Modal title="Follow-up settings" onClose={onClose} footer={
      <><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" onClick={save} disabled={mode === 'custom' && !customPrompt.trim()}>Save</button></>
    }>
      <div className="field">
        <label id="followup-model-label">Model</label>
        <SearchSelect labelledBy="followup-model-label" noun="model" value={model}
          options={MODELS.map((m) => ({ value: m, label: m }))}
          onChange={(v) => setModel(v)} />
      </div>
      <div className="field">
        <label>Prompt</label>
        <div className={`radio-card-lg${mode === 'default' ? ' on' : ''}`} onClick={() => setMode('default')}>
          <div className="row-split"><b>System default prompt</b><span className={`radio-dot${mode === 'default' ? ' on' : ''}`} /></div>
          <div className="small muted">Use the built-in prompt for follow-up question generation.</div>
          {mode === 'default' && <div className="prompt-preview">{DEFAULT_PROMPT}</div>}
        </div>
        <div className={`radio-card-lg${mode === 'custom' ? ' on' : ''}`} onClick={() => setMode('custom')}>
          <div className="row-split"><b>Custom prompt</b><span className={`radio-dot${mode === 'custom' ? ' on' : ''}`} /></div>
          <div className="small muted">Write and use your own follow-up generation prompt. Use <code>{'{{input}}'}</code> and <code>{'{{output}}'}</code> to reference the real conversation.</div>
          {mode === 'custom' && (
            <textarea rows={5} placeholder="Enter a custom prompt" value={customPrompt} onChange={(e) => setCustomPrompt(e.target.value)} onClick={(e) => e.stopPropagation()} style={{ marginTop: 10 }} />
          )}
        </div>
      </div>
    </Modal>
  );
}
