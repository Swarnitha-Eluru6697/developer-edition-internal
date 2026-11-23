import { useEffect, useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import SearchSelect from './SearchSelect';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, AlertDestination, AlertDestinationIn } from '../types';

const KINDS: { value: 'slack' | 'teams'; label: string; icon: string; desc: string }[] = [
  { value: 'slack', label: 'Slack', icon: 'i-comment', desc: 'An Incoming Webhook URL from hooks.slack.com.' },
  { value: 'teams', label: 'Microsoft Teams', icon: 'i-user', desc: 'An Incoming Webhook connector URL from the channel.' },
];

const SEVS: { value: 'low' | 'medium' | 'high'; label: string; hint: string }[] = [
  { value: 'low', label: 'Low and above', hint: 'Everything' },
  { value: 'medium', label: 'Medium and above', hint: 'Skips low-severity notices' },
  { value: 'high', label: 'High only', hint: 'Just the ones worth waking someone for' },
];

export default function AlertDestinationModal({
  existing, onClose, onSaved,
}: { existing?: AlertDestination; onClose: () => void; onSaved: () => void }) {
  const { toast } = useApp();
  const [name, setName] = useState(existing?.name || '');
  const [kind, setKind] = useState<'slack' | 'teams'>(existing?.kind || 'slack');
  const [webhookUrl, setWebhookUrl] = useState('');
  const [minSeverity, setMinSeverity] = useState<'low' | 'medium' | 'high'>(existing?.min_severity || 'low');
  const [agentId, setAgentId] = useState<string>(existing?.agent_id || '');
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentQuery, setAgentQuery] = useState('');
  const [saving, setSaving] = useState(false);

  // listAgents returns only the first page (100 rows, ordered by an
  // opaque id), which showed an arbitrary slice of agents in this
  // dropdown. Snippets are excluded: they are flow fragments, not
  // something that raises alerts.
  useEffect(() => {
    api.listAllAgents()
      .then((all) => setAgents(all.filter((a) => a.plane !== 'Snippet')
        .sort((a, b) => a.name.localeCompare(b.name))))
      .catch(() => {});
  }, []);

  const q = agentQuery.trim().toLowerCase();
  const matching = q
    ? agents.filter((a) => a.name.toLowerCase().includes(q) || a.id.toLowerCase().includes(q))
    : agents;
  // The chosen agent always stays in the list. Without this, filtering it
  // out leaves the select showing blank while the state still holds its
  // id, so the field would lie about what is about to be saved.
  const chosen = agentId ? agents.find((a) => a.id === agentId) : undefined;
  const shown = chosen && !matching.some((a) => a.id === chosen.id)
    ? [chosen, ...matching]
    : matching;

  // On a new destination the URL is required. On an edit it is optional
  // and blank means "keep the existing one" — the API never returns it,
  // so there is nothing to pre-fill and clearing it on every edit would
  // silently disable the channel.
  const urlRequired = !existing;
  const canSave = name.trim().length > 0 && (!urlRequired || webhookUrl.trim().length > 0);

  const save = () => {
    const body: AlertDestinationIn = {
      name: name.trim(), kind, enabled, min_severity: minSeverity,
      agent_id: agentId || null,
      ...(webhookUrl.trim() ? { webhook_url: webhookUrl.trim() } : {}),
    };
    setSaving(true);
    const call = existing
      ? api.updateAlertDestination(existing.id, body)
      : api.createAlertDestination(body);
    call
      .then(() => { toast(existing ? 'Destination updated' : 'Destination added'); onSaved(); onClose(); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal
      title={existing ? `Edit ${existing.name}` : 'Add alert channel'}
      sub="Alerts matching the filters below are posted to this channel. Everything else is left alone."
      onClose={onClose}
      footer={<>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn" onClick={save} disabled={!canSave || saving}>
          {saving ? 'Saving…' : existing ? 'Save changes' : 'Add channel'}
        </button>
      </>}
    >
      <div className="field">
        <label>Channel type</label>
        <div className="provider-grid">
          {KINDS.map((k) => (
            <div key={k.value} className={`provider-card${kind === k.value ? ' on' : ''}`} onClick={() => setKind(k.value)}>
              <Icon name={k.icon} /><div className="small">{k.label}</div>
            </div>
          ))}
        </div>
        <p className="small muted" style={{ margin: '6px 0 0' }}>{KINDS.find((k) => k.value === kind)?.desc}</p>
      </div>

      <div className="field">
        <label>Name</label>
        <input value={name} onChange={(e) => setName(e.target.value)}
          placeholder="e.g. acme-oncall" />
        <p className="small muted" style={{ margin: '6px 0 0' }}>How this channel is identified on this page. Must be unique.</p>
      </div>

      <div className="field">
        <label>Webhook URL {urlRequired ? '' : <span className="muted small">(leave blank to keep the current one)</span>}</label>
        <input className="mono" value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)}
          placeholder={kind === 'slack'
            ? 'https://hooks.slack.com/services/T000/B000/XXXX'
            : 'https://outlook.office.com/webhook/…'} />
        {existing && (
          <p className="small muted" style={{ margin: '6px 0 0' }}>
            Currently <span className="mono">{existing.url_hint || '—'}</span>. The full URL is never shown
            again after it is saved — anyone holding it can post into your channel.
          </p>
        )}
        {!existing && (
          <p className="small muted" style={{ margin: '6px 0 0' }}>
            Must be <span className="mono">https://</span>. Stored encrypted and never returned by the API.
          </p>
        )}
      </div>

      <div className="field">
        <label id="alert-dest-sev-label">Send alerts of severity</label>
        <SearchSelect labelledBy="alert-dest-sev-label" noun="severity" value={minSeverity}
          options={SEVS.map((s) => ({ value: s.value, label: s.label, hint: s.hint }))}
          onChange={(v) => setMinSeverity(v as 'low' | 'medium' | 'high')} />
      </div>

      <div className="field">
        <label id="alert-dest-agent-label">For agent</label>
        <input value={agentQuery} onChange={(e) => setAgentQuery(e.target.value)}
          placeholder="Filter agents by name…" style={{ marginBottom: 6 }} />
        {/* Several agents share a name, so the id is what tells them apart. */}
        <SearchSelect labelledBy="alert-dest-agent-label" noun="agent" value={agentId}
          options={[
            { value: '', label: 'All agents' },
            ...shown.map((a) => ({
              value: a.id,
              label: `${a.name} · ${a.id}${a.status === 'published' ? '' : ` (${a.status})`}`,
            })),
          ]}
          onChange={(v) => setAgentId(v)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          {agents.length > 0 && (
            <>Showing {shown.length} of {agents.length} agents. </>
          )}
          Pick one to send only its alerts here. Other agents&rsquo; alerts will not reach this channel.
        </p>
      </div>

      <div className="row-split" style={{ marginTop: 4 }}>
        <div>
          <b className="small">Enabled</b>
          <p className="small muted" style={{ margin: '2px 0 0' }}>Turn off to stop delivery without losing the configuration.</p>
        </div>
        <label className="switch">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span />
        </label>
      </div>
    </Modal>
  );
}
