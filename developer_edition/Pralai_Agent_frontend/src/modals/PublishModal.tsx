import { useRef, useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import QRCode from 'qrcode';
import { api, BASE } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import SearchSelect from '../components/SearchSelect';
import { Banner } from '../components/ui';
import { useApp } from '../context/AppContext';
import { isChatLikeAgent } from '../components/RunnerUI';
import GitCommitPanel, { downloadAgentYaml } from '../components/GitCommitPanel';
import TemplateDetailsModal from './TemplateDetailsModal';

// Built with VITE_UCS_ENABLED=false (Developer Edition, no Unified Catalog): there is no catalog to grant access in,
// so the Access step is not shown.
const UCS_ENABLED = (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_UCS_ENABLED !== 'false';
import type { Agent, AgentRoleDef, AgentVersion, WebAppSettings } from '../types';

function StatusBadge({ on }: { on: boolean }) {
  return on
    ? <span className="chip green">In service</span>
    : <span className="chip line">Disabled</span>;
}

function relativeTime(iso: string): string {
  const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`).getTime();
  const diffMs = Date.now() - then;
  const sec = Math.max(0, Math.floor(diffMs / 1000));
  if (sec < 10) return 'a few seconds ago';
  if (sec < 60) return `${sec} seconds ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} minute${min === 1 ? '' : 's'} ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? '' : 's'} ago`;
  const day = Math.floor(hr / 24);
  return `${day} day${day === 1 ? '' : 's'} ago`;
}

function WebAppSettingsModal({ agent, onClose, onSaved }: { agent: Agent; onClose: () => void; onSaved: (a: Agent) => void }) {
  const { toast } = useApp();
  const isChatApp = isChatLikeAgent(agent);
  const existing = agent.features?.webapp_settings || {};
  const [ws, setWs] = useState<WebAppSettings>({ theme: 'light', color: '#155EEF', ...existing });
  const [saving, setSaving] = useState(false);
  const set = (patch: Partial<WebAppSettings>) => setWs((prev) => ({ ...prev, ...patch }));

  const descOver = (ws.description || '').length > 160;
  const phOver = (ws.input_placeholder || '').length > 64;

  const save = () => {
    if (descOver || phOver) { toast('Fix the fields over their character limit first'); return; }
    setSaving(true);
    const features = { ...(agent.features || {}), webapp_settings: ws };
    api.setAgentFeatures(agent.id, features)
      .then((a) => { onSaved(a); toast('Web app settings saved — live immediately, no publish needed'); onClose(); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal title="Web App Settings" onClose={onClose} footer={
      <>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn" disabled={saving || descOver || phOver} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
      </>
    }>
      <p className="muted small" style={{ margin: '0 0 16px' }}>
        Branding, visual design, and legal pages for the published web app — applies to every place it's deployed as soon as you save.
      </p>
      <div className="small muted" style={{ marginBottom: 14, textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>App Identity</div>
      <div className="field"><label>Name</label><input placeholder={agent.name} value={ws.display_name || ''} onChange={(e) => set({ display_name: e.target.value })} /></div>
      <div className="field">
        <label>Description {(ws.description || '').length > 0 && <span className="small" style={{ color: descOver ? 'var(--red)' : undefined }}>({(ws.description || '').length}/160)</span>}</label>
        <textarea rows={2} placeholder="What this app does, shown on its landing page" value={ws.description || ''} onChange={(e) => set({ description: e.target.value })} />
      </div>
      <div className="field"><label>Icon</label><input placeholder="e.g. 🎧 or an image URL" value={ws.icon || ''} style={{ maxWidth: 220 }} onChange={(e) => set({ icon: e.target.value })} /></div>

      <div className="small muted" style={{ margin: '18px 0 14px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Visual Design</div>
      <div className="row-split" style={{ gap: 10, alignItems: 'flex-end' }}>
        <div className="field" style={{ flex: 1, margin: 0 }}>
          <label id="publish-theme-label">Theme</label>
          <SearchSelect labelledBy="publish-theme-label" noun="theme" value={ws.theme === 'dark' ? 'dark' : 'light'}
            options={[{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }]}
            onChange={(v) => set({ theme: v as 'light' | 'dark' })} />
        </div>
        <div className="field" style={{ flex: 1, margin: 0 }}>
          <label>Accent color</label>
          <div className="row-split" style={{ gap: 8 }}>
            <input type="color" value={/^#[0-9a-fA-F]{6}$/.test(ws.color || '') ? ws.color : '#155EEF'} style={{ width: 40, padding: 2 }} onChange={(e) => set({ color: e.target.value })} />
            <input value={ws.color || '#155EEF'} style={{ flex: 1 }} onChange={(e) => set({ color: e.target.value })} />
          </div>
        </div>
      </div>

      {isChatApp && (
        <>
          <div className="small muted" style={{ margin: '18px 0 14px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Input Placeholder</div>
          <div className="field">
            <label>Chat input hint text {(ws.input_placeholder || '').length > 0 && <span className="small" style={{ color: phOver ? 'var(--red)' : undefined }}>({(ws.input_placeholder || '').length}/64)</span>}</label>
            <input placeholder="Type a message…" value={ws.input_placeholder || ''} onChange={(e) => set({ input_placeholder: e.target.value })} />
          </div>
        </>
      )}

      <div className="small muted" style={{ margin: '18px 0 14px', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '.03em' }}>Legal Pages</div>
      <div className="field"><label>Copyright</label><input placeholder="© 2026 Your Company" value={ws.copyright || ''} onChange={(e) => set({ copyright: e.target.value })} /></div>
      <div className="row-split" style={{ gap: 10 }}>
        <div className="field" style={{ flex: 1 }}><label>Privacy Policy URL</label><input placeholder="https://…" value={ws.privacy_policy_url || ''} onChange={(e) => set({ privacy_policy_url: e.target.value })} /></div>
        <div className="field" style={{ flex: 1 }}><label>Terms of Service URL</label><input placeholder="https://…" value={ws.terms_of_service_url || ''} onChange={(e) => set({ terms_of_service_url: e.target.value })} /></div>
      </div>
    </Modal>
  );
}

export default function PublishModal({ agentId }: { agentId: string }) {
  const { closeModal, toast, endUserIdentity, notifyDataChanged } = useApp();
  const navigate = useNavigate();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [publishResult, setPublishResult] = useState<{ version: string; entitlement_plan: any; audited_as: string } | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [regeneratingMcp, setRegeneratingMcp] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  // §0.101, GAP_REPORT — the real, PLAINTEXT MCP url is only ever
  // available just-in-time from an actual enable/regenerate response
  // — agent.mcp_token_encrypted (re-fetched below) is always masked,
  // since it's the real, encrypted-at-rest credential, never meant to
  // be read back through a plain GET.
  const [mcpUrl, setMcpUrl] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [showEmbed, setShowEmbed] = useState(false);
  const [embedMode, setEmbedMode] = useState<'iframe' | 'bubble'>('iframe');
  const [wsOpen, setWsOpen] = useState(false);
  const [wsSaving, setWsSaving] = useState(false);
  const [editingInfo, setEditingInfo] = useState(false);
  const [gitOpen, setGitOpen] = useState(false);
  const [yamlBusy, setYamlBusy] = useState(false);
  const [descDraft, setDescDraft] = useState('');
  const importInputRef = useRef<HTMLInputElement>(null);
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [restoringId, setRestoringId] = useState<number | null>(null);
  const [ucsSyncing, setUcsSyncing] = useState(false);
  const [grantGroup, setGrantGroup] = useState('');
  const [grantRole, setGrantRole] = useState('use_agent');
  const [roleDefs, setRoleDefs] = useState<AgentRoleDef[]>([]);
  const [grantBusy, setGrantBusy] = useState(false);
  // Served, not restated: the role model lives in app/agent_roles.py and
  // a second copy here would drift from it.
  useEffect(() => {
    api.getAgentRoleCatalogue().then(setRoleDefs).catch(() => setRoleDefs([]));
  }, []);
  const [grantResult, setGrantResult] = useState<string | null>(null);

  if (!loaded) {
    setLoaded(true);
    api.getAgent(agentId).then((a) => { setAgent(a); setDescDraft(a.description || ''); }).catch((e) => toast(e.message));
    api.listAgentVersions(agentId).then(setVersions).catch(() => setVersions([]));
  }
  if (!agent) {
    return <Modal title="Publish" onClose={closeModal}><p className="small muted">Loading…</p></Modal>;
  }

  const uiUrl = `${window.location.origin}${window.location.pathname}#/app/${agent.id}`;
  const endpoint = `${BASE}/api/agents/${agent.id}/run`;
  const webAppOn = agent.status === 'published' && agent.features?.publish?.web_app !== false;
  const apiOn = agent.features?.publish?.api !== false;
  const mcpEnabled = !!agent.mcp_enabled;

  const copy = (text: string, label: string) => {
    navigator.clipboard.writeText(text).then(() => toast(`${label} copied`), () => toast('Could not copy — your browser blocked clipboard access'));
  };

  const doPublish = () => {
    setPublishing(true);
    api.publishAgent(agent.id, endUserIdentity)
      .then((r) => { setPublishResult(r); toast(`Agent published — ${r.version} is now live`); return api.getAgent(agent.id); })
      // setAgent is this MODAL's state. Everything underneath — the
      // Studio card's status chip, the Builder's own chip — reads its
      // own copy and had no way to learn the status moved, so an agent
      // stayed "draft" on screen until the page was reloaded.
      .then((a) => { setAgent(a); setVersions(null); notifyDataChanged(); })
      .catch((e) => toast(e.message))
      .finally(() => setPublishing(false));
  };

  const toggleVersions = () => {
    const next = !versionsOpen;
    setVersionsOpen(next);
    if (next && versions === null) {
      api.listAgentVersions(agent.id).then(setVersions).catch((e) => toast(e.message));
    }
  };

  const restoreVersion = (v: AgentVersion) => {
    setRestoringId(v.id);
    api.restoreAgentVersion(agent.id, v.id)
      .then((a) => { setAgent(a); notifyDataChanged(); toast(`Restored ${v.version} — now a draft, re-publish to make it live`); return api.listAgentVersions(agent.id); })
      .then(setVersions)
      .catch((e) => toast(e.message))
      .finally(() => setRestoringId(null));
  };

  const retrySync = () => {
    setUcsSyncing(true);
    api.retryUcsSync(agent.id)
      .then((a) => { setAgent(a); toast(a.ucs_synced ? 'Synced to UCS' : a.ucs_sync_error || 'Sync failed'); })
      .catch((e) => toast(e.message))
      .finally(() => setUcsSyncing(false));
  };

  const doGrant = () => {
    if (!grantGroup.trim()) return;
    setGrantBusy(true);
    setGrantResult(null);
    api.grantUcsAccess(agent.id, { group_name: grantGroup.trim(), role: grantRole })
      .then((r) => { setGrantResult(`Granted ${grantRole} on this agent to real group "${r.group}" via UCS role ${r.role}.`); toast(`Granted access to ${r.group}`); setGrantGroup(''); })
      .catch((e) => toast(e.message))
      .finally(() => setGrantBusy(false));
  };

  const doRevoke = () => {
    if (!grantGroup.trim()) return;
    setGrantBusy(true);
    setGrantResult(null);
    api.revokeUcsAccess(agent.id, { group_name: grantGroup.trim(), role: grantRole })
      .then((r) => { setGrantResult(`Revoked ${grantRole} on this agent from real group "${r.group}" via UCS role ${r.role}.`); toast(`Revoked access from ${r.group}`); setGrantGroup(''); })
      .catch((e) => toast(e.message))
      .finally(() => setGrantBusy(false));
  };

  const toggleWebApp = (on: boolean) => {
    api.setPublishSurface(agent.id, 'web_app', on).then((a) => { setAgent(a); notifyDataChanged(); toast(on ? 'Web App enabled' : 'Web App disabled'); }).catch((e) => toast(e.message));
  };
  const toggleApi = (on: boolean) => {
    api.setPublishSurface(agent.id, 'api', on).then((a) => { setAgent(a); notifyDataChanged(); toast(on ? 'Backend Service API enabled' : 'Backend Service API disabled'); }).catch((e) => toast(e.message));
  };
  const toggleMcp = (on: boolean) => {
    api.setMcp(agent.id, on).then((r) => {
      if (r.url) setMcpUrl(`${BASE}${r.url}`);
      return api.getAgent(agent.id);
    }).then((a) => { setAgent(a); toast(on ? 'MCP Server enabled' : 'MCP Server disabled'); }).catch((e) => toast(e.message));
  };
  const regenerateMcp = () => {
    setRegeneratingMcp(true);
    api.regenerateMcpToken(agent.id).then((r) => {
      setMcpUrl(`${BASE}${r.url}`);
      return api.getAgent(agent.id);
    }).then((a) => { setAgent(a); toast('MCP Server URL regenerated — the old URL no longer works'); }).catch((e) => toast(e.message)).finally(() => setRegeneratingMcp(false));
  };

  const showQr = () => {
    QRCode.toDataURL(uiUrl, { width: 220 }).then(setQrDataUrl).catch(() => toast('Could not generate QR code'));
  };

  const doExportDsl = () => {
    api.exportDsl(agent.id).then((dsl) => {
      const blob = new Blob([JSON.stringify(dsl, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `${agent.name.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.dsl.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast('DSL exported');
    }).catch((e) => toast(e.message));
  };

  const doDownloadYaml = () => {
    setYamlBusy(true);
    downloadAgentYaml(agent).then(toast).catch((e) => toast(e.message)).finally(() => setYamlBusy(false));
  };

  const doImportDsl = (file: File) => {
    file.text().then((text) => {
      const dsl = JSON.parse(text);
      return api.importDsl(dsl, true);
    }).then((newAgent) => {
      toast(`Imported as a new agent — ${newAgent.name}`);
      closeModal();
      navigate(`/builder?agent=${newAgent.id}`);
    }).catch((e) => toast(e?.message?.includes('JSON') ? 'That file is not valid DSL JSON' : e.message));
  };

  const doDuplicate = () => {
    api.duplicateAgent(agent.id).then((newAgent) => {
      toast(`Duplicated — ${newAgent.name}`);
      closeModal();
      navigate(`/builder?agent=${newAgent.id}`);
    }).catch((e) => toast(e.message));
  };

  const saveInfo = () => {
    api.setAgentMeta(agent.id, { description: descDraft, tags: agent.tags || [] }).then((a) => {
      setAgent(a);
      setEditingInfo(false);
      toast('Info saved');
    }).catch((e) => toast(e.message));
  };

  const embedSnippet = `<iframe
  src="${uiUrl}"
  style="width: 100%; height: 100%; min-height: 600px"
  frameborder="0"
  allow="microphone">
</iframe>`;
  const chatBubbleSnippet = `<script>
  window.inaieraChatbotConfig = {
    token: '${agent.id}',
    baseUrl: '${window.location.origin}',
  };
</script>
<script src="${BASE}/embed.js"></script>`;

  const hasEverPublished = versions !== null && versions.length > 0;
  const latestVersion = versions?.[0] || null;
  const hasChangesSincePublish = !latestVersion || new Date(`${agent.updated_at}Z`).getTime() > new Date(`${latestVersion.created_at}Z`).getTime();
  const publishDisabled = publishing || (hasEverPublished && !hasChangesSincePublish);
  const roleDef = roleDefs.find((r) => r.name === grantRole);

  return (
    <>
    <Modal title="Publish" sub="Release the current draft, choose where people can use it, control who has access, and export a copy." onClose={closeModal} footer={
      <button className="btn ghost" onClick={closeModal}>Close</button>
    }>
      {/* ---- Agent ---- */}
      <div className="pub-hero">
        <span className="pub-hero-icon"><Icon name="i-flow" /></span>
        <div className="pub-hero-main">
          <b>{agent.name}</b>
          <div className="small muted">
            <span style={{ textTransform: 'capitalize' }}>{agent.app_type || agent.subtype}</span>
            {latestVersion && <> · <span className="mono">{`${latestVersion.version} live`}</span></>}
          </div>
        </div>
        <div className="pub-hero-actions">
          <button className="icon-action" title="Edit Info" aria-label="Edit Info" aria-pressed={editingInfo} onClick={() => setEditingInfo((v) => !v)}><Icon name="i-pencil" /></button>
          <button className="btn sm ghost" onClick={() => setSavingTemplate(true)}
            title="Save this flow as a reusable Private or Platform-provided template draft"><Icon name="i-grid" /> Save as template</button>
          <button className="btn sm ghost" onClick={doDuplicate}><Icon name="i-copy" /> Duplicate</button>
        </div>
      </div>
      {editingInfo && (
        <div className="pub-inline">
          <div className="field"><label>Description</label><textarea rows={2} value={descDraft} onChange={(e) => setDescDraft(e.target.value)} /></div>
          <div className="row-split" style={{ justifyContent: 'flex-end', gap: 8 }}>
            <button className="btn sm ghost" onClick={() => setEditingInfo(false)}>Cancel</button>
            <button className="btn sm" onClick={saveInfo}>Save</button>
          </div>
        </div>
      )}

      {/* ---- 1. Release ---- */}
      <section className="pub-section">
        <div className="pub-section-h">
          <span className="pub-step">1</span>
          <div><h4>Release</h4><p>Publishing makes the current draft the version every channel below runs.</p></div>
        </div>
        {publishResult ? (
          <Banner kind="good">Published — {publishResult.version} is now live, entitlement plan resolved for {publishResult.entitlement_plan?.objects_checked?.length ?? 0} catalog object(s).</Banner>
        ) : (
          <div className="pub-status">
            <span className={`pub-dot ${hasEverPublished && !hasChangesSincePublish ? 'live' : 'draft'}`} />
            <div style={{ flex: 1 }}>
              <div className="pub-status-k">{hasEverPublished && !hasChangesSincePublish ? 'Latest published' : 'Current draft unpublished'}</div>
              <div className="small">
                {!hasEverPublished
                  ? <>Auto-Saved · {relativeTime(agent.updated_at)} <span className="muted">— never published</span></>
                  : hasChangesSincePublish
                    ? <>Auto-Saved · {relativeTime(agent.updated_at)} <span className="muted">— last published {relativeTime(latestVersion!.created_at)}</span></>
                    : <>Published {relativeTime(latestVersion!.created_at)}</>}
              </div>
            </div>
            {hasEverPublished && (
              <button className="link small pub-link-btn" onClick={() => restoreVersion(latestVersion!)} title="Discard the draft and go back to the latest published version">Restore</button>
            )}
          </div>
        )}
        <button className="btn block" disabled={publishDisabled} onClick={doPublish} title={publishDisabled && !publishing ? 'No changes since the last publish' : undefined}>
          {publishing ? 'Publishing…' : 'Publish Update'}
        </button>

        <button type="button" className="pub-disclosure" onClick={toggleVersions} aria-expanded={versionsOpen}>
          <Icon name="i-clock" /><span>Version History</span>
          {versions && versions.length > 0 && <span className="small muted">{versions.length}</span>}
          <span className="pub-chev" style={{ transform: versionsOpen ? 'rotate(90deg)' : 'none' }}><Icon name="i-chev" /></span>
        </button>
        {versionsOpen && (
          versions === null ? (
            <p className="small muted">Loading…</p>
          ) : versions.length === 0 ? (
            <p className="small muted" style={{ margin: '8px 0 0' }}>No published versions yet — Publish above to create the first real snapshot.</p>
          ) : (
            <div className="pub-versions">
              {versions.map((v) => (
                <div key={v.id} className="pub-version">
                  <div>
                    <span className="mono" style={{ fontWeight: 700 }}>{v.version}</span>
                    {v.is_current && <span className="chip green" style={{ marginLeft: 8 }}>current</span>}
                    <div className="small muted">{relativeTime(v.created_at)} · published by {v.published_by}</div>
                  </div>
                  {!v.is_current && (
                    <button className="btn sm ghost" disabled={restoringId === v.id} onClick={() => restoreVersion(v)}>
                      <Icon name="i-repeat" /> {restoringId === v.id ? 'Restoring…' : 'Restore'}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )
        )}
      </section>

      {/* ---- 2. Share ---- */}
      <section className="pub-section">
        <div className="pub-section-h">
          <span className="pub-step">2</span>
          <div><h4>Share</h4><p>Turn on the ways people and systems can run this agent.</p></div>
        </div>

        <div className="pub-channel">
          <div className="pub-channel-h">
            <span className="pub-channel-icon"><Icon name="i-flow" /></span>
            <div className="pub-channel-t"><b>Web App</b><span className="small muted">A hosted page anyone with the link can use.</span></div>
            <StatusBadge on={webAppOn} />
            <div className={`tog${webAppOn ? ' on' : ''}`} role="switch" aria-checked={webAppOn} aria-label="Web App" onClick={() => toggleWebApp(!webAppOn)} />
          </div>
          {agent.status !== 'published' && <p className="small muted pub-note">Publish the agent first — until then the link shows a "not published" message.</p>}
          <div className="pub-copy">
            <input className="mono small" readOnly value={uiUrl} onFocus={(e) => e.target.select()} aria-label="Public URL" />
            <button className="btn sm ghost" onClick={() => copy(uiUrl, 'URL')}><Icon name="i-copy" /> Copy</button>
          </div>
          <div className="pub-links">
            <a href={`#/app/${agent.id}`} target="_blank" rel="noreferrer"><Icon name="i-flow" /> Launch</a>
            <button type="button" onClick={showQr}><Icon name="i-grid" /> QR</button>
            <button type="button" onClick={() => setShowEmbed((v) => !v)}><Icon name="i-code" /> Embedded</button>
            <button type="button" onClick={() => setWsOpen(true)}><Icon name="i-cog" /> Settings</button>
          </div>
          {qrDataUrl && <div style={{ marginTop: 10 }}><img src={qrDataUrl} alt="QR code for the real public URL" width={140} height={140} /></div>}
          {showEmbed && (
            <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
              <div className="seg" style={{ display: 'inline-flex', marginBottom: 8 }}>
                <button className={embedMode !== 'bubble' ? 'on' : ''} onClick={() => setEmbedMode('iframe')}>Iframe</button>
                <button className={embedMode === 'bubble' ? 'on' : ''} onClick={() => setEmbedMode('bubble')}>Chat Bubble</button>
              </div>
              {embedMode === 'bubble' ? (
                <>
                  <p className="small muted" style={{ margin: '0 0 6px' }}>A floating chat button on every page of your site — one snippet, available everywhere.</p>
                  <pre className="codeblock">{chatBubbleSnippet}</pre>
                  <button className="btn sm ghost" onClick={() => copy(chatBubbleSnippet, 'Embed snippet')}><Icon name="i-copy" /> Copy snippet</button>
                  <p className="small muted" style={{ marginTop: 8, marginBottom: 0 }}>
                    Restyle it with CSS variables such as <span className="mono small">--inaiera-chatbot-bubble-button-bg-color</span> on <span className="mono small">#inaiera-chatbot-bubble-button</span>.
                  </p>
                </>
              ) : (
                <>
                  <p className="small muted" style={{ margin: '0 0 6px' }}>Place the app inside a page where it's the main content.</p>
                  <pre className="codeblock">{embedSnippet}</pre>
                  <button className="btn sm ghost" onClick={() => copy(embedSnippet, 'Embed snippet')}><Icon name="i-copy" /> Copy snippet</button>
                </>
              )}
            </div>
          )}
        </div>

        <div className="pub-channel">
          <div className="pub-channel-h">
            <span className="pub-channel-icon"><Icon name="i-code" /></span>
            <div className="pub-channel-t"><b>Backend Service API</b><span className="small muted">Call the agent from your own code over HTTP.</span></div>
            <StatusBadge on={apiOn} />
            <div className={`tog${apiOn ? ' on' : ''}`} role="switch" aria-checked={apiOn} aria-label="Backend Service API" onClick={() => toggleApi(!apiOn)} />
          </div>
          <div className="pub-copy">
            <input className="mono small" readOnly value={endpoint} onFocus={(e) => e.target.select()} aria-label="Service API endpoint" />
            <button className="btn sm ghost" onClick={() => copy(endpoint, 'Endpoint')}><Icon name="i-copy" /> Copy</button>
          </div>
          <p className="small pub-note pub-warn"><Icon name="i-alert" /> No authentication yet — anyone who can reach this endpoint can call it.</p>
        </div>

        <div className="pub-channel">
          <div className="pub-channel-h">
            <span className="pub-channel-icon"><Icon name="i-plug" /></span>
            <div className="pub-channel-t"><b>MCP Server</b><span className="small muted">Use the agent as a tool in Claude Desktop, Cursor and other MCP clients.</span></div>
            <StatusBadge on={mcpEnabled} />
            <div className={`tog${mcpEnabled ? ' on' : ''}`} role="switch" aria-checked={mcpEnabled} aria-label="MCP Server" onClick={() => toggleMcp(!mcpEnabled)} />
          </div>
          {mcpEnabled && !mcpUrl && (
            <div className="pub-note">
              <p className="small muted" style={{ margin: '0 0 8px' }}>The server URL is only shown once, when it's created. Regenerate to get a new one you can copy.</p>
              <button className="btn sm ghost" disabled={regeneratingMcp} onClick={regenerateMcp}><Icon name="i-refresh" /> {regeneratingMcp ? 'Regenerating…' : 'Regenerate URL'}</button>
            </div>
          )}
          {mcpEnabled && mcpUrl && (
            <>
              <div className="pub-copy">
                <input className="mono small" readOnly value={mcpUrl} onFocus={(e) => e.target.select()} aria-label="MCP Server URL" />
                <button className="btn sm ghost" onClick={() => copy(mcpUrl, 'MCP Server URL')}><Icon name="i-copy" /> Copy</button>
                <button className="btn sm ghost" disabled={regeneratingMcp} onClick={regenerateMcp}><Icon name="i-refresh" /> {regeneratingMcp ? 'Regenerating…' : 'Regenerate'}</button>
              </div>
              <p className="small muted pub-note">Treat this URL like an API key.</p>
            </>
          )}
        </div>
      </section>

      {/* ---- 3. Access ---- */}
      {UCS_ENABLED && <section className="pub-section">
        <div className="pub-section-h">
          <span className="pub-step">3</span>
          <div style={{ flex: 1 }}><h4>UCS Access</h4><p>Choose which groups can use this agent. Access is managed in the Unified Catalog.</p></div>
          {agent.ucs_synced ? <span className="chip green">synced</span> : <span className="chip amber">not synced</span>}
        </div>
        {agent.ucs_fqn && <div className="pub-fqn"><span className="small muted">Catalog object</span><span className="mono small">{agent.ucs_fqn}</span></div>}
        {!agent.ucs_synced && (
          <div className="pub-status pub-status-bad">
            <span className="small" style={{ flex: 1, color: 'var(--danger)' }}>{agent.ucs_sync_error || 'Not yet synced.'}</span>
            <button className="btn sm ghost" disabled={ucsSyncing} onClick={retrySync}><Icon name="i-refresh" /> {ucsSyncing ? 'Retrying…' : 'Retry sync'}</button>
          </div>
        )}
        <div className="pub-grant">
          <div className="field"><label>Group</label>
            <input placeholder="group name" value={grantGroup} onChange={(e) => setGrantGroup(e.target.value)} />
          </div>
          <div className="field"><label id="publish-grant-role-label">Access level</label>
            <SearchSelect labelledBy="publish-grant-role-label" noun="access level" value={grantRole}
              title="What this group may do with this agent"
              options={roleDefs.map((r) => ({ value: r.name, label: r.label }))}
              onChange={(v) => setGrantRole(v)} />
          </div>
          <div className="pub-grant-btns">
            <button className="btn sm" disabled={grantBusy || !grantGroup.trim()} onClick={doGrant}><Icon name="i-plus" /> Grant</button>
            <button className="btn sm ghost danger" disabled={grantBusy || !grantGroup.trim()} onClick={doRevoke}><Icon name="i-trash" /> Revoke</button>
          </div>
        </div>
        {/* What the selected level grants. `enforced: false` is shown, not hidden:
            a permission that gates nothing reads as protection otherwise. */}
        {roleDef && (
          <div className="pub-role">
            <div className="small"><b>{roleDef.label}</b> — {roleDef.summary}</div>
            <ul>
              {roleDef.permissions.map((p) => (
                <li key={p.key} className="small">
                  <span className="mono">{p.key}</span> — {p.summary}
                  {!p.enforced && <span className="pub-recorded"> · recorded, not yet enforced</span>}
                </li>
              ))}
            </ul>
            <div className="small muted">Sent to UCS as access level <span className="mono">{roleDef.ucs_access_level}</span>.</div>
          </div>
        )}
        {grantResult && <div className="small muted" style={{ marginTop: 8 }}>{grantResult}</div>}
      </section>}

      {/* ---- 4. Export ---- */}
      <section className="pub-section">
        <div className="pub-section-h">
          <span className="pub-step">{UCS_ENABLED ? 4 : 3}</span>
          <div><h4>Export</h4><p>Keep a copy of the agent, put it in Git, or bring one in.</p></div>
        </div>
        <div className="pub-export-grid">
          <button className="pub-export" onClick={doDownloadYaml} disabled={yamlBusy}>
            <Icon name="i-file" /><span><b>{yamlBusy ? 'Downloading…' : 'Download YAML'}</b><span className="small muted">Readable file; also committed if Git auto-commit is on.</span></span>
          </button>
          <button className={`pub-export${gitOpen ? ' on' : ''}`} onClick={() => setGitOpen((v) => !v)}>
            <Icon name="i-git" /><span><b>Commit to Git</b><span className="small muted">Pick a repository, branch and path.</span></span>
          </button>
          <button className="pub-export" onClick={doExportDsl}>
            <Icon name="i-scroll" /><span><b>Export DSL</b><span className="small muted">JSON file you can import back.</span></span>
          </button>
          <button className="pub-export" onClick={() => importInputRef.current?.click()}>
            <Icon name="i-plus" /><span><b>Import DSL</b><span className="small muted">Create a new agent from a JSON file.</span></span>
          </button>
        </div>
        <input ref={importInputRef} type="file" accept="application/json" style={{ display: 'none' }} onChange={(e) => { const f = e.target.files?.[0]; if (f) doImportDsl(f); e.target.value = ''; }} />
        {gitOpen && <div style={{ marginTop: 12 }}><GitCommitPanel agent={agent} onClose={() => setGitOpen(false)} /></div>}
      </section>
    </Modal>
    {wsOpen && <WebAppSettingsModal agent={agent} onClose={() => setWsOpen(false)} onSaved={setAgent} />}
    {savingTemplate && <TemplateDetailsModal target={{ mode: 'save', agent }} onClose={() => setSavingTemplate(false)} />}
    </>
  );
}
