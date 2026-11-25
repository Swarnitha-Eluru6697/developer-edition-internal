import { useEffect, useState, type ReactNode } from 'react';
import { confirmAction } from '../components/ConfirmPanel';
import { Link } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import { PageHead, Banner, IconAction } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { ModelProvider, Tool, MCPServer, DataSource, KnowledgeBase, MlStudioDeployment, LlmModelOption } from '../types';

type Tab = 'models' | 'tools' | 'mcp' | 'datasources' | 'knowledgebase';
const TAB_LABELS: Record<Tab, string> = { models: 'Model Providers', tools: 'Tools', mcp: 'MCP', datasources: 'Data Sources', knowledgebase: 'KB Connections' };

function ProviderRow({ m }: { m: ModelProvider }) {
  const { openModal, toast, dataVersion } = useApp();
  const [showModels, setShowModels] = useState(false);
  const [models, setModels] = useState<any>(null);

  // Fetched on first expand rather than with the page: there are ten
  // providers and most will never be opened.
  useEffect(() => {
    if (!showModels) return;
    api.listProviderModels(m.name).then(setModels).catch(() => setModels({ builtin: [], custom: [] }));
  }, [showModels, m.name, dataVersion]);

  const remove = async (modelName: string) => {
    if (!(await confirmAction({ title: 'Remove model', confirmLabel: 'Remove',
      body: `Remove "${modelName}" from ${m.name}? Agents configured to use it will stop resolving.` }))) return;
    api.deleteProviderModel(m.name, modelName)
      .then(() => { toast(`Removed ${modelName}`); setModels(null);
                    api.listProviderModels(m.name).then(setModels).catch(() => {}); })
      .catch((e) => toast(e.message));
  };

  return (
    <div className="card pad" style={{ marginBottom: 10 }}>
      <div className="row-split">
        <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
          <b>{m.name}</b><span className="mono small muted">{m.version}</span>
          {/* §0.114, GAP_REPORT — the actual fix for a real, reported
              product/runtime mismatch: a builder must never see a
              catalog-only provider presented the same way as one that
              genuinely executes real Agent runs. */}
          {m.runtime_supported
            ? <span className="chip green">Runtime-wired</span>
            : <span className="chip line">Catalog only — not yet wired for Agent execution</span>}
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className="small muted" style={{ marginBottom: 5 }}>
            {/* A key that cannot be decrypted has to say so here. It is
                stored, so the row reads as configured and shows a masked
                value, but nothing can use it — and the resulting error
                appears far away ("no key configured" from whatever tried
                to call the provider). */}
            {m.key_usable === false
              ? 'Key stored but unreadable — re-enter it'
              : m.status === 'credits' ? 'AI credits in use' : 'API key required'}
          </div>
          <button className={`btn sm${m.status === 'needs_key' ? '' : ' ghost'}`} onClick={() => openModal('apiKeyConfig', m.name)}>
            <Icon name="i-key" /> Add API Key
          </button>
        </div>
      </div>
      <div className="pill-row" style={{ marginTop: 10 }}>{m.types.map((t) => <span key={t} className="chip line">{t}</span>)}</div>
      <div className="row-split" style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--line-2)' }}>
        <button className="link-btn" onClick={() => setShowModels((v) => !v)}>
          {showModels ? 'Hide models' : 'Show models'} <Icon name="i-chev" />
        </button>
        {/* Adding a model only means something for a provider this
            platform can actually execute against and that has a key —
            the API refuses both cases, so the control reflects that
            rather than offering a click that always fails. */}
        <button
          className="link-btn"
          disabled={!m.runtime_supported || m.status === 'needs_key'}
          title={!m.runtime_supported
            ? 'This provider has no execution adapter yet, so a model added here could not run'
            : m.status === 'needs_key' ? 'Add an API key first' : `Register a model for ${m.name}`}
          onClick={() => openModal('addProviderModel', m.name)}
        >
          <Icon name="i-plus" /> Add model
        </button>
      </div>
      {showModels && (
        <div className="provider-models">
          {models === null && <div className="small muted">Loading…</div>}
          {models && models.builtin.length === 0 && models.custom.length === 0 && (
            <div className="small muted">No models listed for this provider.</div>
          )}
          {models && [...models.builtin, ...models.custom].map((mm: any) => (
            <div key={mm.name} className="provider-model-row">
              <div>
                <b className="small">{mm.name}</b>
                <span className="mono small muted" style={{ marginLeft: 8 }}>{mm.api_model_id}</span>
              </div>
              <div className="row-split" style={{ gap: 8, justifyContent: 'flex-end' }}>
                <span className={`chip ${mm.builtin ? 'line' : 'teal'} sm`}>{mm.builtin ? 'Built-in' : 'Registered'}</span>
                {!mm.builtin && (
                  <button className="icon-action danger" title={`Delete ${mm.name}`} aria-label={`Delete ${mm.name}`} onClick={() => remove(mm.name)}>
                    <Icon name="i-trash" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {m.status === 'needs_key' && <Banner kind="info">Set up an API key or add a model to use this provider.</Banner>}
      {m.key_usable === false && (
        <Banner kind="warn">
          This provider&apos;s stored key cannot be decrypted with the encryption key this server is
          running with, so every call using it will report that no key is configured. Re-enter the key
          to fix it. If this keeps happening, the server is starting without a persistent
          SECRETS_ENCRYPTION_KEY and is generating a throwaway one per process.
        </Banner>
      )}
      {!m.runtime_supported && (
        <Banner kind="info">
          {m.unsupported_reason
            ? <>Not wired for Agent execution: {m.name} {m.unsupported_reason}. An API key stored here
                won't change what an Agent run calls until an adapter for that protocol exists.</>
            : <>This provider is a cataloged entry, but this platform's Agent runtime does not yet execute
                against it — configuring an API key here won't change what a real Agent run actually calls.</>}
        </Banner>
      )}
      {m.runtime_supported && m.requires_base_url && m.status !== 'needs_key' && !m.api_base_url && (
        <Banner kind="warn">
          {m.name} is provisioned per resource, so it has no default endpoint. Set the API base URL on this
          provider (e.g. https://&lt;your-resource&gt;.openai.azure.com/openai/v1) or calls will fail.
        </Banner>
      )}
    </div>
  );
}

type ModelsSubTab = 'external' | 'mlstudio';
const MODELS_SUB_TABS: [ModelsSubTab, string][] = [['external', 'External providers'], ['mlstudio', 'ML Studio models']];

function ModelsTab({ sub }: { sub: ModelsSubTab }) {
  return sub === 'external' ? <ExternalProvidersTab /> : <MlStudioModelsTab />;
}

const fmtDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

/** The client secret of one ML Studio endpoint, so agents' LLM nodes can run
 *  that model. Write-only: the page only knows whether one is stored. */
function MlStudioSecretControl({ modelId, configured, onChanged }: {
  modelId: string; configured: boolean; onChanged: () => void;
}) {
  const { toast } = useApp();
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!secret.trim()) return;
    setBusy(true);
    try {
      await api.setMlStudioCredential(modelId, secret.trim());
      toast(`${modelId} can now be picked as an LLM in agents`);
      setSecret(''); setEditing(false); onChanged();
    } catch (e: any) {
      toast(e.message || 'Could not save the client secret');
    } finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true);
    try { await api.deleteMlStudioCredential(modelId); onChanged(); }
    catch (e: any) { toast(e.message || 'Could not remove the client secret'); }
    finally { setBusy(false); }
  };
  if (editing) {
    return (
      <div className="row-split" style={{ gap: 8, justifyContent: 'flex-start', marginTop: 8 }}>
        <input className="mono" type="password" autoComplete="new-password" placeholder="Endpoint client secret (cs_live_…)"
          aria-label={`Client secret for ${modelId}`} value={secret} onChange={(e) => setSecret(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') save(); }} style={{ flex: 1, minWidth: 220 }} />
        <button type="button" className="btn sm" disabled={busy || !secret.trim()} onClick={save}>Save</button>
        <button type="button" className="btn sm ghost" disabled={busy} onClick={() => { setEditing(false); setSecret(''); }}>Cancel</button>
      </div>
    );
  }
  return (
    <div className="row-split" style={{ gap: 8, justifyContent: 'flex-start', marginTop: 8 }}>
      {configured
        ? <span className="chip green">Usable in agents</span>
        : <span className="chip amber">Needs client secret</span>}
      <IconAction icon="i-key" tipAlign="start" disabled={busy} onClick={() => setEditing(true)}
        tip={configured ? `Replace the client secret for ${modelId}` : `Add the client secret for ${modelId}`} />
      {configured && <IconAction icon="i-trash" danger tipAlign="start" disabled={busy} onClick={remove}
        tip={`Remove the client secret for ${modelId} (agents can no longer run it)`} />}
    </div>
  );
}

/** Models the signed-in user deployed in ML Studio — a separate service, read
 *  through the /mlstudio-api route and matched on the gateway user name
 *  (GET /api/me's `user`), which both apps share. LLM deployments (anyone's)
 *  can be connected here so agents' LLM nodes can run them. */
function MlStudioModelsTab() {
  const { me, dataVersion } = useApp();
  const userName = me?.user ?? null;
  const [models, setModels] = useState<MlStudioDeployment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [configured, setConfigured] = useState<Set<string>>(new Set());
  const [otherLlms, setOtherLlms] = useState<LlmModelOption[]>([]);
  const [credVersion, setCredVersion] = useState(0);

  useEffect(() => {
    if (!userName) return;
    setModels(null); setError(null);
    api.listMlStudioDeployments(userName).then(setModels).catch((e) => setError(e.message));
  }, [userName, dataVersion]);

  useEffect(() => {
    api.listMlStudioCredentials?.()?.then((r) => setConfigured(new Set(r.models))).catch(() => {});
    api.listLlmModels?.()?.then((r) => setOtherLlms(r.models.filter((m) => m.mlstudio?.domain === 'llm'))).catch(() => {});
  }, [dataVersion, credVersion]);

  if (!userName) {
    return <Banner kind="info">No signed-in user, so there are no ML Studio deployments to match.</Banner>;
  }
  if (error) {
    return <Banner kind="warn">{error} — is the ML Studio backend running? Its deployments are read through <span className="mono">/mlstudio-api</span>.</Banner>;
  }
  if (!models) return <div className="small muted">Loading…</div>;

  const mine = new Set(models.map((m) => m.modelId));
  const others = otherLlms.filter((m) => m.mlstudio && !mine.has(m.mlstudio.model_id));
  const refresh = () => setCredVersion((v) => v + 1);

  return (
    <>
      <Banner kind="info">
        Models <b>{userName}</b> deployed in ML Studio. LLMs among them can be picked as the model of an LLM, Agent or
        parameter-extraction node once their endpoint's client secret (ML Studio › Endpoints) is added here.
      </Banner>
      {models.length === 0 ? (
        <div className="card pad small muted">
          {userName} hasn't deployed any models in ML Studio yet. Deployments made before ML Studio started recording who deployed them aren't attributed to anyone, so they don't appear here.
        </div>
      ) : models.map((m) => (
        <div key={m.modelId} className="card pad" style={{ marginBottom: 10 }}>
          <div className="row-split">
            <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
              <b>{m.modelId}</b><span className="mono small muted">v{m.currentVersion}</span>
              <span className="chip line">{m.domain === 'llm' ? 'LLM' : 'ML'}</span>
              <span className={`chip ${m.healthStatus === 'healthy' ? 'green' : 'amber'}`}>{m.healthStatus}</span>
            </div>
            <div className="small muted">{m.runtime}</div>
          </div>
          <div className="small muted" style={{ marginTop: 6 }}>
            {m.task} · {m.framework}{m.deployedAt ? ` · deployed ${fmtDate(m.deployedAt)}` : ''}
          </div>
          {m.endpointUrl && <div className="mono small" style={{ marginTop: 6, overflowWrap: 'anywhere' }}>{m.endpointUrl}</div>}
          {m.domain === 'llm'
            ? <MlStudioSecretControl modelId={m.modelId} configured={configured.has(m.modelId)} onChanged={refresh} />
            : <div className="small muted" style={{ marginTop: 8 }}>Classical ML model — it takes feature columns, not a prompt, so it isn't offered as a node's LLM.</div>}
        </div>
      ))}
      {others.length > 0 && (
        <>
          <div className="sechead">Other deployed ML Studio LLMs</div>
          {others.map((m) => (
            <div key={m.name} className="card pad" style={{ marginBottom: 10 }}>
              <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                <b>{m.mlstudio!.model_id}</b>
                {m.mlstudio!.version != null && <span className="mono small muted">v{m.mlstudio!.version}</span>}
                <span className="chip line">LLM</span>
                <span className="small muted">{m.mlstudio!.task}{m.mlstudio!.deployed_by ? ` · deployed by ${m.mlstudio!.deployed_by}` : ''}</span>
              </div>
              <MlStudioSecretControl modelId={m.mlstudio!.model_id} configured={configured.has(m.mlstudio!.model_id)} onChanged={refresh} />
            </div>
          ))}
        </>
      )}
    </>
  );
}

function ExternalProvidersTab() {
  const { dataVersion } = useApp();
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const load = () => api.listModelProviders().then(setProviders).catch(() => {});
  useEffect(() => { load(); }, [dataVersion]);
  const credits = providers.filter((p) => p.status === 'credits');
  const needsKey = providers.filter((p) => p.status === 'needs_key');
  return (
    <>
      <Banner kind="good">Connected providers appear in this workspace's UCS <span className="mono">Model</span> catalog — LLM Nodes in the builder pick from here (§7.4).</Banner>
      <div className="sechead" style={{ marginTop: 0 }}>Connected <span className="small muted" style={{ textTransform: 'none', fontWeight: 400 }}>— using platform AI credits</span></div>
      {credits.map((m) => <ProviderRow key={m.name} m={m} />)}
      <div className="sechead">Configuration required <span className="small muted" style={{ textTransform: 'none', fontWeight: 400 }}>— add your own API key to enable</span></div>
      {needsKey.map((m) => <ProviderRow key={m.name} m={m} />)}
    </>
  );
}

function ToolsTab() {
  const { dataVersion, openDrawer } = useApp();
  const [tools, setTools] = useState<Tool[]>([]);
  useEffect(() => { api.listTools().then(setTools).catch(() => {}); }, [dataVersion]);
  return (
    <>
      <Banner kind="good">Installed tools appear in the Builder's Tool/Connector and HTTP Request pickers — entitlement-checked the same as any UCS-cataloged object.</Banner>
      <div className="grid g3">
        {tools.map((t) => (
          <div className="card pad click" key={t.name} style={{ cursor: 'pointer' }} onClick={() => openDrawer('toolDetail', t.name)}>
            <div className="row-split"><b>{t.name}</b>{t.source === 'builtin' ? <span className="chip slate">built-in</span> : <span className="chip teal">marketplace</span>}</div>
            <div className="small muted mono" style={{ margin: '4px 0' }}>{t.org}{t.version ? ` · v${t.version}` : ''}</div>
            <div className="small muted">{t.desc}</div>
          </div>
        ))}
      </div>
    </>
  );
}

function McpTab() {
  const { dataVersion, openModal } = useApp();
  const [servers, setServers] = useState<MCPServer[]>([]);
  useEffect(() => { api.listMcpServers().then(setServers).catch(() => {}); }, [dataVersion]);
  return (
    <>
      <Banner kind="good">Connected MCP servers register their actions into the Builder's Tool/Connector picker — each action still routes through the same UCS entitlement check as any other tool.</Banner>
      <div className="row-split" style={{ marginBottom: 14 }}>
        <span className="small muted">{servers.length} server(s) connected</span>
        <button className="btn" onClick={() => openModal('addMcp')}><Icon name="i-plus" /> Add MCP Server</button>
      </div>
      <div className="grid g2">
        {servers.map((s) => (
          <div className="card pad" key={s.id}>
            <div className="row-split"><b>{s.name}</b><span className={`chip ${s.status === 'connected' ? 'green' : 'line'}`}>{s.status}</span></div>
            <div className="mono small muted" style={{ margin: '4px 0' }}>{s.identifier}</div>
            <div className="mono small muted" style={{ wordBreak: 'break-all' }}>{s.url}</div>
            <div className="small muted" style={{ marginTop: 8 }}>{s.actions} actions included</div>
          </div>
        ))}
      </div>
    </>
  );
}

function DataSourcesTab() {
  const { dataVersion, openModal } = useApp();
  const [sources, setSources] = useState<DataSource[]>([]);
  useEffect(() => { api.listDataSources().then(setSources).catch(() => {}); }, [dataVersion]);
  return (
    <>
      <Banner kind="info">Data sources feed Knowledge Retrieval nodes via UCS-governed ingestion — connect here, then the object shows up in the Catalog for entitlement and lineage tracking.</Banner>
      <div className="card">
        {sources.map((d) => (
          <div className="row-split" key={d.name} style={{ padding: '14px 18px', borderBottom: '1px solid var(--line-2)' }}>
            <div>
              <b>{d.name}</b> <span className="mono small muted">v{d.version}</span>
              <div className="small muted" style={{ marginTop: 2 }}>
                {/* "Connected" over a credential nothing can read is the
                    same lie the Model Providers tab told: the row looks
                    ready and every browse fails with a crypto error. */}
                {d.status === 'needs_auth'
                  ? 'Please configure authentication'
                  : d.credentials_usable === false
                    ? <span className="chip amber">Credentials unreadable — reconfigure</span>
                    : <span className="chip green">Connected</span>}
              </div>
            </div>
            <button className="btn sm ghost" onClick={() => openModal('dsConfig', d.name)}>
              <Icon name="i-cog" /> {d.status === 'needs_auth' ? 'Configure' : 'Reconfigure'}
            </button>
          </div>
        ))}
      </div>
    </>
  );
}

function KnowledgeBaseTab({ tabStrip }: { tabStrip: ReactNode }) {
  const { openModal, toast, dataVersion } = useApp();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [testing, setTesting] = useState<string | null>(null);
  const load = () => api.listKnowledgeBases().then(setKbs).catch(() => {});
  useEffect(() => { load(); }, [dataVersion]);

  const test = (kb: KnowledgeBase) => {
    setTesting(kb.id);
    api.testKnowledgeBase(kb.id)
      .then((r) => {
        toast(r.test_result.ok ? `${kb.name}: ${r.test_result.detail}` : `${kb.name} failed: ${r.test_result.error?.slice(0, 100)}`);
        load();
      })
      .catch((e) => toast(e.message))
      .finally(() => setTesting(null));
  };

  const remove = (kb: KnowledgeBase) => {
    api.deleteKnowledgeBase(kb.id).then(() => { toast(`${kb.name} removed`); load(); }).catch((e) => toast(e.message));
  };

  return (
    <>
      {/* The tab strip, banner and Add row pin together as one block, so the
          list scrolls under them instead of them sliding over each other. */}
      <div className="sticky-head">
        {tabStrip}
        <Banner kind="good">
          KB Connections here is just the real connection catalog — the same real reachability check every other
          Integrations tab gets, no document pipeline. Once a pgvector connection is added and tested, its real
          document ingestion, chunking, embedding, and retrieval testing live on the <Link to="/rag" className="link">RAG</Link> page,
          not here.
        </Banner>
        <div className="row-split" style={{ marginBottom: 14 }}>
          <span className="small muted">{kbs.length} knowledge base(s) configured</span>
          <button className="btn" onClick={() => openModal('addKnowledgeBase')}><Icon name="i-plus" /> Add Knowledge Base</button>
        </div>
      </div>
      {kbs.map((kb) => (
        <div className="card pad" key={kb.id} style={{ marginBottom: 10 }}>
          <div className="row-split">
            <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
              <b>{kb.name}</b>
              <span className="chip line">{kb.provider}</span>
              <span className={`chip ${kb.status === 'connected' ? 'green' : kb.status === 'failed' ? 'amber' : 'line'}`}>{kb.status}</span>
            </div>
            <div className="row-split" style={{ gap: 8 }}>
              <button className="btn sm ghost" disabled={testing === kb.id} onClick={() => test(kb)}><Icon name="i-test" /> {testing === kb.id ? 'Testing…' : 'Test'}</button>
              {/* Editing was previously impossible — creation was the only
                  write path for these fields, so a wrong host or a missing
                  Pinecone index meant deleting the knowledge base and
                  rebuilding it, losing its documents with it. */}
              <button
                className="icon-action"
                title={`Edit ${kb.name}'s connection`}
                aria-label={`Edit ${kb.name}'s connection`}
                onClick={() => openModal('editKbConnection', kb.id)}
              >
                <Icon name="i-pencil" />
              </button>
              {kb.provider === 'pgvector' && (
                <Link to="/rag" className="btn sm ghost"><Icon name="i-cog" /> Manage in RAG</Link>
              )}
              <button className="icon-action danger" title={`Delete ${kb.name}`} aria-label={`Delete ${kb.name}`} onClick={() => remove(kb)}><Icon name="i-trash" /></button>
            </div>
          </div>
          <div className="mono small muted" style={{ marginTop: 8 }}>
            {kb.provider === 'pgvector' ? `${kb.config.user}@${kb.config.host}:${kb.config.port}/${kb.config.dbname}${kb.config.table ? ` · ${kb.config.table}` : ''}` : kb.config.url || '(no URL configured)'}
          </div>
          {kb.last_error && <div className="small" style={{ color: 'var(--red)', marginTop: 6 }}>{kb.last_error}</div>}
        </div>
      ))}
      {kbs.length === 0 && <div className="empty-cfg" style={{ padding: '40px 10px' }}>No knowledge bases configured yet.</div>}
    </>
  );
}

export default function Integrations() {
  const [tab, setTab] = useState<Tab>('models');
  const [modelsSub, setModelsSub] = useState<ModelsSubTab>('external');
  // Model Providers' own sub-tabs sit at the right end of the same row,
  // not as a second row underneath.
  const tabStrip = (
    <div className="tabs tabs-with-tools">
      <div className="tab-group">
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
          <div key={t} className={`tab${tab === t ? ' on' : ''}`} onClick={() => setTab(t)}>{TAB_LABELS[t]}</div>
        ))}
      </div>
      {tab === 'models' && (
        <div className="tab-group tab-group-end tab-group-sub" role="tablist" aria-label="Model sources">
          {MODELS_SUB_TABS.map(([key, label]) => (
            <button key={key} type="button" role="tab" aria-selected={modelsSub === key}
              className={`tab${modelsSub === key ? ' on' : ''}`} onClick={() => setModelsSub(key)}>{label}</button>
          ))}
        </div>
      )}
    </div>
  );
  return (
    <div className="wrap">
      <PageHead title="Integrations" eyebrow="Build"
        subtitle="External model providers, tools, and data sources — connecting one here registers it into this workspace's UCS Catalog (the Model / Agent catalog engines, §7.4) so builder nodes can pick from it." />
      {tab !== 'knowledgebase' && tabStrip}
      {tab === 'models' && <ModelsTab sub={modelsSub} />}
      {tab === 'tools' && <ToolsTab />}
      {tab === 'mcp' && <McpTab />}
      {tab === 'datasources' && <DataSourcesTab />}
      {tab === 'knowledgebase' && <KnowledgeBaseTab tabStrip={tabStrip} />}
    </div>
  );
}
