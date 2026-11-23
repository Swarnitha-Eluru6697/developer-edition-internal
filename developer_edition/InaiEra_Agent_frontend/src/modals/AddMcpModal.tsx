import { useState } from 'react';
import { api } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';

export default function AddMcpModal() {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [tab, setTab] = useState<'auth' | 'headers' | 'config'>('auth');
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [identifier, setIdentifier] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [dynamicReg, setDynamicReg] = useState(true);

  const submit = () => {
    if (!name || !identifier || !url) { toast('Server URL, name, and identifier are required'); return; }
    api.addMcpServer({ name, identifier, url, client_id: clientId, client_secret: clientSecret, use_dynamic_registration: dynamicReg })
      .then(() => { toast('MCP server added and authorized'); notifyDataChanged(); closeModal(); })
      .catch((e) => toast(e.message));
  };

  return (
    <Modal title="Add MCP Server (HTTP)" onClose={closeModal} footer={
      <><button className="btn ghost" onClick={closeModal}>Cancel</button><button className="btn" onClick={submit}>Add &amp; authorize</button></>
    }>
      <div className="field"><label>Server URL</label><input placeholder="URL to server endpoint" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
      <div className="row-split" style={{ alignItems: 'flex-end', gap: 10 }}>
        <div className="field" style={{ flex: 1, marginBottom: 0 }}><label>Name &amp; icon</label>
          <input placeholder="Name your MCP server" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div style={{ width: 44, height: 44, borderRadius: 10, background: 'var(--purple)', display: 'grid', placeItems: 'center', color: '#fff', flex: 'none' }}>
          <Icon name="i-plug" />
        </div>
      </div>
      <div className="field" style={{ marginTop: 14 }}>
        <label>Server identifier</label>
        <div className="hint" style={{ margin: '0 0 6px' }}>Unique identifier for the MCP server within the workspace. Lowercase letters, numbers, underscores, and hyphens only. Up to 24 characters.</div>
        <input placeholder="Unique identifier, e.g., my-mcp-server" value={identifier} onChange={(e) => setIdentifier(e.target.value.slice(0, 24))} />
      </div>
      <div className="tabs" style={{ marginTop: 16, marginBottom: 14 }}>
        <div className={`tab${tab === 'auth' ? ' on' : ''}`} onClick={() => setTab('auth')}>Authentication</div>
        <div className={`tab${tab === 'headers' ? ' on' : ''}`} onClick={() => setTab('headers')}>Headers</div>
        <div className={`tab${tab === 'config' ? ' on' : ''}`} onClick={() => setTab('config')}>Configurations</div>
      </div>
      {tab === 'auth' && (
        <>
          <div className="row-split" style={{ padding: '6px 0' }}>
            <span className="small">Use Dynamic Client Registration</span>
            <div className={`tog${dynamicReg ? ' on' : ''}`} onClick={() => setDynamicReg(!dynamicReg)} />
          </div>
          <div className="field"><label>Client ID</label><input placeholder="Client ID" value={clientId} onChange={(e) => setClientId(e.target.value)} disabled={dynamicReg} /></div>
          <div className="field"><label>Client secret</label><input placeholder="Client Secret" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} disabled={dynamicReg} /></div>
        </>
      )}
      {tab === 'headers' && (
        <>
          <div className="mini-table"><div className="r"><div className="c">Key</div><div className="c">Value</div></div></div>
          <button className="btn sm ghost" style={{ marginTop: 8 }}><Icon name="i-plus" /> Add header</button>
        </>
      )}
      {tab === 'config' && (
        <>
          <div className="field"><label>Request timeout (s)</label><input defaultValue={30} /></div>
          <div className="row-split" style={{ padding: '6px 0' }}><span className="small">Verify SSL certificate</span><div className="tog on" /></div>
        </>
      )}
    </Modal>
  );
}
