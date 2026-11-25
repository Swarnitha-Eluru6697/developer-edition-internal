import { useState } from "react";
import { matchesRecord, usePageSearch, usePageSearchState } from "../context/PageSearchContext";
import { api, ApiRequestError, type DateRange } from "../api/client";
import { useApi } from "../api/useApi";
import { confirmDialog } from "../components/ConfirmDrawer";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import SearchSelect from "../components/SearchSelect";
import type { ActiveAlert, AlertHistoryEntry, AlertPolicy, NotificationChannel } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

// Same tabs, in the same order, as the Agents Alerts page: the three states an
// alert moves through first, then the configuration behind them.
type Tab = "open" | "acknowledged" | "resolved" | "rules" | "channels";
const TABS: [Tab, string][] = [
  ["open", "Open"], ["acknowledged", "Acknowledged"], ["resolved", "Resolved"], ["rules", "Rules"], ["channels", "Channels"],
];

/** Severity reuses the status-pill palette: high reads as failing, medium as
 *  in-progress, low as fine — the same colour language as every other pill. */
function SeverityPill({ severity }: { severity: "low" | "medium" | "high" }) {
  const cls = severity === "high" ? "status-failed" : severity === "medium" ? "status-running" : "status-success";
  return (
    <span className={`status-pill ${cls}`} style={{ fontSize: 10.5, padding: "2px 8px" }}>
      <span className="status-dot" />
      {severity}
    </span>
  );
}

export default function AlertsPage() {
  const { can } = usePermissions();
  const [tab, setTab] = useState<Tab>("open");
  // One date range, shared by every tab that lists something dated (as on the
  // Agents page). Channels are configuration, so they take none.
  const [range, setRange] = useState<DateRange>({ from: "", to: "" });
  const dated = tab !== "channels";
  const ranged = !!(range.from || range.to);
  // Unfiltered: Channels lists every rule routed to a channel, and a new rule
  // picks its signal from the built-ins, whatever the date range says.
  const policies = useApi(api.getAlertPolicies, []);
  const rules = useApi(() => api.getAlertPolicies(range), [range.from, range.to]);
  const channels = useApi(api.getNotificationChannels, []);
  const active = useApi(() => api.getActiveAlerts(range), [range.from, range.to]);
  const history = useApi(() => api.getAlertHistory(range), [range.from, range.to]);
  const [adding, setAdding] = useState(false);
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null);
  const [addingRule, setAddingRule] = useState(false);
  const reloadRules = () => { rules.reload(); policies.reload(); };
  const [evalResult, setEvalResult] = useState<string | null>(null);
  usePageSearch("Search alerts, rules and channels…");

  const openAlerts = (active.data ?? []).filter((a) => a.ackState === "none");
  const ackedAlerts = (active.data ?? []).filter((a) => a.ackState === "acknowledged");
  const reloadAlerts = () => { active.reload(); history.reload(); };

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Alerts</h1>
            <p>
              Rules are data, evaluated live — 3 consecutive windows over threshold before anything
              fires. Every change is audited.
            </p>
          </div>
        </div>

        {/* The tab's own actions sit on the tab row, as on the Agents page. */}
        <div className="tabs tabs-with-tools" role="tablist">
          {TABS.map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id}
              className={"tab" + (tab === id ? " active" : "")} onClick={() => setTab(id)}>
              {label}
              {id === "open" && openAlerts.length > 0 && <span className="tab-count">{openAlerts.length}</span>}
            </button>
          ))}
          <div className="filter-spacer" />
          {tab === "rules" && (
            <span style={{ display: "flex", gap: 8 }}>
              <EvaluateButton onDone={(msg) => { setEvalResult(msg); reloadAlerts(); }} />
              <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary btn-sm" onClick={() => setAddingRule(true)}>
                <Icon name="plus" />
                New alert rule
              </button>
            </span>
          )}
          {tab === "channels" && (
            <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary btn-sm" onClick={() => setAdding(true)}>
              <Icon name="plus" />
              Add channel
            </button>
          )}
        </div>
        {tab === "rules" && evalResult && <div className="hint alert-eval-result">{evalResult}</div>}
        {dated && <DateRangeFilter range={range} onChange={setRange} />}
      </div>

      {tab === "open" && (
        <AlertList alerts={openAlerts} loading={active.loading} onChanged={reloadAlerts}
          intro="Alerts that fired, or are building towards firing, and have not been acknowledged or resolved yet."
          empty={ranged ? "No open alerts were raised in this date range."
            : "Nothing is firing or building right now. Run the rules on the Rules tab to check the current measured signals."} />
      )}
      {tab === "acknowledged" && (
        <AlertList alerts={ackedAlerts} loading={active.loading} onChanged={reloadAlerts}
          intro="Alerts someone has acknowledged but not resolved yet."
          empty={ranged ? "No acknowledged alerts were raised in this date range." : "No acknowledged alerts."} />
      )}
      {tab === "resolved" && <ResolvedList history={history} ranged={ranged} />}
      {tab === "rules" && <RulesTab rules={rules} channels={channels} onChanged={reloadRules} />}
      {tab === "channels" && <ChannelsTab policies={policies} channels={channels} onEdit={setEditingChannel} />}

      {(adding || editingChannel) && (
        <ChannelForm
          existing={editingChannel ?? undefined}
          onClose={() => { setAdding(false); setEditingChannel(null); }}
          onSaved={() => { setAdding(false); setEditingChannel(null); channels.reload(); }}
        />
      )}
      {addingRule && (
        <NewRule
          signals={(policies.data ?? []).filter((p) => p.builtIn && p.evaluable)}
          channels={channels.data ?? []}
          onClose={() => setAddingRule(false)}
          onCreated={() => { setAddingRule(false); reloadRules(); }}
        />
      )}
    </div>
  );
}

function DateRangeFilter({ range, onChange }: { range: DateRange; onChange: (r: DateRange) => void }) {
  const today = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time
  return (
    <div className="alert-dates">
      <div className="field">
        <label htmlFor="alerts-from">From</label>
        <input id="alerts-from" className="input" type="date" value={range.from} max={range.to || today}
          onChange={(e) => onChange({ ...range, from: e.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="alerts-to">To</label>
        <input id="alerts-to" className="input" type="date" value={range.to} min={range.from || undefined} max={today}
          onChange={(e) => onChange({ ...range, to: e.target.value })} />
      </div>
      {(range.from || range.to) && (
        <IconAction icon="x" label="Clear dates" onClick={() => onChange({ from: "", to: "" })} />
      )}
    </div>
  );
}

function EvaluateButton({ onDone }: { onDone: (result: string) => void }) {
  const { can } = usePermissions();
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      const r = await api.evaluateAlertsNow();
      // Reported verbatim. "Skipped for insufficient data" is the honest
      // outcome for a model with too few measured traces to compute a signal
      // from — not a failure, and not something to round up into "evaluated".
      onDone(
        `Evaluated ${r.evaluated} model/policy pair${r.evaluated === 1 ? "" : "s"}` +
        (r.skippedInsufficientData ? `, skipped ${r.skippedInsufficientData} for insufficient measured data` : "") +
        (r.fired.length ? `. Fired: ${r.fired.join(", ")}` : ". Nothing fired.")
      );
    } catch (e) {
      onDone(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <button hidden={!can(PERM.alertsEdit)} className="btn btn-secondary btn-sm" disabled={busy} onClick={run}>
      <Icon name="refresh" />
      {busy ? "Running…" : "Run rules now"}
    </button>
  );
}

function AlertList({
  alerts, loading, intro, empty, onChanged,
}: { alerts: ActiveAlert[]; loading: boolean; intro: string; empty: string; onChanged: () => void }) {
  const { query } = usePageSearchState();
  const [selected, setSelected] = useState<ActiveAlert | null>(null);
  const shown = alerts.filter((x) => matchesRecord(query, x));

  return (
    <div className="alert-list">
      <p className="hint" style={{ margin: 0 }}>{intro}</p>
      {loading && !alerts.length && <div className="empty-hint">Loading…</div>}
      {shown.map((a) => (
        <button type="button" className="card alert-card" key={`${a.modelId}-${a.policyId}`} onClick={() => setSelected(a)}>
          <span className="alert-card-head">
            <span className="alert-card-tags">
              <SeverityPill severity={a.severity} />
              <span className="alert-card-name">{a.policyName}</span>
              <span className="badge badge-outline">{a.modelId}</span>
              {a.status === "BUILDING" && <span className="badge badge-amber">building</span>}
            </span>
            {/* A fired alert keeps counting past windowsToFire, so "2325/3" would read as nonsense. */}
            <span className="alert-card-when">
              {a.status === "FIRED"
                ? `${a.windowsExceeded} window${a.windowsExceeded === 1 ? "" : "s"} over threshold`
                : `${a.windowsExceeded}/${a.windowsToFire} windows`}
            </span>
          </span>
          <span className="alert-card-summary">{a.label} — {a.condition} · current {a.currentValue}</span>
          <span className="alert-card-meta">Raised {new Date(a.raisedAt).toLocaleString()}</span>
        </button>
      ))}
      {!loading && shown.length === 0 && <div className="empty-hint">{empty}</div>}
      {selected && (
        <AlertDetail alert={selected} onClose={() => setSelected(null)}
          onChanged={() => { setSelected(null); onChanged(); }} />
      )}
    </div>
  );
}

function AlertDetail({ alert, onClose, onChanged }: { alert: ActiveAlert; onClose: () => void; onChanged: () => void }) {
  const { can } = usePermissions();
  const [busy, setBusy] = useState<"ack" | "resolve" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(what: "ack" | "resolve") {
    setBusy(what);
    setError(null);
    try {
      if (what === "ack") await api.acknowledgeAlert(alert.modelId, alert.policyId);
      else await api.resolveAlert(alert.modelId, alert.policyId);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer" role="dialog" aria-modal="true" aria-label={`Alert ${alert.policyName}`}>
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)" }}>{alert.policyName}</div>
            <div className="alert-card-tags" style={{ marginTop: 8 }}>
              <SeverityPill severity={alert.severity} />
              <span className="badge badge-outline">{alert.modelId}</span>
              <span className="badge badge-outline">{alert.status === "FIRED" ? "fired" : "building"}</span>
            </div>
          </div>
          <button className="drawer-close" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
          <div className="drawer-section">
            <div className="drawer-section-title">What happened</div>
            <div className="kv-row"><span className="k">Rule</span><span className="v">{alert.label}</span></div>
            <div className="kv-row"><span className="k">Condition</span><span className="v">{alert.condition}</span></div>
            <div className="kv-row"><span className="k">Current value</span><span className="v">{alert.currentValue}</span></div>
            <div className="kv-row"><span className="k">Windows over threshold</span><span className="v">{alert.status === "FIRED" ? alert.windowsExceeded : `${alert.windowsExceeded} of ${alert.windowsToFire}`}</span></div>
            <div className="kv-row"><span className="k">State</span><span className="v">{alert.ackState === "none" ? "open" : alert.ackState}</span></div>
            <div className="kv-row"><span className="k">Raised</span><span className="v">{new Date(alert.raisedAt).toLocaleString()}</span></div>
          </div>
          <div className="drawer-section" style={{ display: "flex", gap: 8 }}>
            {/* Once Resolve is clicked, Acknowledge stays disabled — a resolved alert has nothing to acknowledge. */}
            <button hidden={!can(PERM.alertsEdit)} className="btn btn-secondary" onClick={() => act("ack")}
              disabled={busy !== null || alert.ackState !== "none"}
              title={busy === "resolve" ? "Resolved alerts cannot be acknowledged" : alert.ackState === "acknowledged" ? "Already acknowledged" : undefined}>
              Acknowledge
            </button>
            <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary" disabled={busy !== null} onClick={() => act("resolve")}>
              <Icon name="check" />
              {busy === "resolve" ? "Resolving…" : "Resolve"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ResolvedList({ history, ranged }: { history: ReturnType<typeof useApi<AlertHistoryEntry[]>>; ranged: boolean }) {
  const { query } = usePageSearchState();
  const shown = (history.data ?? []).filter((x) => matchesRecord(query, x));

  return (
    <div className="alert-list">
      <p className="hint" style={{ margin: 0 }}>Alerts that were resolved, with the value they had when they cleared.</p>
      {history.loading && !history.data?.length && <div className="empty-hint">Loading…</div>}
      {shown.map((h) => (
        <div className="card alert-card" key={h.id}>
          <span className="alert-card-head">
            <span className="alert-card-tags">
              <span className="alert-card-name">{h.policyName}</span>
              <span className="badge badge-outline">{h.modelId}</span>
            </span>
            <span className="alert-card-when">{new Date(h.resolvedAt).toLocaleString()}</span>
          </span>
          <span className="alert-card-summary">{h.label} · final value {h.finalValue} · {h.windowsAtResolution} window{h.windowsAtResolution === 1 ? "" : "s"}</span>
          <span className="alert-card-meta">Resolved by <b>{h.resolvedBy}</b></span>
        </div>
      ))}
      {!history.loading && shown.length === 0 && <div className="empty-hint">{ranged ? "No alerts were resolved in this date range." : "No alerts have been resolved yet."}</div>}
    </div>
  );
}

function RulesTab({
  rules: policies, channels, onChanged,
}: {
  rules: ReturnType<typeof useApi<AlertPolicy[]>>;
  channels: ReturnType<typeof useApi<NotificationChannel[]>>;
  onChanged: () => void;
}) {
  const { query } = usePageSearchState();
  const [editing, setEditing] = useState<AlertPolicy | null>(null);

  async function remove(p: AlertPolicy) {
    const ok = await confirmDialog({
      title: `Delete ${p.name}?`,
      message: `This rule will stop being evaluated${p.modelId ? ` for ${p.modelId}` : ""}. Its open alerts and their history are removed with it.`,
      confirmLabel: "Delete rule",
      danger: true,
    });
    if (!ok) return;
    await api.deleteAlertPolicy(p.id);
    onChanged();
  }

  return (
    <div className="alert-list">
      <p className="hint" style={{ margin: 0 }}>
        Rules are stored as data and read by the engine on every evaluation window. Toggle a rule,
        retune its threshold, or override it per model — changes take effect immediately and are
        written to the audit log.
      </p>

      {/* The engine can only measure its eight built-in signals, so a new rule
          picks one of them (see NewRule) rather than naming a metric of its own
          that would never be evaluated. Each row says whether it can be. */}
      {policies.loading && <div className="empty-hint">Loading…</div>}
      <div className="table-wrap" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr><th>Rule</th><th>Fires when</th><th>Scope</th><th>Channel</th><th style={{ textAlign: "right" }}>Actions</th></tr>
          </thead>
          <tbody>
            {policies.data?.filter((x) => matchesRecord(query, x)).map((p) => (
              <tr key={p.id} style={{ opacity: p.enabled ? 1 : 0.5 }}>
                <td>
                  <div className="cell-name" style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}>{p.name}</div>
                  <div className="cell-sub">
                    {p.label}
                    {!p.builtIn && <span className="badge badge-outline" style={{ marginLeft: 6 }}>custom</span>}
                    {!p.evaluable && <span className="badge badge-amber" style={{ marginLeft: 6 }}>not evaluable</span>}
                  </div>
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <SeverityPill severity={p.severity} />
                  <span className="cell-sub" style={{ marginLeft: 8 }}>{p.operator} {p.defaultThreshold}{p.unit} for 3 windows</span>
                </td>
                <td className="cell-sub">
                  {p.modelId
                    ? <span style={{ fontFamily: "var(--font-mono)" }}>{p.modelId}</span>
                    : p.overrides.length ? `All models · ${p.overrides.length} with custom threshold` : "All models"}
                </td>
                <td>
                  {channels.data?.find((c) => c.id === p.channelId)?.name ?? <span className="cell-sub">not routed</span>}
                </td>
                <td>
                  <span style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
                  <label className="switch" title={p.enabled ? "Enabled" : "Disabled"}>
                    <input
                      type="checkbox"
                      aria-label={`Enable ${p.name}`}
                      checked={p.enabled}
                      onChange={async (e) => { await api.updateAlertPolicy(p.id, { enabled: e.target.checked }); policies.reload(); }}
                    />
                    <span className="slider" />
                  </label>
                  <IconAction icon="pencil" label={`Edit rule ${p.name}`} gated={PERM.alertsEdit} tip="up" align="end" onClick={() => setEditing(p)} />
                  {/* Built-in rules are the only definition of their signal: turned off, never deleted. */}
                  {!p.builtIn && (
                    <IconAction icon="trash" label={`Delete rule ${p.name}`} danger gated={PERM.alertsEdit} tip="up" align="end" onClick={() => remove(p)} />
                  )}
                  </span>
                </td>
              </tr>
            ))}
            {!policies.loading && !policies.data?.length && (
              <tr><td colSpan={5}><div className="empty-hint">No rules were created in this date range.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      {editing && (
        <RuleEditor
          policy={editing}
          channels={channels.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); policies.reload(); }}
        />
      )}
    </div>
  );
}

function ChannelsTab({
  policies, channels, onEdit,
}: {
  policies: ReturnType<typeof useApi<AlertPolicy[]>>;
  channels: ReturnType<typeof useApi<NotificationChannel[]>>;
  onEdit: (c: NotificationChannel) => void;
}) {
  const { query } = usePageSearchState();

  async function remove(c: NotificationChannel) {
    const ok = await confirmDialog({
      title: `Remove ${c.name}?`,
      message: `Alerts will stop being sent to this ${c.type === "teams" ? "Teams" : "Slack"} channel. The webhook URL is deleted with it — you would need to paste it again to re-add the channel. Rules routed here move to another channel.`,
      confirmLabel: "Remove channel",
      danger: true,
    });
    if (!ok) return;
    await api.deleteNotificationChannel(c.id);
    channels.reload();
  }

  return (
    <div className="alert-list">
      <p className="hint" style={{ margin: 0 }}>
        Alerts post to Slack or Teams as a real webhook call when a rule fires. Each rule picks its
        channel when you edit it on the Rules tab; a rule with no channel is still recorded here.
      </p>
      {channels.loading && <div className="empty-hint">Loading…</div>}
      {channels.data?.filter((x) => matchesRecord(query, x)).map((c) => {
        // Routing lives on the rule; listing it here answers "what lands in this channel?".
        const routed = (policies.data ?? []).filter((p) => p.channelId === c.id);
        return (
          <div className="card alert-card" key={c.id}>
            <span className="alert-card-head">
              <span className="alert-card-tags">
                <b>{c.name}</b>
                <span className="badge badge-outline">{c.type === "teams" ? "Teams" : "Slack"}</span>
                {!c.enabled && <span className="badge badge-outline">disabled</span>}
              </span>
              <span className="alert-card-tags">
                <IconAction icon="pencil" label={`Edit channel ${c.name}`} gated={PERM.alertsEdit} align="end" onClick={() => onEdit(c)} />
                <IconAction icon="trash" label={`Remove channel ${c.name}`} danger gated={PERM.alertsEdit} align="end" onClick={() => remove(c)} />
              </span>
            </span>
            <span className="alert-card-meta">
              Receives <b>{SEVERITY_FLOORS.find((s) => s.value === c.minSeverity)?.label.toLowerCase() ?? c.minSeverity}</b>
              {" from "}<b>{c.modelId ?? "all models"}</b>
              {c.channelDest && <> · {c.channelDest}</>}
              {" · "}<span style={{ fontFamily: "var(--font-mono)" }}>{c.webhookMasked || "no webhook — recorded only"}</span>
            </span>
            <span className="alert-card-meta">
              Rules routed here: {routed.length ? routed.map((p) => p.label).join(", ") : "none yet — pick this channel when editing a rule"}
            </span>
          </div>
        );
      })}
      {!channels.loading && !channels.data?.length && (
        <div className="empty-hint">No channels yet — alerts stay inside ML Studio until you add one.</div>
      )}
    </div>
  );
}

function NewRule({
  signals, channels, onClose, onCreated,
}: { signals: AlertPolicy[]; channels: NotificationChannel[]; onClose: () => void; onCreated: () => void }) {
  const { can } = usePermissions();
  const deployments = useApi(api.getDeployments, []);
  const [name, setName] = useState("");
  const [signal, setSignal] = useState(signals[0]?.id ?? "");
  const [severity, setSeverity] = useState<"low" | "medium" | "high">("medium");
  const [threshold, setThreshold] = useState("");
  const [modelId, setModelId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = signals.find((s) => s.id === signal);
  const value = Number(threshold);
  const canSave = name.trim().length > 0 && !!base && threshold.trim() !== "" && Number.isFinite(value);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.createAlertPolicy({
        name: name.trim(), signal, severity, threshold: value,
        modelId: modelId || null, channelId: channelId || null,
      });
      onCreated();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer" role="dialog" aria-modal="true" aria-label="New alert rule">
        <div className="drawer-head">
          <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>New alert rule</div>
          <button className="drawer-close" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
          <p className="hint" style={{ marginBottom: 16 }}>
            A rule watches one of the signals the engine measures, with its own threshold, severity and
            scope — for example to hold one model to a tighter bar than the rest.
          </p>
          <div className="drawer-section">
            <div className="field">
              <label htmlFor="new-rule-name">Rule name</label>
              <input id="new-rule-name" className="input" value={name} onChange={(e) => setName(e.target.value)}
                placeholder="e.g. fraud-latency-tight" />
              <div className="hint">Must be unique.</div>
            </div>
            <div className="field">
              <label id="new-rule-signal-label">Signal</label>
              <SearchSelect labelledBy="new-rule-signal-label" noun="signal" value={signal}
                options={signals.map((s) => ({ value: s.id, label: s.label, hint: `${s.operator} ${s.defaultThreshold}${s.unit}` }))}
                onChange={(v) => setSignal(v)} />
              {base && (
                <div className="hint">
                  Fires when the value is {base.operator === ">" ? "above" : "below"} the threshold for 3 windows in a row.
                  The built-in rule uses {base.operator} {base.defaultThreshold}{base.unit}.
                </div>
              )}
            </div>
            <div className="field">
              <label id="new-rule-sev-label">Severity</label>
              <SearchSelect labelledBy="new-rule-sev-label" noun="severity" value={severity}
                options={[{ value: "low", label: "low" }, { value: "medium", label: "medium" }, { value: "high", label: "high" }]}
                onChange={(v) => setSeverity(v as "low" | "medium" | "high")} />
            </div>
            <div className="field">
              <label htmlFor="new-rule-threshold">Threshold</label>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ color: "var(--text-faint)" }}>{base?.operator}</span>
                <input id="new-rule-threshold" className="input" style={{ maxWidth: 140 }} inputMode="decimal"
                  value={threshold} onChange={(e) => setThreshold(e.target.value)} />
                <span style={{ color: "var(--text-faint)", fontSize: 12 }}>{base?.unit}</span>
              </div>
            </div>
            <div className="field">
              <label id="new-rule-model-label">For model</label>
              <SearchSelect labelledBy="new-rule-model-label" noun="model" value={modelId}
                options={[
                  { value: "", label: "All models" },
                  ...(deployments.data ?? []).map((d) => ({ value: d.modelId, label: d.modelId, hint: d.task })),
                ]}
                onChange={(v) => setModelId(v)} />
              <div className="hint">Only deployed models are listed — a rule can only fire against something serving.</div>
            </div>
            <div className="field">
              <label id="new-rule-channel-label">Channel</label>
              <SearchSelect labelledBy="new-rule-channel-label" noun="channel" value={channelId}
                options={[
                  { value: "", label: "No channel — recorded here only" },
                  ...channels.map((c) => ({ value: c.id, label: `${c.name} (${c.type})` })),
                ]}
                onChange={(v) => setChannelId(v)} />
            </div>
            <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary btn-block" disabled={busy || !canSave} onClick={save}>
              <Icon name="check" />
              {busy ? "Creating…" : "Create rule"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function RuleEditor({
  policy, channels, onClose, onSaved,
}: { policy: AlertPolicy; channels: NotificationChannel[]; onClose: () => void; onSaved: () => void }) {
  const { can } = usePermissions();
  const deployments = useApi(api.getDeployments, []);
  const [threshold, setThreshold] = useState(String(policy.defaultThreshold));
  const [enabled, setEnabled] = useState(policy.enabled);
  const [channelId, setChannelId] = useState(policy.channelId ?? "");
  const [overrides, setOverrides] = useState<Record<string, string>>(
    Object.fromEntries(policy.overrides.map((o) => [o.modelId, String(o.threshold)]))
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateAlertPolicy(policy.id, {
        enabled,
        defaultThreshold: Number(threshold),
        channelId: channelId || null,
      });
      // Each override is its own real write; an empty field clears it.
      for (const d of deployments.data ?? []) {
        const raw = overrides[d.modelId];
        const wasSet = policy.overrides.some((o) => o.modelId === d.modelId);
        if (raw === undefined || raw === "") {
          if (wasSet) await api.setAlertOverride(policy.id, d.modelId, null);
        } else if (Number(raw) !== policy.overrides.find((o) => o.modelId === d.modelId)?.threshold) {
          await api.setAlertOverride(policy.id, d.modelId, Number(raw));
        }
      }
      onSaved();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)" }}>{policy.name}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>{policy.label} · {policy.window} windows</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          {!policy.evaluable && (
            <div className="callout callout-amber" style={{ marginBottom: 14 }}>
              The evaluation engine has no measurement for this rule, so it will never fire no matter
              how it's configured here. Its threshold and routing are still stored.
            </div>
          )}

          <div className="drawer-section">
            <div className="drawer-section-title">Rule</div>
            <div className="kv-row"><span className="k">Severity</span><span className="v">{policy.severity}</span></div>
            <div className="kv-row"><span className="k">Applies to</span><span className="v">{policy.appliesTo.join(", ")}</span></div>
            <div className="kv-row"><span className="k">On fire</span><span className="v">{policy.action}</span></div>
            <div className="util-row" style={{ gridTemplateColumns: "1fr auto", marginTop: 8 }}>
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>Enabled</span>
              <label className="switch">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                <span className="slider" />
              </label>
            </div>
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">Default threshold</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              Applies to every model that hasn't set its own override below.
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: "var(--text-faint)" }}>{policy.operator}</span>
              <input className="input" style={{ maxWidth: 140 }} value={threshold} onChange={(e) => setThreshold(e.target.value)} />
              <span style={{ color: "var(--text-faint)", fontSize: 12 }}>{policy.unit}</span>
            </div>
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">Per-model overrides</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              Leave blank to use the default. Only deployed models are listed — a rule can only fire
              against something actually serving.
            </div>
            {(deployments.data ?? []).map((d) => (
              <div className="field" key={d.modelId} style={{ marginBottom: 10 }}>
                <label style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{d.modelId}</label>
                <input
                  className="input"
                  placeholder={`default ${policy.defaultThreshold}${policy.unit}`}
                  value={overrides[d.modelId] ?? ""}
                  onChange={(e) => setOverrides({ ...overrides, [d.modelId]: e.target.value })}
                />
              </div>
            ))}
            {!deployments.data?.length && <div className="empty-hint">No models are deployed.</div>}
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title" id="alert-routing-label">Routing</div>
            <SearchSelect labelledBy="alert-routing-label" noun="channel" value={channelId}
              options={[
                { value: "", label: "No channel — recorded here only" },
                ...channels.map((c) => ({ value: c.id, label: `${c.name} (${c.type})` })),
              ]}
              onChange={(v) => setChannelId(v)} />
          </div>

          <div className="drawer-section">
            <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary btn-block" disabled={busy} onClick={save}>
              <Icon name="check" />
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const CHANNEL_KINDS: { value: "slack" | "teams"; label: string; icon: "message" | "share"; desc: string; placeholder: string }[] = [
  { value: "slack", label: "Slack", icon: "message", desc: "An Incoming Webhook URL from hooks.slack.com.", placeholder: "https://hooks.slack.com/services/T000/B000/XXXX" },
  { value: "teams", label: "Microsoft Teams", icon: "share", desc: "An Incoming Webhook connector URL from the channel.", placeholder: "https://outlook.office.com/webhook/…" },
];

const SEVERITY_FLOORS: { value: "low" | "medium" | "high"; label: string; hint: string }[] = [
  { value: "low", label: "Low and above", hint: "Everything" },
  { value: "medium", label: "Medium and above", hint: "Skips low-severity notices" },
  { value: "high", label: "High only", hint: "Just the ones worth waking someone for" },
];

/** Add or edit a channel — the same form, fields and wording as the Agents
 *  "Add alert channel" dialog, with models in place of agents. Opens as a
 *  right-hand drawer like the rule editor and every other panel here. */
function ChannelForm({
  existing, onClose, onSaved,
}: { existing?: NotificationChannel; onClose: () => void; onSaved: () => void }) {
  const { can } = usePermissions();
  const deployments = useApi(api.getDeployments, []);
  const [type, setType] = useState<"slack" | "teams">(existing?.type ?? "slack");
  const [name, setName] = useState(existing?.name ?? "");
  const [webhook, setWebhook] = useState("");
  const [minSeverity, setMinSeverity] = useState<"low" | "medium" | "high">(existing?.minSeverity ?? "low");
  const [modelId, setModelId] = useState(existing?.modelId ?? "");
  const [modelFilter, setModelFilter] = useState("");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kind = CHANNEL_KINDS.find((k) => k.value === type)!;
  const models = deployments.data ?? [];
  // The filter narrows the list but never hides the model already chosen.
  const shown = models.filter((d) => d.modelId === modelId || d.modelId.toLowerCase().includes(modelFilter.trim().toLowerCase()));
  // A new channel needs its URL; an edit may leave it blank to keep the stored one.
  const urlRequired = !existing;
  const canSave = name.trim().length > 0 && (!urlRequired || webhook.trim().length > 0);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const fields = { name: name.trim(), type, enabled, minSeverity, modelId: modelId || null };
      if (existing) await api.updateNotificationChannel(existing.id, { ...fields, webhookUrl: webhook.trim() });
      else await api.createNotificationChannel({ ...fields, webhookUrl: webhook.trim() });
      onSaved();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="channel-form-title">
        <div className="drawer-head">
          <div>
            <div id="channel-form-title" style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>
              {existing ? `Edit ${existing.name}` : "Add alert channel"}
            </div>
            <div className="hint" style={{ marginTop: 4 }}>
              Alerts matching the filters below are posted to this channel. Everything else is left alone.
            </div>
          </div>
          <button className="drawer-close" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          <div className="field">
            <label id="channel-type-label">Channel type</label>
            <div className="channel-type-grid" role="group" aria-labelledby="channel-type-label">
              {CHANNEL_KINDS.map((k) => (
                <button key={k.value} type="button" aria-pressed={type === k.value}
                  className={"channel-type" + (type === k.value ? " active" : "")} onClick={() => setType(k.value)}>
                  <Icon name={k.icon} />
                  {k.label}
                </button>
              ))}
            </div>
            <div className="hint">{kind.desc}</div>
          </div>

          <div className="field">
            <label htmlFor="channel-name">Name</label>
            <input id="channel-name" className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. ml-oncall" />
            <div className="hint">How this channel is identified on this page. Must be unique.</div>
          </div>

          <div className="field">
            <label htmlFor="channel-url">
              Webhook URL {!urlRequired && <span className="hint" style={{ fontWeight: 400 }}>(leave blank to keep the current one)</span>}
            </label>
            <input id="channel-url" className="input" style={{ fontFamily: "var(--font-mono)" }} value={webhook}
              onChange={(e) => setWebhook(e.target.value)} placeholder={kind.placeholder} />
            <div className="hint">
              {existing
                ? <>Currently <span style={{ fontFamily: "var(--font-mono)" }}>{existing.webhookMasked || "—"}</span>. The full URL is never shown again after it is saved — anyone holding it can post into your channel.</>
                : <>Must be <span style={{ fontFamily: "var(--font-mono)" }}>https://</span>. Never returned by the API once saved.</>}
            </div>
          </div>

          <div className="field">
            <label id="channel-sev-label">Send alerts of severity</label>
            <SearchSelect labelledBy="channel-sev-label" noun="severity" value={minSeverity}
              options={SEVERITY_FLOORS.map((s) => ({ value: s.value, label: s.label, hint: s.hint }))}
              onChange={(v) => setMinSeverity(v as "low" | "medium" | "high")} />
          </div>

          <div className="field">
            <label id="channel-model-label">For model</label>
            <input className="input" aria-label="Filter models" value={modelFilter} onChange={(e) => setModelFilter(e.target.value)}
              placeholder="Filter models by name…" style={{ marginBottom: 6 }} />
            <SearchSelect labelledBy="channel-model-label" noun="model" value={modelId}
              options={[
                { value: "", label: "All models" },
                ...shown.map((d) => ({ value: d.modelId, label: d.modelId, hint: d.task })),
              ]}
              onChange={(v) => setModelId(v)} />
            <div className="hint">
              {models.length > 0 && <>Showing {shown.length} of {models.length} models. </>}
              Pick one to send only its alerts here. Other models&rsquo; alerts will not reach this channel.
            </div>
          </div>

          <div className="channel-enabled">
            <div>
              <b style={{ fontSize: 13 }}>Enabled</b>
              <div className="hint" style={{ marginTop: 2 }}>Turn off to stop delivery without losing the configuration.</div>
            </div>
            <label className="switch">
              <input type="checkbox" aria-label="Enabled" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <span className="slider" />
            </label>
          </div>
        </div>
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button hidden={!can(PERM.alertsEdit)} className="btn btn-primary" onClick={save} disabled={!canSave || busy}>
            {busy ? "Saving…" : existing ? "Save changes" : "Add channel"}
          </button>
        </div>
      </div>
    </div>
  );
}

