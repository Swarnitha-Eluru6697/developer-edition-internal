import { useState } from 'react';
import { api } from '../api';
import Modal from '../components/Modal';
import SearchSelect from '../components/SearchSelect';
import { useApp } from '../context/AppContext';

// Per-provider hints. The previous single set was OpenAI's for every
// provider — configuring Anthropic showed "e.g. https://api.openai.com"
// as the base-URL example, and entering it pointed the Anthropic client
// at OpenAI so every call 404'd. The API protocol selector is an
// OpenAI-only concept and is hidden elsewhere for the same reason.
const PROVIDER_HINTS: Record<string, { host: string; model: string; protocol: boolean }> = {
  OpenAI:      { host: 'https://api.openai.com/v1', model: 'gpt-4o-mini',        protocol: true },
  Anthropic:   { host: 'https://api.anthropic.com', model: 'claude-haiku-4-5',   protocol: false },
  DeepSeek:    { host: 'https://api.deepseek.com',  model: 'deepseek-chat',      protocol: false },
  Cohere:      { host: 'https://api.cohere.com',    model: 'command-r',          protocol: false },
  'Azure OpenAI': { host: 'https://<resource>.openai.azure.com', model: 'gpt-4o', protocol: true },
};

export default function ApiKeyConfigModal({ provider }: { provider: string }) {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [authName, setAuthName] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [org, setOrg] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [validateModel, setValidateModel] = useState('');
  const [protocol, setProtocol] = useState('Responses API');
  const hint = PROVIDER_HINTS[provider];

  const save = () => {
    if (!apiKey) { toast('API key is required'); return; }
    api.configureModelProvider(provider, {
      authorization_name: authName || undefined, api_key: apiKey, organization: org || undefined,
      api_base_url: baseUrl || undefined, validate_model: validateModel || undefined, api_protocol: protocol,
    }).then(() => {
      toast(`${provider} API key saved`);
      // Tell the Integrations page to refetch, or it keeps showing this
      // provider as unconfigured and the save looks like it failed.
      notifyDataChanged();
      closeModal();
    }).catch((e) => toast(e.message));
  };

  return (
    <Modal title="API Key Authorization Configuration"
      sub="After configuring credentials, all members within the workspace can use this model when orchestrating applications."
      onClose={closeModal}
      footer={<><button className="btn ghost" onClick={closeModal}>Cancel</button><button className="btn" onClick={save}>Save</button></>}>
      <div className="field"><label>Authorization name</label><input placeholder="Please enter" value={authName} onChange={(e) => setAuthName(e.target.value)} /></div>
      <div className="field"><label>API key *</label><input type="password" placeholder="Enter your API Key" value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>
      <div className="field">
        <label>Organization</label>
        <input
          placeholder={provider === 'OpenAI' ? 'Optional, e.g. org-AbC123… (an ID, not a name)' : 'Optional'}
          value={org}
          onChange={(e) => setOrg(e.target.value)}
        />
        {provider === 'OpenAI' && (
          <div className="small muted" style={{ marginTop: 4 }}>
            Must be the organization ID from your OpenAI account. A company name here is rejected
            by OpenAI with a 401.
          </div>
        )}
      </div>
      <div className="field">
        <label>API base URL</label>
        <input
          placeholder={hint ? `Leave blank to use ${hint.host}` : 'Leave blank to use the provider default'}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
        />
        <div className="small muted" style={{ marginTop: 4 }}>
          Optional. Only set this for a proxy or self-hosted endpoint — the wrong value here
          points {provider} at another vendor and every call fails.
        </div>
      </div>
      <div className="field"><label>Validate model</label><input placeholder={`Model name used to validate credentials${hint ? `, e.g. ${hint.model}` : ''}`} value={validateModel} onChange={(e) => setValidateModel(e.target.value)} /></div>
      {(!hint || hint.protocol) && (
        <div className="field">
          <label id="apikey-protocol-label">API protocol</label>
          <SearchSelect labelledBy="apikey-protocol-label" noun="protocol" value={protocol}
            options={[
              { value: 'Responses API', label: 'Responses API' },
              { value: 'Chat Completions API', label: 'Chat Completions API' },
            ]}
            onChange={(v) => setProtocol(v)} />
        </div>
      )}
      <a href="#" onClick={(e) => e.preventDefault()} className="small" style={{ color: 'var(--teal-d)', fontWeight: 600 }}>
        Get your API key from {provider} ↗
      </a>
      <p className="hint" style={{ marginTop: 14 }}>
        Encrypted at rest. Once saved, {provider} is registered into this workspace's UCS <span className="mono">Model</span> catalog (§7.4).
      </p>
    </Modal>
  );
}
