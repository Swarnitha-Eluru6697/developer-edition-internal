import { useEffect, useState } from 'react';
import { confirmAction } from '../components/ConfirmPanel';
import { api } from '../api';
import Modal from '../components/Modal';
import Icon from '../components/Icon';
import SearchSelect from '../components/SearchSelect';
import { useApp } from '../context/AppContext';
import type { DataSource } from '../types';

const MASK = '••••••••';

/** What each source actually needs in order to be browsed without a
 *  signed-in user. This is per-vendor, not per-auth-flavour: Microsoft
 *  exchanges a client ID + secret for an app-only token, while Google
 *  has no client-credentials grant at all and can only act on its own
 *  through a service-account key. */
const GUIDANCE: Record<string, string> = {
  SharePoint:
    'Register an app in Entra ID, give it the application permissions Sites.Read.All and Files.Read.All, '
    + 'grant admin consent, then paste its client ID and secret here. The client-credentials grant is used, '
    + 'so there is no consent screen to send anyone through — but an app with only delegated permissions '
    + 'will return 403 on the first browse.',
  OneDrive:
    'Register an app in Entra ID with the application permission Files.Read.All and admin consent. '
    + 'A personal OneDrive is addressed per user, so either name one user below, or additionally grant '
    + 'User.Read.All to list everyone. A team site\'s document library is a different thing — use the '
    + 'SharePoint source for that.',
  'Google Drive':
    'Paste a service-account JSON key. Google has no client-credentials grant for Drive, so a service '
    + 'account is the only way to read files without a signed-in user. It sees only what is shared with '
    + 'its own address unless you set up domain-wide delegation and name a user to impersonate below.',
  'Google Cloud Storage':
    'Paste a service-account JSON key with Storage Object Viewer on the buckets you want to read.',
  GitHub: 'Paste a personal access token (classic: repo scope, or fine-grained: Contents read).',
  Notion:
    'Paste an internal integration token, then share each page or database with the integration in Notion — '
    + 'an integration sees nothing until it is explicitly shared.',
};

export default function DsConfigModal({ sourceName }: { sourceName: string }) {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [source, setSource] = useState<DataSource | null>(null);
  const [busy, setBusy] = useState(false);

  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [subdomain, setSubdomain] = useState('');
  const [tenantId, setTenantId] = useState('');

  const [authName, setAuthName] = useState('');
  const [credentials, setCredentials] = useState('');
  const [buckets, setBuckets] = useState('');
  const [impersonate, setImpersonate] = useState('');
  const [oneDriveUser, setOneDriveUser] = useState('');
  const [whoCanUse, setWhoCanUse] = useState('All team members');

  // Prefill from what is stored so re-saving to change one field does
  // not blank the others. Secrets come back masked; sending the mask
  // back keeps the stored value untouched.
  useEffect(() => {
    api.listDataSources().then((all) => {
      const found = all.find((d) => d.name === sourceName) || null;
      setSource(found);
      const c = found?.config || {};
      setClientId(c.client_id || '');
      setClientSecret(c.client_secret || '');
      setSubdomain(c.sharepoint_subdomain || '');
      setTenantId(c.tenant_id || '');
      setAuthName(c.authorization_name || '');
      setCredentials(c.credentials || '');
      setBuckets(c.bucket_names || '');
      setImpersonate(c.impersonate_subject || '');
      setOneDriveUser(c.onedrive_user || '');
      setWhoCanUse(c.who_can_use || 'All team members');
    });
  }, [sourceName]);
  if (!source) return null;

  const isGoogle = source.name === 'Google Drive';
  const connected = source.status === 'connected';

  const done = (msg: string) => { toast(msg); notifyDataChanged(); closeModal(); };

  const disconnect = async () => {
    if (!(await confirmAction({ title: 'Remove credentials', confirmLabel: 'Remove',
      body: `Remove the stored credentials for ${source.name}?` }))) return;
    setBusy(true);
    api.disconnectDataSource(source.name)
      .then(() => done(`${source.name} disconnected`))
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  const saveOAuth = () => {
    if (isGoogle) {
      if (!credentials) { toast('A service-account JSON key is required'); return; }
    } else if (!clientId || !clientSecret) {
      toast('Client ID and Client Secret are required'); return;
    }
    setBusy(true);
    api.configureDataSourceOAuth(source.name, {
      client_id: clientId || undefined,
      client_secret: clientSecret || undefined,
      sharepoint_subdomain: subdomain || undefined,
      tenant_id: tenantId || undefined,
      credentials: credentials || undefined,
      impersonate_subject: impersonate || undefined,
      onedrive_user: oneDriveUser || undefined,
    })
      .then(() => done(`${source.name} credentials saved`))
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  const saveApiKey = () => {
    if (!credentials) { toast('Credentials are required'); return; }
    setBusy(true);
    api.configureDataSourceApiKey(source.name, {
      authorization_name: authName || undefined,
      credentials,
      bucket_names: buckets || undefined,
      who_can_use: whoCanUse,
      impersonate_subject: impersonate || undefined,
    })
      .then(() => done(`${source.name} configured`))
      .catch((e) => toast(e.message))
      .finally(() => setBusy(false));
  };

  const guidance = GUIDANCE[source.name];
  const footer = (save: () => void) => (
    <>
      {connected && <button className="icon-action danger" title="Disconnect" aria-label="Disconnect" onClick={disconnect} disabled={busy}><Icon name="i-trash" /></button>}
      <button className="btn ghost" onClick={closeModal} disabled={busy}>Cancel</button>
      <button className="btn" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
    </>
  );

  if (source.auth === 'oauth') {
    return (
      <Modal title={`Configure ${source.name}`} onClose={closeModal} footer={footer(saveOAuth)}>
        {guidance && (
          <div className="banner info small" style={{ alignItems: 'flex-start', padding: 12 }}>
            <div>{guidance}</div>
          </div>
        )}
        {isGoogle ? (
          <>
            <div className="field" style={{ marginTop: 14 }}>
              <label>Service-account JSON key *</label>
              <textarea
                rows={4}
                className="mono"
                placeholder='{"type": "service_account", "client_email": "...", "private_key": "..."}'
                value={credentials}
                onChange={(e) => setCredentials(e.target.value)}
              />
            </div>
            {/* The address to share WITH. A service account has its own,
                empty Drive, so a correctly configured source lists
                nothing until someone shares a folder with this address —
                and the address was previously only inside the key, which
                comes back masked. */}
            {source.service_account_email && (
              <div className="field">
                <label>This source signs in as</label>
                <div className="row-split" style={{ gap: 8 }}>
                  <span className="mono small" style={{ wordBreak: 'break-all' }}>
                    {source.service_account_email}
                  </span>
                  <button
                    className="btn sm ghost"
                    onClick={() => {
                      navigator.clipboard?.writeText(source.service_account_email!)
                        .then(() => toast('Address copied'))
                        .catch(() => toast(source.service_account_email!));
                    }}
                  >
                    Copy
                  </button>
                </div>
                <p className="hint">
                  Share a Drive folder with this address (Viewer is enough) and it appears in the
                  document browser. It cannot see anything else in your Drive.
                </p>
              </div>
            )}
            {/* Google Workspace domains commonly block sharing outside
                the organisation, and a service account is always
                outside it — the address ends in
                .iam.gserviceaccount.com, never your domain. Delegation
                is then the only route, and its Admin console screen
                asks for this NUMBER, not the address. */}
            {source.service_account_client_id && (
              <div className="field">
                <label>OAuth client ID (for domain-wide delegation)</label>
                <div className="row-split" style={{ gap: 8 }}>
                  <span className="mono small">{source.service_account_client_id}</span>
                  <button
                    className="btn sm ghost"
                    onClick={() => {
                      navigator.clipboard?.writeText(source.service_account_client_id!)
                        .then(() => toast('Client ID copied'))
                        .catch(() => toast(source.service_account_client_id!));
                    }}
                  >
                    Copy
                  </button>
                </div>
                <p className="hint">
                  If your Workspace refuses to share with the address above (&ldquo;outside your
                  organisation&rdquo;), use this instead: Admin console → Security → Access and data
                  control → API controls → Domain-wide delegation → Add new, with the scope{' '}
                  <span className="mono">https://www.googleapis.com/auth/drive.readonly</span>. Then
                  name the user to impersonate below.
                </p>
              </div>
            )}
            <div className="field">
              <label>Impersonate user (optional, needs domain-wide delegation)</label>
              <input placeholder="user@yourcompany.com" value={impersonate} onChange={(e) => setImpersonate(e.target.value)} />
            </div>
          </>
        ) : (
          <>
            <div className="field" style={{ marginTop: 14 }}>
              <label>Client ID *</label>
              <input placeholder="Please enter" value={clientId} onChange={(e) => setClientId(e.target.value)} />
            </div>
            <div className="field">
              <label>Client secret *</label>
              <input type="password" placeholder="Please enter" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
            </div>
            <div className="field">
              <label>{source.name === 'SharePoint' ? 'SharePoint subdomain *' : 'Tenant subdomain *'}</label>
              <input placeholder="mycompany" value={subdomain} onChange={(e) => setSubdomain(e.target.value)} />
            </div>
            <div className="field">
              <label>Tenant ID</label>
              <input placeholder="organizations or your tenant ID" value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
            </div>
            {source.name === 'OneDrive' && (
              <div className="field">
                <label>OneDrive user (optional)</label>
                <input
                  placeholder="you@yourcompany.com"
                  value={oneDriveUser}
                  onChange={(e) => setOneDriveUser(e.target.value)}
                />
                {/* Personal OneDrive is addressed per user. Naming one
                    goes straight to their files; leaving it blank lists
                    everyone, which needs User.Read.All over the whole
                    directory — a lot of access for one person's files. */}
                <p className="hint">
                  Goes straight to this person&apos;s files. Leave blank to browse everyone&apos;s,
                  which additionally requires the <span className="mono">User.Read.All</span>{' '}
                  application permission.
                </p>
              </div>
            )}
          </>
        )}
        {clientSecret === MASK || credentials === MASK
          ? <p className="hint">The stored secret is hidden. Leave it as-is to keep it, or type a new one to replace it.</p>
          : null}
        <p className="hint">Once saved, this source appears in the Source dropdown when adding documents to a Knowledge pipeline.</p>
      </Modal>
    );
  }

  return (
    <Modal title="API Key Authorization Configuration"
      sub="After configuring credentials, all members within the workspace can use this source when building Knowledge pipelines."
      onClose={closeModal}
      footer={footer(saveApiKey)}>
      {guidance && (
        <div className="banner info small" style={{ alignItems: 'flex-start', padding: 12, marginBottom: 12 }}>
          <div>{guidance}</div>
        </div>
      )}
      <div className="field"><label>Authorization name</label><input placeholder="Please enter" value={authName} onChange={(e) => setAuthName(e.target.value)} /></div>
      <div className="field">
        <label>Credentials *</label>
        {source.name === 'Google Cloud Storage' ? (
          <textarea
            rows={4}
            className="mono"
            placeholder='{"type": "service_account", "client_email": "...", "private_key": "..."}'
            value={credentials}
            onChange={(e) => setCredentials(e.target.value)}
          />
        ) : (
          <input type="password" placeholder={`Enter your ${source.name} token`} value={credentials} onChange={(e) => setCredentials(e.target.value)} />
        )}
      </div>
      {source.name === 'Google Cloud Storage' && (
        <div className="field"><label>Bucket names (optional, comma-separated)</label><input placeholder="my-bucket-1, my-bucket-2, my-bucket-3" value={buckets} onChange={(e) => setBuckets(e.target.value)} /></div>
      )}
      <div className="field">
        <label id="ds-who-can-use-label">Who can use</label>
        <SearchSelect labelledBy="ds-who-can-use-label" noun="audience" value={whoCanUse}
          options={[
            { value: 'All team members', label: 'All team members' },
            { value: 'Owners only', label: 'Owners only' },
          ]}
          onChange={(v) => setWhoCanUse(v)} />
      </div>
      {credentials === MASK && <p className="hint">The stored credential is hidden. Leave it as-is to keep it, or paste a new one to replace it.</p>}
      <p className="hint">Credentials are encrypted at rest with Fernet and are never returned to the browser in plaintext.</p>
    </Modal>
  );
}
