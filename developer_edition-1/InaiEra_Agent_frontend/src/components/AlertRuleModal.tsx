import { useEffect, useState } from 'react';
import Drawer from './Drawer';
import SearchSelect from './SearchSelect';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, AlertRule, AlertRuleIn, AlertRuleKind } from '../types';

const SEVS: ('high' | 'medium' | 'low')[] = ['high', 'medium', 'low'];

// Failure classes a run can carry. The analogue of the catalog's
// "dimensions" — empty means any.
const FAIL_CLASSES = ['orchestration', 'harness', 'ucs', 'moderation', 'graph_deadlock'];

const WINDOWS: { value: number; label: string }[] = [
  { value: 0, label: 'Default (24 hours)' },
  { value: 300, label: '5 minutes' },
  { value: 900, label: '15 minutes' },
  { value: 3600, label: '1 hour' },
  { value: 21600, label: '6 hours' },
  { value: 86400, label: '24 hours' },
  { value: 604800, label: '7 days' },
];

export default function AlertRuleModal({
  existing, onClose, onSaved,
}: { existing?: AlertRule; onClose: () => void; onSaved: () => void }) {
  const { toast } = useApp();
  const [name, setName] = useState(existing?.name || '');
  const [kind, setKind] = useState(existing?.kind || 'error_rate');
  const [severity, setSeverity] = useState<'high' | 'medium' | 'low'>(existing?.severity || 'medium');
  const [threshold, setThreshold] = useState(String(existing?.threshold ?? 25));
  const [windowSeconds, setWindowSeconds] = useState(existing?.window_seconds ?? 0);
  const [minRuns, setMinRuns] = useState(String(existing?.min_runs ?? 20));
  const [failClasses, setFailClasses] = useState((existing?.fail_classes || []).join(', '));
  const [agentId, setAgentId] = useState(existing?.agent_id || '');
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [kinds, setKinds] = useState<AlertRuleKind[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getAlertRuleKinds().then((r) => setKinds(r.kinds)).catch(() => {});
    // Every agent, not the first page — listAgents caps at 100 and orders
    // by an opaque id.
    api.listAllAgents()
      .then((all) => setAgents(all.filter((a) => a.plane !== 'Snippet')
        .sort((a, b) => a.name.localeCompare(b.name))))
      .catch(() => {});
  }, []);

  const selected = kinds.find((k) => k.value === kind);
  const thresholdNum = Number(threshold);
  const minRunsNum = Number(minRuns);
  const canSave = name.trim().length > 0
    && Number.isFinite(thresholdNum) && thresholdNum >= 0
    && Number.isFinite(minRunsNum) && minRunsNum >= 1;

  const save = () => {
    const body: AlertRuleIn = {
      name: name.trim(), kind, severity,
      threshold: thresholdNum, window_seconds: windowSeconds,
      min_runs: minRunsNum,
      fail_classes: failClasses.split(',').map((f) => f.trim()).filter(Boolean),
      agent_id: agentId || null, enabled,
    };
    setSaving(true);
    (existing ? api.updateAlertRule(existing.id, body) : api.createAlertRule(body))
      .then(() => { toast(existing ? 'Rule updated' : 'Rule created'); onSaved(); onClose(); })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Drawer title={existing ? `Edit ${existing.name}` : 'New alert rule'} onClose={onClose}
      footer={<>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn" onClick={save} disabled={!canSave || saving}>
          {saving ? 'Saving…' : existing ? 'Save changes' : 'Create rule'}
        </button>
      </>}
    >
      <p className="small muted" style={{ margin: '0 0 16px' }}>
        Rules are stored as data and evaluated every sweep. A rule scoped to one agent
        lets you hold that agent to a tighter bar than the rest.
      </p>

      <div className="field">
        <label>Rule name</label>
        <input value={name} onChange={(e) => setName(e.target.value)}
          placeholder="e.g. acme-error-rate-tight" />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Must be unique — a duplicate name returns 409.
        </p>
      </div>

      <div className="row-split" style={{ gap: 12, alignItems: 'flex-start' }}>
        <div className="field" style={{ flex: 1 }}>
          <label id="alert-rule-kind-label">Kind</label>
          <SearchSelect labelledBy="alert-rule-kind-label" noun="kind" value={kind}
            options={kinds.map((k) => ({ value: k.value, label: k.label }))}
            onChange={(v) => setKind(v)} />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label id="alert-rule-sev-label">Severity</label>
          <SearchSelect labelledBy="alert-rule-sev-label" noun="severity" value={severity}
            options={SEVS.map((s) => ({ value: s, label: s }))}
            onChange={(v) => setSeverity(v as 'high' | 'medium' | 'low')} />
        </div>
      </div>
      {selected && (
        <p className="small muted" style={{ margin: '-8px 0 14px' }}>{selected.desc}</p>
      )}

      <div className="row-split" style={{ gap: 12, alignItems: 'flex-start' }}>
        <div className="field" style={{ flex: 1 }}>
          <label>Threshold</label>
          <input value={threshold} onChange={(e) => setThreshold(e.target.value)} />
          <p className="small muted" style={{ margin: '6px 0 0' }}>
            {/* What a threshold MEANS changes with the kind — a percent
                for error rate, a count for denials, a multiple for the
                spike rules. Guessing it wrong is silent. */}
            {selected ? selected.threshold : 'Depends on the kind.'}
          </p>
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label id="alert-rule-window-label">Window</label>
          <SearchSelect labelledBy="alert-rule-window-label" noun="window" value={String(windowSeconds)}
            options={WINDOWS.map((w) => ({ value: String(w.value), label: w.label }))}
            onChange={(v) => setWindowSeconds(Number(v))} />
        </div>
      </div>

      <div className="field">
        <label>Minimum runs</label>
        <input value={minRuns} onChange={(e) => setMinRuns(e.target.value)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          How much data before the rule may fire. 100% of three runs is noise; over eighty
          it is an outage. Must be at least 1.
        </p>
      </div>

      <div className="field">
        <label id="alert-rule-agent-label">For agent</label>
        <SearchSelect labelledBy="alert-rule-agent-label" noun="agent" value={agentId}
          options={[
            { value: '', label: 'All agents' },
            ...agents.map((a) => ({ value: a.id, label: `${a.name} · ${a.id}` })),
          ]}
          onChange={(v) => setAgentId(v)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Scope this rule to one agent, or leave it across all of them.
        </p>
      </div>

      <div className="field">
        <label>Failure classes</label>
        <input value={failClasses} onChange={(e) => setFailClasses(e.target.value)}
          placeholder={FAIL_CLASSES.slice(0, 2).join(', ')} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Comma-separated — {FAIL_CLASSES.join(', ')}. Empty means any class.
        </p>
      </div>

      <div className="row-split">
        <div>
          <b className="small">Enabled</b>
          <p className="small muted" style={{ margin: '2px 0 0' }}>
            Only enabled rules are evaluated.
          </p>
        </div>
        <label className="switch">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span />
        </label>
      </div>
    </Drawer>
  );
}
