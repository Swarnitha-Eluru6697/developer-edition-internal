import { useEffect, useState } from 'react';
import { api } from '../api';
import Icon from './Icon';
import { Banner, Chip } from './ui';
import { confirmAction } from './ConfirmPanel';
import SearchSelect from './SearchSelect';
import { useApp } from '../context/AppContext';
import type { GitConnection, GitProvider, GitRepo } from '../types';

/** Provider facts the connect form needs: what the token is called there,
 *  where to make one, and which scopes the commit needs. `icon` is the brand
 *  mark, the same Tabler glyph the Workspace app's Git dialog shows. */
export const GIT_PROVIDERS: Record<GitProvider, {
  label: string; icon: string; tokenLabel: string; createUrl: string; scopes: string;
  serverLabel?: string; serverPlaceholder?: string;
}> = {
  github: {
    label: 'GitHub', icon: 'i-brand-github', tokenLabel: 'Personal access token',
    createUrl: 'https://github.com/settings/tokens',
    scopes: "Classic token: 'repo' scope. Fine-grained token: Contents → Read and write on the repositories you'll commit to.",
    serverLabel: 'GitHub Enterprise URL (optional)', serverPlaceholder: 'https://github.acme.com',
  },
  gitlab: {
    label: 'GitLab', icon: 'i-brand-gitlab', tokenLabel: 'Personal access token',
    createUrl: 'https://gitlab.com/-/user_settings/personal_access_tokens',
    scopes: "Scope 'api' (or 'read_api' + 'write_repository'), with at least Developer access to the project.",
    serverLabel: 'Self-managed GitLab URL (optional)', serverPlaceholder: 'https://gitlab.acme.com',
  },
  bitbucket: {
    label: 'Bitbucket', icon: 'i-brand-bitbucket', tokenLabel: 'App password, API token or access token',
    createUrl: 'https://bitbucket.org/account/settings/app-passwords/',
    scopes: 'Repositories: Read and Write. App passwords and API tokens need your username/email below; repository or workspace access tokens go alone.',
  },
  azure_devops: {
    label: 'Azure DevOps', icon: 'i-brand-azure', tokenLabel: 'Personal access token',
    createUrl: 'https://learn.microsoft.com/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate',
    scopes: "Scope Code → Read & write, created in the same organization as the URL below. Repositories are listed as project/repository.",
    serverLabel: 'Organization URL', serverPlaceholder: 'https://dev.azure.com/your-org',
  },
};

export function GitProviderBadge({ provider, size = 28 }: { provider: GitProvider; size?: number }) {
  return (
    <span className="git-badge" title={GIT_PROVIDERS[provider].label} style={{ width: size, height: size }} aria-hidden>
      <Icon name={GIT_PROVIDERS[provider].icon} />
    </span>
  );
}

function statusOf(c: GitConnection): { cls: string; label: string } {
  if (c.last_error) return { cls: 'red', label: 'failing' };
  if (c.last_checked_at) return { cls: 'green', label: 'connected' };
  return { cls: 'line', label: 'untested' };
}

/** Settings → Git integrations. */
export default function GitIntegrations() {
  const { toast } = useApp();
  const [rows, setRows] = useState<GitConnection[] | null>(null);
  const [canStore, setCanStore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ provider: GitProvider; conn?: GitConnection } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => api.listGitConnections()
    .then((r) => { setRows(r.connections); setCanStore(r.can_store_credentials); setError(null); })
    .catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const retest = (c: GitConnection) => {
    setBusy(c.id);
    api.retestGitConnection(c.id)
      .then((r) => { toast(r.ok ? `${c.name}: ${r.message}` : `${c.name} failed — ${r.message}`); load(); })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(null));
  };
  const remove = async (c: GitConnection) => {
    const ok = await confirmAction({
      title: `Disconnect ${c.name}?`,
      body: 'The stored token is deleted. Agents already committed stay in the repository.',
      confirmLabel: 'Disconnect',
    });
    if (!ok) return;
    api.deleteGitConnection(c.id).then(() => { toast(`Disconnected ${c.name}`); load(); }).catch((e) => toast(e.message));
  };

  if (editing) {
    return <GitConnectionForm key={editing.conn?.id || editing.provider} provider={editing.provider} conn={editing.conn}
      onDone={(saved, next) => { if (saved) load(); setEditing(next ? { provider: next.provider, conn: next } : null); }} />;
  }

  return (
    <div role="tabpanel" aria-label="Git integrations">
      {!canStore && (
        <Banner kind="warn">SECRETS_ENCRYPTION_KEY isn't set on the server, so tokens can't be stored. Set it and restart the API to connect a provider.</Banner>
      )}
      <div className="card">
        <div className="card-h"><h3><Icon name="i-git" /> Connect a provider</h3></div>
        <div className="card-b">
          <p className="small muted" style={{ margin: '0 0 12px' }}>
            Connect with a personal access token. Exported agents are committed as YAML to the repository you choose —
            and, if you switch it on, every time an agent is downloaded.
          </p>
          <div className="git-provider-grid">
            {(Object.keys(GIT_PROVIDERS) as GitProvider[]).map((p) => (
              <button key={p} type="button" className="git-provider" disabled={!canStore} onClick={() => setEditing({ provider: p })}>
                <GitProviderBadge provider={p} size={34} />
                <span><b>{GIT_PROVIDERS[p].label}</b><span className="small muted">Connect with a token</span></span>
                <Icon name="i-plus" />
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h"><h3><Icon name="i-link" /> Connections</h3>{rows && <span className="small muted">{rows.length}</span>}</div>
        <div className="card-b">
          {error && <div className="small" style={{ color: 'var(--danger)' }}>Couldn't load Git connections: {error}</div>}
          {!error && rows === null && <div className="small muted">Loading…</div>}
          {rows?.length === 0 && <div className="small muted">No providers connected yet.</div>}
          {rows?.map((c) => {
            const st = statusOf(c);
            return (
              <div className="git-conn" key={c.id}>
                <GitProviderBadge provider={c.provider} />
                <div className="git-conn-main">
                  <div><b>{c.name}</b> <Chip cls={st.cls}>{st.label}</Chip>
                    {c.commit_on_download && <Chip cls="teal">commits on download</Chip>}</div>
                  <div className="small muted">
                    {c.provider_label}{c.base_url ? ` · ${c.base_url}` : ''} · {c.account || 'unknown account'}
                    {c.default_repo ? <> · <span className="mono">{c.default_repo}@{c.default_branch || '—'}/{c.default_folder}</span></> : ' · no default repository'}
                  </div>
                  {c.last_error && <div className="small" style={{ color: 'var(--danger)', marginTop: 3 }}>{c.last_error}</div>}
                </div>
                <div className="git-conn-actions">
                  <button className="btn sm ghost" onClick={() => retest(c)} disabled={busy === c.id}><Icon name="i-test" /> {busy === c.id ? 'Testing…' : 'Test'}</button>
                  <button className="icon-action" title="Edit" aria-label={`Edit ${c.name}`} onClick={() => setEditing({ provider: c.provider, conn: c })}><Icon name="i-pencil" /></button>
                  <button className="icon-action danger" title="Disconnect" aria-label={`Disconnect ${c.name}`} onClick={() => remove(c)}><Icon name="i-trash" /></button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** Add or edit one connection. The token is tested before anything is saved;
 *  repository and branch pickers load once a saved connection exists. */
function GitConnectionForm({ provider, conn, onDone }: {
  provider: GitProvider; conn?: GitConnection;
  /** next: a connection just created — the form continues to its repository step. */
  onDone: (saved: boolean, next?: GitConnection) => void;
}) {
  const { toast } = useApp();
  const meta = GIT_PROVIDERS[provider];
  const [name, setName] = useState(conn?.name || '');
  const [baseUrl, setBaseUrl] = useState(conn?.base_url || '');
  const [username, setUsername] = useState(conn?.username || '');
  const [token, setToken] = useState('');
  const [repo, setRepo] = useState(conn?.default_repo || '');
  const [branch, setBranch] = useState(conn?.default_branch || '');
  const [folder, setFolder] = useState(conn?.default_folder || 'agents');
  const [onDownload, setOnDownload] = useState(!!conn?.commit_on_download);
  const [repos, setRepos] = useState<GitRepo[] | null>(null);
  const [branches, setBranches] = useState<string[] | null>(null);
  const [check, setCheck] = useState<{ ok: boolean; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!conn) return;
    api.listGitRepos(conn.id).then(setRepos).catch((e) => setErr(e.message));
  }, [conn?.id]);
  useEffect(() => {
    if (!conn || !repo) { setBranches(null); return; }
    api.listGitBranches(conn.id, repo).then(setBranches).catch(() => setBranches([]));
  }, [conn?.id, repo]);

  const creds = () => ({ provider, token: token.trim(), base_url: baseUrl.trim() || undefined,
    username: provider === 'bitbucket' ? username.trim() || undefined : undefined });

  const test = () => {
    if (!token.trim()) { setCheck({ ok: false, message: `Enter the ${meta.tokenLabel.toLowerCase()} to test it.` }); return; }
    setTesting(true); setCheck(null);
    api.testGitCredentials(creds()).then(setCheck).catch((e) => setCheck({ ok: false, message: e.message })).finally(() => setTesting(false));
  };

  const save = () => {
    setErr(null);
    if (!conn && !token.trim()) { setErr(`Enter the ${meta.tokenLabel.toLowerCase()}.`); return; }
    if (onDownload && !(repo && branch)) { setErr('Choose a default repository and branch to commit agents on download.'); return; }
    setSaving(true);
    const done = (c: GitConnection) => {
      toast(conn ? `Saved ${c.name}` : `Connected ${c.name} as ${c.account} — now choose where agents are committed`);
      onDone(true, conn ? undefined : c);
    };
    const p = conn
      ? api.updateGitConnection(conn.id, {
          name, base_url: baseUrl, ...(provider === 'bitbucket' ? { username } : {}),
          ...(token.trim() ? { token: token.trim() } : {}),
          default_repo: repo, default_branch: branch, default_folder: folder, commit_on_download: onDownload,
        })
      : api.createGitConnection({ ...creds(), name: name || undefined, default_folder: folder });
    p.then(done).catch((e) => setErr(e.message)).finally(() => setSaving(false));
  };

  const repoOptions = (repos || []).map((r) => ({ value: r.full_name, label: r.full_name, hint: r.private ? 'private' : 'public' }));
  const pickRepo = (v: string) => {
    setRepo(v);
    const r = repos?.find((x) => x.full_name === v);
    setBranch(r?.default_branch || '');
  };

  return (
    <div className="card" role="tabpanel" aria-label={`${conn ? 'Edit' : 'Connect'} ${meta.label}`}>
      <div className="card-h">
        <h3><GitProviderBadge provider={provider} size={22} /> {conn ? `Edit ${conn.name}` : `Connect ${meta.label}`}</h3>
        <button className="btn sm ghost" onClick={() => onDone(false)}>Back to connections</button>
      </div>
      <div className="card-b">
        <div className="field"><label>Connection name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={`${meta.label} · team agents`} />
        </div>
        {meta.serverLabel && (
          <div className="field"><label>{meta.serverLabel}</label>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={meta.serverPlaceholder} />
            <div className="hint">Leave empty for {provider === 'github' ? 'github.com' : 'gitlab.com'}.</div>
          </div>
        )}
        {provider === 'bitbucket' && (
          <div className="field"><label>Username or Atlassian email</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Only for app passwords / API tokens" autoComplete="off" />
          </div>
        )}
        <div className="field"><label>{meta.tokenLabel}</label>
          <input type="password" value={token} onChange={(e) => { setToken(e.target.value); setCheck(null); }} autoComplete="new-password"
            placeholder={conn ? 'Leave blank to keep the stored token' : 'Paste the token'} />
          <div className="hint">{meta.scopes} <a href={meta.createUrl} target="_blank" rel="noreferrer" className="link">Create one ↗</a></div>
        </div>
        {!conn && (
          <div className="row-split" style={{ justifyContent: 'flex-start', gap: 10, marginBottom: 12 }}>
            <button className="btn sm ghost" onClick={test} disabled={testing}><Icon name="i-test" /> {testing ? 'Testing…' : 'Test token'}</button>
            {check && <span className="small" style={{ color: check.ok ? 'var(--green-d)' : 'var(--danger)' }}>{check.message}</span>}
          </div>
        )}

        {conn ? (
          <>
            <div className="lbl" style={{ margin: '6px 0 8px' }}>Where agents are committed</div>
            <div className="grid g2">
              <div className="field"><label>Default repository</label>
                {repos === null && !err ? <div className="small muted">Loading repositories…</div> : (
                  <SearchSelect value={repo} options={repoOptions} onChange={pickRepo} placeholder="Choose a repository" noun="repository" />
                )}
              </div>
              <div className="field"><label>Default branch</label>
                <SearchSelect value={branch} options={(branches || []).map((b) => ({ value: b, label: b }))}
                  onChange={setBranch} placeholder={repo ? 'Choose a branch' : 'Choose a repository first'} noun="branch" />
              </div>
            </div>
            <div className="field"><label>Folder</label>
              <input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="agents" />
              <div className="hint">Files are written as <span className="mono">{(folder || 'agents').replace(/^\/+|\/+$/g, '')}/&lt;agent-name&gt;.yaml</span>.</div>
            </div>
            <label className="git-toggle">
              <input type="checkbox" checked={onDownload} onChange={(e) => setOnDownload(e.target.checked)} />
              <span><b>Commit agents here whenever they're downloaded</b>
                <span className="small muted">Downloading an agent's YAML also commits it to {repo || 'the default repository'}{branch ? `@${branch}` : ''}.</span></span>
            </label>
          </>
        ) : (
          <div className="small muted" style={{ marginBottom: 8 }}>After connecting, choose the default repository, branch and folder.</div>
        )}

        {err && <div className="small" style={{ color: 'var(--danger)', margin: '10px 0' }}>{err}</div>}
        <div className="row-split" style={{ justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <button className="btn ghost" onClick={() => onDone(false)} disabled={saving}>Cancel</button>
          <button className="btn" onClick={save} disabled={saving}>{saving ? (conn ? 'Saving…' : 'Checking token…') : conn ? 'Save' : 'Connect'}</button>
        </div>
      </div>
    </div>
  );
}
