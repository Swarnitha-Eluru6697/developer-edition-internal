import { useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import { INPUT_FIELD_TYPES } from '../constants';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, FlowNode } from '../types';

export interface InputField {
  id: string;
  name: string;
  type: string;
  required: boolean;
  hidden_prefilled: boolean;
  default?: string;
  options?: string[];
}

export default function AddInputFieldModal({ agent, node, editField, onClose, onSaved }: {
  agent: Agent; node: FlowNode; editField?: InputField | null; onClose: () => void; onSaved: (a: Agent) => void;
}) {
  const { toast } = useApp();
  const [type, setType] = useState(editField?.type || 'short_text');
  const [name, setName] = useState(editField?.name || '');
  const [required, setRequired] = useState(editField?.required ?? true);
  const [hiddenPrefilled, setHiddenPrefilled] = useState(editField?.hidden_prefilled ?? false);
  const [defaultVal, setDefaultVal] = useState(editField?.default || '');
  const [typeMenuOpen, setTypeMenuOpen] = useState(false);

  const selectedType = INPUT_FIELD_TYPES.find((t) => t.value === type) || INPUT_FIELD_TYPES[0];

  // The name is the variable's name, NOT a reference to it. Typing the
  // braces here — an easy and entirely reasonable mistake, since the
  // rest of the builder always shows these as {{name}} — created a
  // field literally called "{{document}}", so every {{document}}
  // reference downstream resolved to nothing and the flow ran green
  // with empty output. Stripped rather than rejected: the intent is
  // unambiguous, so correcting it silently beats an error message.
  const cleanName = (raw: string) => raw.trim().replace(/^\{\{\s*/, '').replace(/\s*\}\}$/, '').trim();

  const save = () => {
    const finalName = cleanName(name);
    if (!finalName) { toast('Enter a field name'); return; }
    const fields: InputField[] = node.config.fields || [];
    const newField: InputField = {
      id: editField?.id || crypto.randomUUID(), name: finalName, type, required, hidden_prefilled: hiddenPrefilled,
      default: hiddenPrefilled ? defaultVal : undefined,
    };
    const nextFields = editField ? fields.map((f) => (f.id === editField.id ? newField : f)) : [...fields, newField];
    api.updateNodeConfig(agent.id, node.id, { fields: nextFields }, agent.draft_revision)
      .then((a) => { onSaved(a); toast('Input field saved'); onClose(); })
      .catch((e) => toast(e.message));
  };

  return (
    <Modal title="Add Input Field" onClose={onClose} footer={
      <><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" onClick={save}>Save</button></>
    }>
      <div className="field">
        <label>Field Type</label>
        <div className="field-type-select" onClick={() => setTypeMenuOpen((v) => !v)}>
          <Icon name={selectedType.icon} /><span style={{ flex: 1 }}>{selectedType.label}</span>
          <span className="chip line mono sm">{selectedType.dataType}</span>
          <Icon name="i-chev-down" className={typeMenuOpen ? 'combo-chev up' : 'combo-chev'} />
        </div>
        {typeMenuOpen && (
          <div className="field-type-menu">
            {INPUT_FIELD_TYPES.map((t) => (
              <div key={t.value} className={`field-type-option${t.value === type ? ' sel' : ''}`} onClick={() => { setType(t.value); setTypeMenuOpen(false); }}>
                <Icon name={t.icon} /><span style={{ flex: 1 }}>{t.label}</span>
                <span className="chip line mono sm">{t.dataType}</span>
                {t.value === type && <Icon name="i-check" />}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="field">
        <label>Variable name</label>
        <input placeholder="e.g. topic" value={name} onChange={(e) => setName(e.target.value)} />
        {/* Shows the reference the rest of the flow will use, so the
            relationship between the two is visible while typing rather
            than something to infer. */}
        <div className="small muted" style={{ marginTop: 4 }}>
          {cleanName(name)
            ? <>Reference it downstream as <span className="mono">{`{{${cleanName(name)}}}`}</span></>
            : 'Just the name — the braces are only used when referencing it.'}
        </div>
      </div>
      <div className="row-split" style={{ padding: '10px 0', cursor: 'pointer' }} onClick={() => setRequired((v) => !v)}>
        <span className="small" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={`check-box${required ? ' on' : ''}`}>{required && <Icon name="i-check" />}</span> Required
        </span>
      </div>
      <div className="row-split" style={{ padding: '10px 0', cursor: 'pointer' }} onClick={() => setHiddenPrefilled((v) => !v)}>
        <span className="small" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={`check-box${hiddenPrefilled ? ' on' : ''}`}>{hiddenPrefilled && <Icon name="i-check" />}</span> Hidden &amp; Pre-Filled
        </span>
      </div>
      {hiddenPrefilled && (
        <div className="field">
          <label>Pre-filled value</label>
          <input value={defaultVal} onChange={(e) => setDefaultVal(e.target.value)} />
        </div>
      )}
    </Modal>
  );
}
