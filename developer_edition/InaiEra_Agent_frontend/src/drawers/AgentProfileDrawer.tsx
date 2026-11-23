import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, BASE } from '../api';
import Drawer from '../components/Drawer';
import Icon from '../components/Icon';
import SearchSelect from '../components/SearchSelect';
import { HealthChip, StatusChip, TierChip, AGENT_PRIVILEGE_LABEL, SecHead, KV, Empty, Banner, fmtMetric } from '../components/ui';
import { useApp } from '../context/AppContext';
import { P } from '../permissions';
import type { Agent, Role, AgentLog, Trace, AgentLineage, AgentQuality, AgentLiveJob } from '../types';

const TABS = ['overview', 'api access', 'quality', 'lineage', 'entitlements', 'usage', 'logs', 'monitoring', 'traces', 'live jobs'];
// Seconds into something a person can read at a glance; a run that has
// been going for 43 hours should not print as "155716".
function fmtDuration(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

const DATA_TABS = ['usage', 'logs', 'monitoring', 'traces', 'live jobs'];

export default function AgentProfileDrawer({ agentId }: { agentId: string }) {
  const { closeDrawer, toast, openDrawer, can } = useApp();
  const navigate = useNavigate();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tab, setTab] = useState('overview');
  const [roles, setRoles] = useState<Role[]>([]);
  const [logs, setLogs] = useState<AgentLog[]>([]);
  const [logSearch, setLogSearch] = useState('');
  const [logLevelFilter, setLogLevelFilter] = useState('all');
  const [traces, setTraces] = useState<Trace[]>([]);
  const [metrics, setMetrics] = useState<Record<string, string | number | null> | null>(null);
  // Loaded only when their tab is opened: each is a call out to UCS, and
  // paying for both on every drawer open would slow the common case for
  // panels most people never look at.
  const [lineage, setLineage] = useState<AgentLineage | null>(null);
  const [quality, setQuality] = useState<AgentQuality | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [liveJobs, setLiveJobs] = useState<AgentLiveJob[] | null>(null);

  useEffect(() => {
    api.getAgent(agentId).then(setAgent).catch(() => {});
    api.getAgentRoles(agentId).then(setRoles).catch(() => {});
    api.getAgentLogs(agentId).then(setLogs).catch(() => {});
    api.getAgentTraces(agentId).then(setTraces).catch(() => {});
    // These used to read agent.metrics, a JSON column nothing writes
    // outside seed.py's hardcoded fixture — so this panel showed
    // fabricated numbers for the seeded demo agents and blanks for
    // every real one. Same source as the Monitoring page now, so the
    // two cannot disagree about the same agent.
    api.getMetrics(agentId).then((r) => setMetrics(r.metrics)).catch(() => setMetrics({}));
    setLineage(null);
    setQuality(null);
    setLiveJobs(null);
  }, [agentId]);

  // Fetched when the tab is first opened, and kept until the drawer
  // moves to another agent.
  useEffect(() => {
    if (tab === 'lineage' && !lineage) api.getAgentLineage(agentId).then(setLineage).catch(() => {});
    if (tab === 'quality' && !quality) api.getAgentQuality(agentId).then(setQuality).catch(() => {});
    if (tab === 'live jobs' && !liveJobs) {
      api.getAgentLiveJobs(agentId).then((r) => setLiveJobs(r.jobs)).catch(() => setLiveJobs([]));
    }
  }, [tab, agentId, lineage, quality, liveJobs]);

  if (!agent) return null;
  const m = metrics || {};
  const gated = agent.status === 'draft' && DATA_TABS.includes(tab);
  const filteredLogs = logs
    .filter((l) => logLevelFilter === 'all' || l.level === logLevelFilter)
    .filter((l) => !logSearch.trim() || l.msg.toLowerCase().includes(logSearch.trim().toLowerCase()));

  return (
    <Drawer title={`Agent · ${agent.name}`} onClose={closeDrawer} footer={
      <>
        <button className="btn ghost" onClick={closeDrawer}>Close</button>
        {agent.type === 'App' && agent.status === 'published' && (
          <a className="btn ghost" href={`#/app/${agent.id}`} target="_blank" rel="noreferrer">
            <Icon name="i-flow" /> Open app
          </a>
        )}
        <button className="btn" onClick={() => { closeDrawer(); navigate(`/builder?agent=${agent.id}`); }}>Open in builder</button>
      </>
    }>
      {/* Status, tabs and the active tab's search/filter stay pinned while
          the tab's content scrolls beneath them. */}
      <div className="drawer-sticky">
        <div className="row-split"><StatusChip status={agent.status} /><span className="fqn">{agent.owner} / {agent.version}</span></div>
        <div className="tabs" style={{ marginTop: 16, marginBottom: 0 }}>
          {TABS.map((t) => (
            <div key={t} className={`tab${tab === t ? ' on' : ''}`} onClick={() => setTab(t)}>
              {t.split(' ').map((w) => w[0].toUpperCase() + w.slice(1)).join(' ')}
            </div>
          ))}
        </div>
        {!gated && tab === 'logs' && (
          <div style={{ paddingTop: 16 }}>
            <p className="small muted" style={{ marginTop: 0 }}>Structured logs per run, correlated to trace ID (§6.3). Click a line to open its trace.</p>
            <div className="row-split" style={{ gap: 8 }}>
              <div className="cmd" style={{ flex: 1 }}>
                <Icon name="i-search" /><input placeholder="Search logs…" value={logSearch} onChange={(e) => setLogSearch(e.target.value)} />
              </div>
              <SearchSelect ariaLabel="Log level" noun="level" value={logLevelFilter}
                style={{ width: 140, flex: 'none' }}
                options={[
                  { value: 'all', label: 'All levels' },
                  { value: 'INFO', label: 'Info' },
                  { value: 'WARN', label: 'Warn' },
                  { value: 'ERROR', label: 'Error' },
                ]}
                onChange={(v) => setLogLevelFilter(v)} />
            </div>
          </div>
        )}
      </div>

      {gated && <Empty icon="i-play">This agent hasn't been published yet — {tab} populates after the first run.</Empty>}

      {!gated && tab === 'overview' && (
        <>
          <KV k="Type" v={`${agent.type} · ${agent.subtype}`} />
          <KV k="Plane" v={agent.plane} />
          <KV k="Owner" v={agent.owner} />
          <KV k="Version" v={<span className="mono">{agent.version}</span>} />
          <KV k="Status" v={<StatusChip status={agent.status} />} />
          <KV k="Model" v={<span className="mono">{agent.model}</span>} />
          <KV k="Mode" v={agent.mode} />
        </>
      )}

      {!gated && tab === 'api access' && (
        <ApiAccessTab agent={agent} toast={toast} />
      )}

      {!gated && tab === 'quality' && (
        <>
          {!quality && <p className="small muted">Measuring…</p>}
          {quality && (
            <>
              <div className="gauge">
                <div className={`ring ${quality.status === 'pass' ? 'pass' : 'warn'}`}
                  style={{ ['--pct' as any]: Math.round(quality.score ?? 0) }}>
                  <span>{quality.score === null ? '—' : Math.round(quality.score)}</span>
                </div>
                <div>
                  <b>Agent quality</b>
                  <div className="small muted">
                    Measured from this agent&rsquo;s own eval runs and its last{' '}
                    {quality.window_days} days of runs
                  </div>
                </div>
              </div>
              <div className="dims">
                {Object.entries(quality.dims).map(([name, d]) => (
                  <div className="dim" key={name}>
                    <div className="nm">{name}</div>
                    <div className="st">
                      <span className={`chip ${d.verdict === 'pass' ? 'green'
                        : d.verdict === 'warn' ? 'amber'
                        : d.verdict === 'fail' ? 'red' : 'line'}`}>
                        {/* An unmeasured dimension says so rather than
                            showing 0, which would read as failing. */}
                        {d.observed !== null ? `${d.observed}%` : d.verdict}
                      </span>
                    </div>
                    <div className="small muted">{d.detail}</div>
                  </div>
                ))}
              </div>
              <div className="row-split" style={{ marginTop: 14 }}>
                <span className="small muted">
                  Published to UCS as <span className="mono">{quality.fqn}</span>
                </span>
                {can(P.agentsPublish) && <button className="btn sm ghost" disabled={publishing}
                  onClick={() => {
                    setPublishing(true);
                    api.publishAgentCatalog(agentId)
                      .then((r) => toast(r.failed.length
                        ? `Published ${r.sent.length}, ${r.failed.length} failed`
                        : `Published ${r.sent.join(', ')} to UCS`))
                      .catch((e) => toast(e.message))
                      .finally(() => setPublishing(false));
                  }}>
                  <Icon name="i-link" /> {publishing ? 'Publishing…' : 'Publish to UCS'}
                </button>}
              </div>
            </>
          )}
        </>
      )}

      {!gated && tab === 'lineage' && (
        <>
          {!lineage && <p className="small muted">Reading the flow…</p>}
          {lineage && (
            <>
              <SecHead style={{ marginTop: 0 }}>Upstream (reads)</SecHead>
              {lineage.upstream.length ? lineage.upstream.map((u) => (
                <div className="chain" key={u.fqn}>
                  <div className="hop data">
                    <b>{u.name}</b>
                    <div className="mono small muted">{u.fqn}</div>
                  </div>
                  <div className="arrow">&rarr;</div>
                  <div className="hop agent"><b>{agent.name}</b></div>
                </div>
              )) : (
                <p className="small muted">
                  This agent declares no knowledge bases, so it reads nothing catalogued.
                </p>
              )}

              {!!lineage.runs.length && (
                <>
                  <SecHead>Recent runs</SecHead>
                  {lineage.runs.map((r) => (
                    <div className="row-split" key={r.id}
                      style={{ padding: '7px 0', borderBottom: '1px solid var(--line-2)' }}>
                      <span className="mono small">{r.id}</span>
                      <span className={`chip ${r.status === 'success' ? 'green' : 'red'}`}>{r.status}</span>
                      <span className="mono small muted">
                        {r.ts ? String(r.ts).replace('T', ' ').slice(0, 19) : '—'}
                      </span>
                    </div>
                  ))}
                </>
              )}
              <p className="small muted" style={{ marginTop: 14 }}>
                Each run publishes this to UCS as an OpenLineage event &middot;{' '}
                <span className="mono">{lineage.fqn}</span>
              </p>
            </>
          )}
        </>
      )}

      {!gated && tab === 'entitlements' && (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>Read-only here — this agent is a UCS Catalog asset (§7.4), so bindings are administered in UCS's own RBAC &amp; policy console, not duplicated in this view.</p>
          <SecHead style={{ marginTop: 0 }}>Bound roles</SecHead>
          {roles.length ? roles.map((r) => (
            <div className="row-split" key={r.id} style={{ padding: '8px 0', borderBottom: '1px solid var(--line-2)' }}>
              <TierChip tier={r.tier} label={AGENT_PRIVILEGE_LABEL[r.tier]} /><span className="mono small">{r.group}</span>
            </div>
          )) : <span className="small muted">No bindings yet</span>}
          <button className="btn sm ghost" style={{ marginTop: 12 }} onClick={() => toast('This is managed in UCS Catalog — a separate console')}>
            <Icon name="i-cog" /> Manage in UCS Catalog
          </button>
        </>
      )}

      {!gated && tab === 'usage' && (
        <>
          <KV k="Runs (7d)" v={agent.runs_7d} />
          <KV k="Cost (7d)" v={<span className="mono">{agent.cost_7d}</span>} />
          <SecHead>Recent invocations</SecHead>
          {logs.slice(0, 3).map((l) => (
            <div className="row-split" key={l.id} style={{ padding: '7px 0', borderBottom: '1px solid var(--line-2)' }}>
              <span className="mono small">{l.ts}</span><span className="small">{l.msg}</span>
            </div>
          ))}
        </>
      )}

      {!gated && tab === 'logs' && (
        <>
          {filteredLogs.length ? filteredLogs.map((l) => (
            <div className="logline click" key={l.id} onClick={() => { /* could switch drawer to trace */ }}>
              <span className={`lvl ${l.level.toLowerCase()}`}>{l.level}</span><span className="ts">{l.ts}</span><span className="msg">{l.msg}</span>
            </div>
          )) : <Empty icon="i-scroll">{logs.length ? 'No logs match your search' : 'No recent logs'}</Empty>}
        </>
      )}

      {!gated && tab === 'monitoring' && (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>Scoped to <b>{agent.name}</b> only — same metrics catalog as the platform Monitoring page (§6.2), filtered to this agent.</p>
          <SecHead style={{ marginTop: 0 }}>Run-level</SecHead>
          <KV k="Success rate" v={fmtMetric(m.success, '%')} />
          <KV k="Latency p50 / p95" v={<span className="mono">{fmtMetric(m.p50)} / {fmtMetric(m.p95)}</span>} />
          <KV k="Cost per run (avg)" v={<span className="mono">{fmtMetric(m.costAvg)}</span>} />
          <KV k="Retries per run" v={fmtMetric(m.retries)} />
          <SecHead>Harness-level</SecHead>
          <KV k="Loop iterations / run" v={fmtMetric(m.loopIter)} />
          <KV k="Step-limit hits" v={fmtMetric(m.stepLimit, '%')} />
          <KV k="Guardrail triggers" v={fmtMetric(m.guardrail, '%')} />
          <SecHead>UCS / governance-level</SecHead>
          <KV k="Entitlement pass rate" v={fmtMetric(m.entPass, '%')} />
          <KV k="Avg chain depth" v={fmtMetric(m.chainDepth)} />
          <KV k="Entitlement-plan cache hit" v={fmtMetric(m.cacheHit)} />
          <button className="btn sm ghost" style={{ marginTop: 14 }} onClick={() => { closeDrawer(); navigate('/monitoring'); }}>
            <Icon name="i-chart" /> Open platform Monitoring
          </button>
        </>
      )}

      {!gated && tab === 'traces' && (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>Traces for <b>{agent.name}</b> only.</p>
          {traces.length ? traces.map((t) => (
            <div className="row-split click" key={t.id} style={{ padding: '9px 0', borderBottom: '1px solid var(--line-2)', cursor: 'pointer' }}
              onClick={() => openDrawer('traceDetail', t.id)}>
              <div><span className="mono small">{t.id}</span><div className="small muted">{t.spans} spans · {t.duration}</div></div>
              <span className="mono small muted">{t.ts}</span>
            </div>
          )) : <span className="small muted">No traces for this agent in the current window</span>}
          <button className="btn sm ghost" style={{ marginTop: 14 }} onClick={() => { closeDrawer(); navigate('/traces'); }}>
            <Icon name="i-branch" /> Open platform Traces
          </button>
        </>
      )}

      {!gated && tab === 'live jobs' && (
        <>
          {!liveJobs && <p className="small muted">Checking…</p>}
          {liveJobs?.length === 0 && <Empty icon="i-check">Nothing running right now</Empty>}
          {liveJobs?.map((j) => {
            // A run going for hours is stuck, not busy. The tab used to
            // report "no live jobs" regardless, which hid exactly this.
            const stale = (j.running_for_s ?? 0) > 3600;
            return (
              <div className="row-split" key={j.id}
                style={{ padding: '9px 0', borderBottom: '1px solid var(--line-2)' }}>
                <div className="row-split" style={{ gap: 8, justifyContent: 'flex-start' }}>
                  <span className={`chip ${j.status === 'paused' ? 'amber' : 'blue'}`}>{j.status}</span>
                  <span className="mono small">{j.id}</span>
                  {stale && <span className="chip red">stuck?</span>}
                </div>
                <span className="small muted">
                  {j.running_for_s === null ? '—' : fmtDuration(j.running_for_s)}
                  {j.invoking_user ? ` · ${j.invoking_user}` : ''}
                </span>
              </div>
            );
          })}
        </>
      )}
    </Drawer>
  );
}

function ApiAccessTab({ agent, toast }: { agent: Agent; toast: (t: string) => void }) {
  const [mcpAgent, setMcpAgent] = useState(agent);
  const [regenerating, setRegenerating] = useState(false);
  const uiUrl = `${window.location.origin}${window.location.pathname}#/app/${mcpAgent.id}`;
  const endpoint = `${BASE}/api/agents/${mcpAgent.id}/run`;
  const curl = `curl -X POST '${endpoint}' \\
  -H 'Content-Type: application/json' \\
  -d '{"input": "your message here", "invoking_user": "user:you"}'`;
  const sampleResponse = `{
  "run_id": "r-live-a1b2c3d4",
  "trace_id": "tr-live-e5f6g7h8",
  "execution_mode": "sync (direct harness call, no Temporal)",
  "status": "success",
  "output": "...",
  "spans": [ { "node": "trigger", "status": "ok" }, ... ]
}`;

  const copy = (text: string, label: string) => {
    navigator.clipboard.writeText(text).then(
      () => toast(`${label} copied`),
      () => toast('Could not copy — your browser blocked clipboard access'),
    );
  };

  // §0.101, GAP_REPORT — the real MCP credential now lives on real,
  // top-level Agent fields (mcp_enabled/mcp_token_encrypted, the
  // latter always masked in a real GET response), not
  // features["mcp"] — reading the OLD shape here would show nothing
  // at all after a real page refresh, since the backend genuinely
  // never returns that key anymore. mcpUrl (the real, PLAINTEXT url)
  // is real, local-only state, populated just-in-time from an actual
  // enable/regenerate response — the same real, honest "shown once"
  // behavior the backend's own encryption-at-rest already implies
  // (a masked, encrypted value is never a real url).
  const [mcpEnabled, setMcpEnabled] = useState(mcpAgent.mcp_enabled ?? false);
  const [mcpUrl, setMcpUrl] = useState<string | null>(null);

  const toggleMcp = (enabled: boolean) => {
    api.setMcp(mcpAgent.id, enabled).then((r) => {
      setMcpEnabled(r.enabled);
      if (r.url) setMcpUrl(`${BASE}${r.url}`);
      toast(enabled ? 'MCP Server enabled' : 'MCP Server disabled');
    }).catch((e) => toast(e.message));
  };

  const regenerate = () => {
    setRegenerating(true);
    api.regenerateMcpToken(mcpAgent.id).then((r) => {
      setMcpEnabled(r.enabled);
      setMcpUrl(`${BASE}${r.url}`);
      toast('MCP Server URL regenerated — the old URL no longer works');
    }).catch((e) => toast(e.message)).finally(() => setRegenerating(false));
  };

  const cursorConfig = `{
  "mcpServers": {
    "${mcpAgent.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}": {
      "url": "${mcpUrl || 'enable MCP Server above first'}"
    }
  }
}`;

  return (
    <>
      <p className="small muted" style={{ marginTop: 0 }}>
        Every agent is reachable two real ways — a dedicated UI URL for people, and a real REST endpoint for
        programmatic access. Both call the exact same execution path this platform's own Builder and Try It use;
        there's no separate "public API" code path that could drift from what you tested.
      </p>

      <SecHead style={{ marginTop: 0 }}>Dedicated UI URL</SecHead>
      {agent.type === 'App' && agent.status !== 'published'
        ? <Banner kind="info">This agent isn't published yet — the URL below will show an honest "not published" message until it is. It works for anyone once you publish, no separate step needed.</Banner>
        : null}
      <div className="row-split" style={{ gap: 8 }}>
        <input className="mono small" readOnly value={uiUrl} style={{ flex: 1 }} onFocus={(e) => e.target.select()} />
        <button className="btn sm ghost" onClick={() => copy(uiUrl, 'URL')}><Icon name="i-copy" /> Copy</button>
        <a className="btn sm ghost" href={`#/app/${mcpAgent.id}`} target="_blank" rel="noreferrer"><Icon name="i-flow" /> Open</a>
      </div>

      <SecHead>REST endpoint</SecHead>
      <p className="small muted" style={{ marginTop: 0 }}>
        Real right now, regardless of publish status — the same endpoint the Builder's Try It panel calls.
        This reference implementation has no auth layer yet (a real, documented platform gap, not hidden here):
        this call is genuinely unauthenticated as shown.
      </p>
      <div className="row-split" style={{ gap: 8, marginBottom: 10 }}>
        <span className="chip line mono">POST</span>
        <input className="mono small" readOnly value={endpoint} style={{ flex: 1 }} onFocus={(e) => e.target.select()} />
        <button className="btn sm ghost" onClick={() => copy(endpoint, 'Endpoint')}><Icon name="i-copy" /> Copy</button>
      </div>

      <div className="field">
        <label>Example request</label>
        <pre className="codeblock">{curl}</pre>
        <button className="btn sm ghost" onClick={() => copy(curl, 'curl command')}><Icon name="i-copy" /> Copy curl</button>
      </div>

      <div className="field">
        <label>Example response</label>
        <pre className="codeblock">{sampleResponse}</pre>
      </div>

      <SecHead>MCP Server</SecHead>
      <p className="small muted" style={{ marginTop: 0 }}>
        Expose this agent as a real <span className="mono">tools/call</span> target for MCP clients like Claude
        Desktop and Cursor — a genuine JSON-RPC endpoint (initialize / tools/list / tools/call), not a static
        toggle. Off by default; enabling generates a real, unique URL. Treat it like an API key — anyone with it
        can call this agent.
      </p>
      <div className="row-split" style={{ padding: '8px 0' }}>
        <span className="small">Enable MCP Server</span>
        <div className={`tog${mcpEnabled ? ' on' : ''}`} onClick={() => toggleMcp(!mcpEnabled)} />
      </div>
      {mcpEnabled && !mcpUrl && (
        // §0.101, GAP_REPORT — a real, necessary fix: this agent's own
        // MCP credential is genuinely enabled (persisted, real, top-
        // level Agent state), but the real, plaintext URL isn't known
        // to THIS page load — it's never recoverable from a plain GET
        // (mcp_token_encrypted is always masked there, the same real
        // reason a password reset, not a password reveal, is the only
        // real option once a real credential is only ever shown once).
        // A person can still always get a real, fresh, visible one
        // here — never silently stuck with no way to see or use their
        // own, already-enabled MCP endpoint again.
        <div className="field">
          <p className="small muted">
            MCP Server is enabled, but its real URL was only shown once, when it was created — regenerate to get a
            new one you can see and copy.
          </p>
          <button className="btn sm ghost" onClick={regenerate} disabled={regenerating}><Icon name="i-refresh" /> {regenerating ? 'Regenerating…' : 'Regenerate URL'}</button>
        </div>
      )}
      {mcpEnabled && mcpUrl && (
        <>
          <div className="row-split" style={{ gap: 8, marginTop: 10 }}>
            <input className="mono small" readOnly value={mcpUrl} style={{ flex: 1 }} onFocus={(e) => e.target.select()} />
            <button className="btn sm ghost" onClick={() => copy(mcpUrl, 'MCP Server URL')}><Icon name="i-copy" /> Copy</button>
            <button className="btn sm ghost" onClick={regenerate} disabled={regenerating}><Icon name="i-refresh" /> {regenerating ? 'Regenerating…' : 'Regenerate'}</button>
          </div>
          <div className="field">
            <label>Cursor — .cursor/mcp.json</label>
            <pre className="codeblock">{cursorConfig}</pre>
            <button className="btn sm ghost" onClick={() => copy(cursorConfig, 'Cursor config')}><Icon name="i-copy" /> Copy config</button>
          </div>
          <p className="small muted">For Claude Desktop: Profile → Settings → Integrations → Add integration, and paste this same URL.</p>
        </>
      )}
    </>
  );
}
