import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import Icon from '../components/Icon';
import Modal from '../components/Modal';
import { PageHead, Chip, Banner } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { GuardrailAgentRow, GuardrailsOverview } from '../types';

type ModerationProvider = 'keywords' | 'custom_endpoint';

function ModerationConfigModal({ row, onClose, onSaved }: {
  row: GuardrailAgentRow; onClose: () => void; onSaved: () => void;
}) {
  const { toast } = useApp();
  const [enabled, setEnabled] = useState(row.moderation.enabled);
  const [provider, setProvider] = useState<ModerationProvider>(
    row.moderation.provider === 'custom_endpoint' ? 'custom_endpoint' : 'keywords'
  );
  const [keywords, setKeywords] = useState('');
  const [endpointUrl, setEndpointUrl] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getAgent(row.id).then((full) => {
      const mod = full.features?.moderation as any;
      if (mod?.keywords) setKeywords((mod.keywords as string[]).join(', '));
      if (mod?.custom_endpoint_url) setEndpointUrl(mod.custom_endpoint_url);
    }).catch(() => {});
  }, [row.id]);

  const save = () => {
    setSaving(true);
    const features: any = {
      moderation: {
        enabled, provider,
        keywords: provider === 'keywords' ? keywords.split(',').map((k) => k.trim()).filter(Boolean) : undefined,
        custom_endpoint_url: provider === 'custom_endpoint' ? endpointUrl.trim() : undefined,
      },
    };
    api.setAgentFeatures(row.id, features)
      .then(() => { toast(`Guardrails updated for ${row.name}`); onSaved(); onClose(); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal
      title={`Content moderation — ${row.name}`}
      sub="A real match genuinely stops the flow before it runs (input) or before the reply reaches the user (output) — not decorative."
      onClose={onClose}
      footer={<>
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn" onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
      </>}
    >
      <div className="field">
        <label className="row-split"><span>Enabled</span>
          <label className="switch"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /><span /></label>
        </label>
      </div>
      <div className="field">
        <label>Provider</label>
        <div className="trigger-type-list">
          <div className={`trigger-type-row${provider === 'keywords' ? ' sel' : ''}`} onClick={() => setProvider('keywords')}>
            <span className="tt-label">Keywords — a real, deterministic block-list check</span>
          </div>
          <div className={`trigger-type-row${provider === 'custom_endpoint' ? ' sel' : ''}`} onClick={() => setProvider('custom_endpoint')}>
            <span className="tt-label">Custom endpoint — a real HTTP POST to your own moderation service</span>
          </div>
        </div>
      </div>
      {provider === 'keywords' && (
        <div className="field">
          <label>Blocked keywords (comma-separated)</label>
          <input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="e.g. password, ssn, confidential" />
        </div>
      )}
      {provider === 'custom_endpoint' && (
        <div className="field">
          <label>Endpoint URL</label>
          <input value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} placeholder="https://your-service.example.com/moderate" />
          <div className="small muted" style={{ marginTop: 4 }}>Called as a real POST with {'{"text": "..."}'}; expects {'{"flagged": bool, "reason": "..."}'} back.</div>
        </div>
      )}
    </Modal>
  );
}

export default function Guardrails() {
  const { toast } = useApp();
  const [data, setData] = useState<GuardrailsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [moderationModalRow, setModerationModalRow] = useState<GuardrailAgentRow | null>(null);
  const [savingAuthId, setSavingAuthId] = useState<string | null>(null);

  const load = () => {
    api.getGuardrailsOverview().then(setData).catch((e) => setError(e.message));
  };
  useEffect(load, []);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    if (!q) return data.agents;
    return data.agents.filter((a) => a.name.toLowerCase().includes(q));
  }, [data, search]);

  const moderationOn = filtered.filter((a) => a.moderation.enabled).length;
  const authOn = filtered.filter((a) => a.require_authenticated_user).length;
  const ucsAgents = filtered.filter((a) => a.ucs.touches_checked_objects);
  const customCapAgents = filtered.filter((a) =>
    a.safety_caps.llm_agent_nodes_with_custom_step_limit > 0 || a.safety_caps.loop_nodes_with_custom_max_loops > 0
  );

  const toggleAuth = (row: GuardrailAgentRow) => {
    setSavingAuthId(row.id);
    api.setAgentFeatures(row.id, { require_authenticated_user: !row.require_authenticated_user } as any)
      .then(() => { toast(`${!row.require_authenticated_user ? 'Enabled' : 'Disabled'} required authentication for ${row.name}`); load(); })
      .catch((e) => toast(e.message))
      .finally(() => setSavingAuthId(null));
  };

  if (error) {
    return (
      <div className="wrap">
        <PageHead title="Guardrails" eyebrow="Platform" subtitle="Real, enforced controls on what an agent can do and who can run it." />
        <div className="card"><div className="card-b small" style={{ color: 'var(--red)' }}>Couldn't load guardrails: {error}</div></div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="wrap">
        <PageHead title="Guardrails" eyebrow="Platform" subtitle="Real, enforced controls on what an agent can do and who can run it." />
        <div className="small muted">Loading…</div>
      </div>
    );
  }

  const c = data.platform_constants;

  return (
    <div className="wrap">
      <PageHead
        title="Guardrails" eyebrow="Platform"
        subtitle="Real, enforced controls on what an agent can do and who can run it — not a policy document."
        actions={<input className="text-input" placeholder="Search agents…" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 240 }} />}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3><Icon name="i-shield" /> Content Moderation</h3><Chip cls={moderationOn > 0 ? 'green' : 'line'}>{moderationOn} of {filtered.length} enabled</Chip></div>
        <div className="card-b">
          {(() => {
            const inert = (data?.agents ?? []).filter(
              (r) => r.moderation.enabled && !r.moderation.effective);
            return inert.length ? (
              <Banner kind="warn">
                <b>{inert.length} agent{inert.length === 1 ? '' : 's'} have moderation
                switched on but nothing behind it</b> — with no provider chosen the check
                never matches, so those agents block nothing. Open the row to pick one.
              </Banner>
            ) : null;
          })()}
          <p className="small muted" style={{ marginTop: 0 }}>Define per agent below — Keywords (a real, deterministic block-list) or Custom Endpoint (a real HTTP call to your own moderation service). A real match genuinely stops the flow, on input and/or output independently.</p>
          <div className="tbl-scroll" style={{ maxHeight: 360, overflowY: 'auto' }}>
            {filtered.slice(0, 200).map((row) => (
              <div className="row-split" key={row.id} style={{ padding: '9px 0', borderBottom: '1px solid var(--line-2)' }}>
                <div><b className="small">{row.name}</b><div className="small muted">{row.status} · {row.app_type || '—'}</div></div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {/* Green means it can actually block. An enabled config
                      with no provider blocks nothing — showing that as
                      "on" reported protection that was not there. */}
                  <Chip cls={row.moderation.effective ? 'green'
                    : row.moderation.enabled ? 'amber' : 'line'}>
                    {row.moderation.effective ? (row.moderation.provider || 'on')
                      : row.moderation.enabled ? 'on · inert' : 'off'}
                  </Chip>
                  {row.moderation.enabled && !row.moderation.effective && (
                    <div className="small muted" style={{ marginTop: 4 }}>
                      {row.moderation.reason}
                    </div>
                  )}
                  <button className="btn ghost sm" onClick={() => setModerationModalRow(row)}><Icon name="i-cog" /> Configure</button>
                </div>
              </div>
            ))}
            {filtered.length > 200 && <div className="small muted" style={{ padding: '8px 0' }}>Showing the first 200 of {filtered.length} matching agents — narrow your search to see more.</div>}
            {filtered.length === 0 && <div className="small muted" style={{ padding: '8px 0' }}>No agents match "{search}".</div>}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3><Icon name="i-link" /> UCS Entitlement</h3><Chip cls="blue">{ucsAgents.length} agents checked</Chip></div>
        <div className="card-b">
          <p className="small muted" style={{ marginTop: 0 }}>
            Defined in UCS's own console, not here — access is granted or revoked externally, the real, single source of truth. Enforced as a real dual check on every Tool/Retrieval touch: both the agent's own principal <i>and</i> the invoking user must independently be allowed. A UCS outage fails the run loudly — never a silent local fallback.
          </p>
          {ucsAgents.length === 0 ? (
            <div className="small muted">No agents in the current search touch a UCS-checked catalog object.</div>
          ) : (
            <div className="tbl-scroll" style={{ maxHeight: 260, overflowY: 'auto' }}>
              {ucsAgents.slice(0, 100).map((row) => (
                <div className="row-split" key={row.id} style={{ padding: '8px 0', borderBottom: '1px solid var(--line-2)' }}>
                  <b className="small">{row.name}</b>
                  <span className="small muted">{row.ucs.checked_object_count} real catalog object{row.ucs.checked_object_count === 1 ? '' : 's'}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3><Icon name="i-clock" /> Runtime Safety Caps</h3><Chip cls="line">platform-wide</Chip></div>
        <div className="card-b">
          <p className="small muted" style={{ marginTop: 0 }}>Real, enforced limits against runaway execution — some are per-node settings a builder can tune, others are fixed platform constants, shown here from their actual source, not re-typed numbers that could drift.</p>
          <div className="kv"><span className="k">Agentic step limit (LLM/Agent nodes)</span><span className="v mono">{c.step_limit_default} default, per-node configurable</span></div>
          <div className="kv"><span className="k">Loop max iterations</span><span className="v mono">{c.max_loops_default} default, per-node configurable</span></div>
          <div className="kv"><span className="k">Max graph steps (cycle safety net)</span><span className="v mono">{c.max_graph_steps} — fixed, not configurable</span></div>
          <div className="kv"><span className="k">Sub-Agent hop cap</span><span className="v mono">{c.sub_agent_hop_cap} — fixed, shared with UCS's own chain_depth_cap</span></div>
          <div className="kv"><span className="k">Code node sandbox</span><span className="v mono">{c.code_sandbox_timeout_seconds}s timeout · {c.code_sandbox_memory_mb}MB memory · no network</span></div>
          <div className="kv"><span className="k">Chat history bound</span><span className="v mono">last {c.chat_history_max_turns} messages</span></div>
          <div className="small muted" style={{ marginTop: 10 }}>{customCapAgents.length} of {filtered.length} agents in the current search have a node with a non-default step/loop limit.</div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3><Icon name="i-check" /> Build/Publish-time Validation</h3><Chip cls="line">automatic</Chip></div>
        <div className="card-b">
          <p className="small muted" style={{ marginTop: 0 }}>Real, structural checks that run automatically in the Builder — a genuinely malformed flow can't be published without warning.</p>
          <ul className="small" style={{ margin: 0, paddingLeft: 18, color: 'var(--mute)' }}>
            <li>Every node has the real config its own type requires (e.g. a Condition node's expression, a Code node's source)</li>
            <li>No node is left disconnected from the rest of the flow</li>
            <li>A Workflow app ends with a real Output node</li>
            <li>A chat-type app (Chatbot/Chatflow/Agent) has at least one node that can actually produce a response</li>
          </ul>
        </div>
      </div>

      <div className="card">
        <div className="card-h"><h3><Icon name="i-key" /> Gateway-trust Authentication</h3><Chip cls={authOn > 0 ? 'green' : 'line'}>{authOn} of {filtered.length} required</Chip></div>
        <div className="card-b">
          <p className="small muted" style={{ marginTop: 0 }}>
            This backend never performs OIDC login itself — an upstream gateway verifies the person against your org's IdP and forwards a trusted identity header, which genuinely replaces any self-reported invoking_user. Toggle per agent below.
          </p>
          <div className="tbl-scroll" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {filtered.slice(0, 200).map((row) => (
              <div className="row-split" key={row.id} style={{ padding: '9px 0', borderBottom: '1px solid var(--line-2)' }}>
                <div><b className="small">{row.name}</b><div className="small muted">{row.status}</div></div>
                <label className="switch">
                  <input type="checkbox" checked={row.require_authenticated_user} disabled={savingAuthId === row.id} onChange={() => toggleAuth(row)} />
                  <span />
                </label>
              </div>
            ))}
            {filtered.length > 200 && <div className="small muted" style={{ padding: '8px 0' }}>Showing the first 200 of {filtered.length} matching agents — narrow your search to see more.</div>}
          </div>
        </div>
      </div>

      {moderationModalRow && (
        <ModerationConfigModal row={moderationModalRow} onClose={() => setModerationModalRow(null)} onSaved={load} />
      )}
    </div>
  );
}
