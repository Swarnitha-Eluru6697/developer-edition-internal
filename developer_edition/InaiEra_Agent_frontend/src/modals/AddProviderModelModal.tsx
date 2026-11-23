import { useState } from 'react';
import { api } from '../api';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';

// Vendor ids differ per provider, and getting this wrong is the whole
// failure mode: the id is what the SDK is called with, so a typo means a
// 404 from the vendor at run time rather than a validation error here.
const ID_HINTS: Record<string, string> = {
  Anthropic: 'e.g. claude-sonnet-4-6',
  OpenAI: 'e.g. gpt-4.1-mini',
};

export default function AddProviderModelModal({ provider }: { provider: string }) {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [name, setName] = useState('');
  const [apiModelId, setApiModelId] = useState('');
  const [inPrice, setInPrice] = useState('');
  const [outPrice, setOutPrice] = useState('');
  const [saving, setSaving] = useState(false);

  const save = () => {
    if (!name.trim() || !apiModelId.trim()) { toast('Both a display name and a model ID are required'); return; }
    setSaving(true);
    api.addProviderModel(provider, {
      name: name.trim(),
      api_model_id: apiModelId.trim(),
      ...(inPrice ? { input_price_per_mtok: Number(inPrice) } : {}),
      ...(outPrice ? { output_price_per_mtok: Number(outPrice) } : {}),
    })
      .then(() => { toast(`${name.trim()} added to ${provider}`); notifyDataChanged(); closeModal(); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal title={`Add a model to ${provider}`}
      sub="Registers a model so builder LLM nodes can select it and agent runs can route to it."
      onClose={closeModal}
      footer={<>
        <button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
        <button className="btn" onClick={save} disabled={saving}>{saving ? 'Adding…' : 'Add model'}</button>
      </>}>
      <div className="field">
        <label>Display name *</label>
        <input placeholder="Shown in the builder, e.g. Claude Sonnet 4.6" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label>Model ID *</label>
        <input className="mono" placeholder={ID_HINTS[provider] || 'The provider’s own model identifier'}
          value={apiModelId} onChange={(e) => setApiModelId(e.target.value)} />
        <div className="small muted" style={{ marginTop: 4 }}>
          The provider&apos;s exact identifier — this is what gets called. A wrong value fails at
          run time with the vendor&apos;s own 404, not here.
        </div>
      </div>
      <div className="row-split" style={{ gap: 10 }}>
        <div className="field" style={{ flex: 1 }}>
          <label>Input price</label>
          <input placeholder="USD per 1M tokens" value={inPrice} onChange={(e) => setInPrice(e.target.value)} />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>Output price</label>
          <input placeholder="USD per 1M tokens" value={outPrice} onChange={(e) => setOutPrice(e.target.value)} />
        </div>
      </div>
      <p className="hint">
        Prices are optional and only drive cost reporting. Left blank, runs on this model report
        $0.00 rather than an estimate.
      </p>
    </Modal>
  );
}
