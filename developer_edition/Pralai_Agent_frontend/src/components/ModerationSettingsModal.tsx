import { useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, AgentFeatures } from '../types';

const PROVIDERS: { value: 'keywords' | 'ai' | 'custom_endpoint'; label: string; icon: string; desc: string }[] = [
  { value: 'keywords', label: 'Keywords', icon: 'i-search', desc: 'A real, deterministic block-list check against configured words.' },
  { value: 'ai', label: 'AI Moderation', icon: 'i-cpu', desc: 'A real Claude call classifies the message as SAFE or FLAGGED.' },
  { value: 'custom_endpoint', label: 'Custom Endpoint', icon: 'i-plug', desc: 'A real HTTP POST to your own endpoint, expecting {flagged, reason}.' },
];

export default function ModerationSettingsModal({ agent, onClose, onSaved }: { agent: Agent; onClose: () => void; onSaved: (a: Agent) => void }) {
  const { toast } = useApp();
  const existing = agent.features?.moderation;
  const [provider, setProvider] = useState<'keywords' | 'ai' | 'custom_endpoint'>(existing?.provider || 'keywords');
  const [inputEnabled, setInputEnabled] = useState(existing?.input_enabled ?? true);
  const [outputEnabled, setOutputEnabled] = useState(existing?.output_enabled ?? true);
  const [inputPreset, setInputPreset] = useState(existing?.input_preset_reply || '');
  const [outputPreset, setOutputPreset] = useState(existing?.output_preset_reply || '');
  const [keywordsText, setKeywordsText] = useState((existing?.keywords || []).join('\n'));
  const [endpointUrl, setEndpointUrl] = useState(existing?.custom_endpoint_url || '');

  const canSave = inputEnabled || outputEnabled;

  const save = () => {
    if (!canSave) { toast('Enable Moderate INPUT or OUTPUT Content — at least one'); return; }
    const next: AgentFeatures = {
      ...(agent.features || {}),
      moderation: {
        enabled: true, provider, input_enabled: inputEnabled, output_enabled: outputEnabled,
        input_preset_reply: inputPreset, output_preset_reply: outputPreset,
        keywords: keywordsText.split('\n').map((w) => w.trim()).filter(Boolean),
        custom_endpoint_url: endpointUrl,
      },
    };
    api.setAgentFeatures(agent.id, next).then((a) => { onSaved(a); toast('Content moderation settings saved'); onClose(); }).catch((e) => toast(e.message));
  };

  return (
    <Modal title="Content moderation settings" onClose={onClose} footer={
      <><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" onClick={save} disabled={!canSave}>Save</button></>
    }>
      <div className="field">
        <label>Provider</label>
        <div className="provider-grid">
          {PROVIDERS.map((p) => (
            <div key={p.value} className={`provider-card${provider === p.value ? ' on' : ''}`} onClick={() => setProvider(p.value)}>
              <Icon name={p.icon} /><div className="small">{p.label}</div>
            </div>
          ))}
        </div>
        <div className="small muted" style={{ marginTop: 8 }}>{PROVIDERS.find((p) => p.value === provider)?.desc}</div>
      </div>

      {provider === 'keywords' && (
        <div className="field">
          <label>Keywords</label>
          <p className="small muted" style={{ marginTop: 0 }}>One per line.</p>
          <textarea rows={4} placeholder="One per line, separated by line breaks" value={keywordsText} onChange={(e) => setKeywordsText(e.target.value)} />
        </div>
      )}
      {provider === 'custom_endpoint' && (
        <div className="field">
          <label>Custom Endpoint URL</label>
          <input placeholder="https://your-service.example.com/moderate" value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} />
          <p className="small muted" style={{ marginTop: 6 }}>A real POST is sent here with <code>{'{"text": "..."}'}</code>; expects <code>{'{"flagged": bool, "reason": "..."}'}</code> back.</p>
        </div>
      )}

      <div className="sechead">Content Moderation</div>
      <div className="mod-direction-row">
        <div className="row-split"><b>Moderate INPUT Content</b>
          <label className="switch"><input type="checkbox" checked={inputEnabled} onChange={(e) => setInputEnabled(e.target.checked)} /><span /></label>
        </div>
        {inputEnabled && (
          <div style={{ marginTop: 10 }}>
            <label className="small muted" style={{ display: 'block', marginBottom: 4 }}>PRESET REPLY</label>
            <textarea rows={2} maxLength={300} placeholder="Preset reply content here" value={inputPreset} onChange={(e) => setInputPreset(e.target.value)} />
          </div>
        )}
      </div>
      <div className="mod-direction-row">
        <div className="row-split"><b>Moderate OUTPUT Content</b>
          <label className="switch"><input type="checkbox" checked={outputEnabled} onChange={(e) => setOutputEnabled(e.target.checked)} /><span /></label>
        </div>
        {outputEnabled && (
          <div style={{ marginTop: 10 }}>
            <label className="small muted" style={{ display: 'block', marginBottom: 4 }}>PRESET REPLY</label>
            <textarea rows={2} maxLength={300} placeholder="Preset reply content here" value={outputPreset} onChange={(e) => setOutputPreset(e.target.value)} />
          </div>
        )}
      </div>
      {!canSave && <p className="small" style={{ color: 'var(--red)' }}>Moderate INPUT and OUTPUT Content — enable at least one.</p>}
    </Modal>
  );
}
