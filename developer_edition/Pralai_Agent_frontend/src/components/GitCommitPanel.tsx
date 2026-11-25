import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import Icon from './Icon';
import SearchSelect from './SearchSelect';
import { GitProviderBadge } from './GitIntegrations';
import { useApp } from '../context/AppContext';
import type { Agent, GitCommitResult, GitConnection, GitRepo } from '../types';

const slug = (name: string) => (name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'agent');

/** Download the agent's YAML; then commit it to every connection set to
 *  "commit on download". Returns a one-line summary for a toast. */
export async function downloadAgentYaml(agent: Agent): Promise<string> {
  const { text, filename } = await api.exportAgentYaml(agent.id);
  const url = URL.createObjectURL(new Blob([text], { type: 'application/yaml' }));
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  const auto = await api.listGitConnections().then((r) => r.connections.filter((c) => c.commit_on_download)).catch(() => []);
  if (!auto.length) return `Downloaded ${filename}`;
  const results = await Promise.allSettled(auto.map((c) => api.commitAgentToGit(agent.id, { connection_id: c.id })));
  const parts = results.map((r, i) => r.status === 'fulfilled'
    ? `committed to ${r.value.repo}@${r.value.branch}`
    : `${auto[i].name}: ${(r.reason as Error)?.message || 'commit failed'}`);
  return `Downloaded ${filename} · ${parts.join(' · ')}`;
}

/** Commit an agent's YAML to a connected repository, choosing where. */
export default function GitCommitPanel({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const { toast } = useApp();
  const [conns, setConns] = useState<GitConnection[] | null>(null);
  const [connId, setConnId] = useState('');
  const [repos, setRepos] = useState<GitRepo[] | null>(null);
  const [branches, setBranches] = useState<string[] | null>(null);
  const [repo, setRepo] = useState('');
  const [branch, setBranch] = useState('');
  const [path, setPath] = useState('');
  const [message, setMessage] = useState(`Export agent '${agent.name}' from Pralai`);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<GitCommitResult | null>(null);
  const conn = useMemo(() => conns?.find((c) => c.id === connId) || null, [conns, connId]);

  useEffect(() => {
    api.listGitConnections().then((r) => {
      setConns(r.connections);
      const first = r.connections.find((c) => c.default_repo) || r.connections[0];
      if (first) setConnId(first.id);
    }).catch((e) => setErr(e.message));
  }, []);
  useEffect(() => {
    if (!conn) return;
    setRepo(conn.default_repo || ''); setBranch(conn.default_branch || '');
    setPath(`${conn.default_folder || 'agents'}/${slug(agent.name)}.yaml`);
    setRepos(null); setResult(null); setErr(null);
    api.listGitRepos(conn.id).then(setRepos).catch((e) => { setRepos([]); setErr(e.message); });
  }, [conn?.id]);
  useEffect(() => {
    if (!conn || !repo) { setBranches(null); return; }
    api.listGitBranches(conn.id, repo).then(setBranches).catch(() => setBranches([]));
  }, [conn?.id, repo]);

  const commit = () => {
    if (!conn) return;
    setBusy(true); setErr(null); setResult(null);
    api.commitAgentToGit(agent.id, { connection_id: conn.id, repo, branch, path, message })
      .then((r) => { setResult(r); toast(`${r.created ? 'Added' : 'Updated'} ${r.path} on ${r.repo}@${r.branch}`); })
      .catch((e) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="card pad" style={{ marginBottom: 14 }}>
      <div className="row-split" style={{ marginBottom: 10 }}>
        <b className="small"><Icon name="i-git" /> Commit YAML to Git</b>
        <button className="iconbtn" aria-label="Close" onClick={onClose}><Icon name="i-x" /></button>
      </div>
      {conns === null && !err && <div className="small muted">Loading connections…</div>}
      {conns?.length === 0 && (
        <div className="small muted">No Git provider connected. <Link className="link" to="/settings?tab=git">Connect GitHub, GitLab or Bitbucket in Settings</Link>.</div>
      )}
      {!!conns?.length && (
        <>
          <div className="field"><label id="git-commit-conn-label">Connection</label>
            <SearchSelect labelledBy="git-commit-conn-label" noun="connection" value={connId}
              options={conns.map((c) => ({ value: c.id, label: `${c.name} — ${c.provider_label} · ${c.account || '?'}` }))}
              onChange={(v) => setConnId(v)} />
          </div>
          {conn && (
            <div className="row-split small muted" style={{ justifyContent: 'flex-start', gap: 8, margin: '-4px 0 10px' }}>
              <GitProviderBadge provider={conn.provider} size={18} /> {conn.web_url}
            </div>
          )}
          <div className="field"><label>Repository</label>
            {repos === null ? <div className="small muted">Loading repositories…</div> : (
              <SearchSelect value={repo} noun="repository" placeholder="Choose a repository"
                options={repos.map((r) => ({ value: r.full_name, label: r.full_name, hint: r.private ? 'private' : 'public' }))}
                onChange={(v) => { setRepo(v); setBranch(repos.find((r) => r.full_name === v)?.default_branch || ''); }} />
            )}
          </div>
          <div className="field"><label>Branch</label>
            <SearchSelect value={branch} noun="branch" placeholder={repo ? 'Choose a branch' : 'Choose a repository first'}
              options={(branches || []).map((b) => ({ value: b, label: b }))} onChange={setBranch} />
          </div>
          <div className="field"><label>File path</label><input className="mono" value={path} onChange={(e) => setPath(e.target.value)} /></div>
          <div className="field"><label>Commit message</label><input value={message} onChange={(e) => setMessage(e.target.value)} /></div>
          {err && <div className="small" style={{ color: 'var(--danger)', marginBottom: 8 }}>{err}</div>}
          {result && (
            <div className="small" style={{ color: 'var(--green-d)', marginBottom: 8 }}>
              ✓ {result.created ? 'Added' : 'Updated'} <span className="mono">{result.path}</span> on {result.repo}@{result.branch}
              {result.commit_sha && <> · <span className="mono">{result.commit_sha.slice(0, 8)}</span></>}
              {result.file_url && <> · <a className="link" href={result.file_url} target="_blank" rel="noreferrer">view file ↗</a></>}
            </div>
          )}
          <button className="btn sm" onClick={commit} disabled={busy || !repo || !branch || !path.trim()}>
            <Icon name="i-git" /> {busy ? 'Committing…' : 'Commit'}
          </button>
        </>
      )}
    </div>
  );
}
