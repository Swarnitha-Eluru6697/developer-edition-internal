import { useCallback, useEffect, useMemo, useState } from 'react';
import DateRangeFilter from '../components/DateRangeFilter';
import Pager from '../components/Pager';
import { useKeysetPage } from '../hooks/useKeysetPage';
import { api } from '../api';
import Icon from '../components/Icon';
import AlertDestinationModal from '../components/AlertDestinationModal';
import AlertRuleModal from '../components/AlertRuleModal';
import ConfirmPanel from '../components/ConfirmPanel';
import { PageHead, SevChip, Empty } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Agent, Alert, AlertCondition, AlertDestination, AlertRule } from '../types';

const SEV_LABEL: Record<string, string> = {
  low: 'low and above', medium: 'medium and above', high: 'high only',
};

export default function Alerts() {
  const { openDrawer, toast, dataVersion } = useApp();
  const [tab, setTab] = useState<'open' | 'acknowledged' | 'resolved' | 'rules' | 'channels'>('open');
  // One date range, shared by every tab that lists events. Channels is
  // configuration rather than events, so it does not take one.
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const dated = tab !== 'channels';

  const alertState = (tab === 'channels' || tab === 'rules' ? 'open' : tab) as
    'open' | 'acknowledged' | 'resolved';

  const alertPage = useKeysetPage<Alert>(
    useCallback((cursor, limit) => api.listAlertsPage({
      state: alertState, cursor, limit,
      fromDate: fromDate || undefined, toDate: toDate || undefined,
    }), [alertState, fromDate, toDate]),
    [alertState, fromDate, toDate, dataVersion], tab !== 'rules' && tab !== 'channels');

  const rulePage = useKeysetPage<AlertRule>(
    useCallback((cursor, limit) => api.listAlertRulesPage({
      cursor, limit, fromDate: fromDate || undefined, toDate: toDate || undefined,
    }), [fromDate, toDate]),
    [fromDate, toDate], tab === 'rules');

  const channelPage = useKeysetPage<AlertDestination>(
    useCallback((cursor, limit) => api.listAlertDestinationsPage({ cursor, limit }), []),
    [], tab === 'channels');

  const alerts = alertPage.items;
  const rules = rulePage.items;
  const destinations = channelPage.items;

  const [editing, setEditing] = useState<AlertDestination | null>(null);
  const [adding, setAdding] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  // Destinations carry an agent_id, not a name, and the alerts list is
  // empty on this tab — so the names have to come from the agents.
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const [conditions, setConditions] = useState<AlertCondition[] | null>(null);
  const [sweeping, setSweeping] = useState(false);
  const [undelivered, setUndelivered] = useState(0);
  const [editingRule, setEditingRule] = useState<AlertRule | null>(null);
  const [addingRule, setAddingRule] = useState(false);
  // What is pending confirmation, rather than a browser dialog blocking
  // the thread.
  const [confirming, setConfirming] = useState<
    { title: string; body: React.ReactNode; label: string; run: () => void } | null>(null);

  // Alerts that reached no channel are excluded from every tab, so
  // without this count they would simply vanish — the most dangerous way
  // to lose an alert. The sweep retries them; this says how many wait.
  useEffect(() => {
    if (tab === 'channels' || tab === 'rules') return;
    api.listAlerts('undelivered').then((u) => setUndelivered(u.length)).catch(() => {});
  }, [tab, dataVersion]);

  const loadConditions = useCallback(() => {
    api.previewAlertRules().then((r) => setConditions(r.conditions)).catch(() => {});
    rulePage.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const removeRule = (r: AlertRule) => setConfirming({
    title: `Delete ${r.name}?`, label: 'Delete rule',
    body: <>
      This rule will stop being evaluated. Alerts it already raised are kept.
      {r.agent_name
        ? <> It currently applies to <b>{r.agent_name}</b> only.</>
        : <> It currently applies to <b>all agents</b>.</>}
    </>,
    run: () => api.deleteAlertRule(r.id)
      .then(() => { toast(`${r.name} deleted`); loadConditions(); })
      .catch((e) => toast(e.message))
      .finally(() => setConfirming(null)),
  });

  const toggleRule = (r: AlertRule) => {
    api.updateAlertRule(r.id, {
      name: r.name, kind: r.kind, severity: r.severity, threshold: r.threshold,
      window_seconds: r.window_seconds, min_runs: r.min_runs,
      fail_classes: r.fail_classes, agent_id: r.agent_id, enabled: !r.enabled,
    }).then(() => loadConditions()).catch((e) => toast(e.message));
  };
  useEffect(() => { if (tab === 'rules') loadConditions(); }, [tab, loadConditions]);

  const sweep = () => {
    setSweeping(true);
    api.sweepAlertRules()
      .then((r) => toast(
        `${r.raised} raised, ${r.suppressed_as_duplicate} already open, `
        + `${r.resolved} resolved, ${r.deliveries} delivered to `
        + `${r.destinations} channel(s)`))
      .catch((e) => toast(e.message))
      .finally(() => { setSweeping(false); loadConditions(); });
  };

  useEffect(() => {
    if (tab !== 'channels') return;
    api.listAgents()
      .then((as: Agent[]) => setAgentNames(Object.fromEntries(as.map((a) => [a.id, a.name]))))
      .catch(() => {});
  }, [tab]);

  const sendTest = (d: AlertDestination) => {
    setTesting(d.id);
    api.testAlertDestination(d.id)
      // A failed test is a 200 carrying the reason, so the outcome is in
      // the body rather than in a rejected promise.
      .then((r) => toast(r.delivered
        ? `Test message posted to ${d.name}`
        : `${d.name} did not accept the message — ${r.detail}`))
      .catch((e) => toast(e.message))
      .finally(() => { setTesting(null); channelPage.reload(); });
  };

  const remove = (d: AlertDestination) => {
    setConfirming({
      title: `Remove ${d.name}?`, label: 'Remove channel',
      body: <>
        Alerts will stop being sent to this {d.kind === 'teams' ? 'Teams' : 'Slack'} channel.
        The webhook URL is deleted with it and cannot be recovered — you would need to paste
        it again to re-add the channel.
      </>,
      run: () => api.deleteAlertDestination(d.id)
        .then(() => { toast(`${d.name} removed`); channelPage.reload(); })
        .catch((e) => toast(e.message))
        .finally(() => setConfirming(null)),
    });
  };

  return (
    <div className="wrap">
      <PageHead title="Alerts" eyebrow="Operate"
        subtitle="denied-burst · priv-escalation · quality-breach — plus harness-level alerts (step-limit, guardrail block)." />
      {/* The tabs, their actions and the date filter pin as one block under
          the page header, so a long list scrolls beneath them on every tab. */}
      <div className="sticky-head" style={{ marginBottom: 12 }}>
        <div className="row-split alerts-tabbar" style={{ alignItems: 'flex-end' }}>
          <div className="tabs">
            {(['open', 'acknowledged', 'resolved', 'rules', 'channels'] as const).map((t) => (
              <div key={t} className={`tab${tab === t ? ' on' : ''}`} onClick={() => setTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </div>
            ))}
          </div>
          {tab === 'channels' && (
            <button className="btn" onClick={() => setAdding(true)}>
              <Icon name="i-plus" /> Add channel
            </button>
          )}
          {tab === 'rules' && (
            <div className="row-split" style={{ gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn ghost" onClick={sweep} disabled={sweeping}>
                <Icon name="i-bolt" /> {sweeping ? 'Running…' : 'Run rules now'}
              </button>
              <button className="btn" onClick={() => setAddingRule(true)}>
                <Icon name="i-plus" /> New alert rule
              </button>
            </div>
          )}
        </div>

        {dated && (
          <div style={{ padding: '12px 0 0' }}>
            <DateRangeFilter from={fromDate} to={toDate}
              onFrom={setFromDate} onTo={setToDate} />
          </div>
        )}
      </div>

      {tab === 'rules' ? (
        <div className="grid" style={{ gap: 12 }}>
          <p className="small muted" style={{ margin: 0 }}>
            Configure what raises an alert — stored as data, evaluated every sweep against
            real runs, traces and eval results. A rule scoped to one agent holds that agent
            to a tighter bar than the rest.
          </p>

          <div className="card" style={{ overflow: 'hidden' }}>
            <table>
              <thead><tr>
                <th>Rule</th><th>Kind</th><th>Fires when</th><th>Agent</th><th></th>
              </tr></thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id} style={{ opacity: r.enabled ? 1 : 0.5 }}>
                    <td><b className="mono small">{r.name}</b></td>
                    <td><span className="chip line">{r.kind.replace(/_/g, ' ')}</span></td>
                    <td className="small muted">
                      <SevChip sev={r.severity} /> ≥ {r.threshold}
                      {' · '}min {r.min_runs} run{r.min_runs === 1 ? '' : 's'}
                      {r.fail_classes.length > 0 && ` · ${r.fail_classes.join(', ')}`}
                    </td>
                    <td className="small">{r.agent_name || <span className="muted">all agents</span>}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <label className="switch" title={r.enabled ? 'Enabled' : 'Disabled'}>
                        <input type="checkbox" checked={r.enabled} onChange={() => toggleRule(r)} />
                        <span />
                      </label>
                      {/* Icon-only buttons need an accessible name —
                          otherwise nothing but position identifies them. */}
                      <button className="icon-action" style={{ marginLeft: 8, verticalAlign: 'middle' }}
                        title={`Edit ${r.name}`} aria-label={`Edit ${r.name}`}
                        onClick={() => setEditingRule(r)}><Icon name="i-pencil" /></button>
                      <button className="icon-action danger" style={{ marginLeft: 6, verticalAlign: 'middle' }}
                        title={`Delete ${r.name}`} aria-label={`Delete ${r.name}`}
                        onClick={() => removeRule(r)}><Icon name="i-trash" /></button>
                    </td>
                  </tr>
                ))}
                {rules.length === 0 && (
                  <tr><td colSpan={5} className="small muted" style={{ padding: 16 }}>
                    No rules configured — nothing will ever alert.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>

          <Pager page={rulePage} noun="rules" />

          <p className="small muted" style={{ margin: '4px 0 0' }}>
            <b>Currently breaching.</b> A condition already reported stays suppressed until
            it clears, so a channel is not flooded with the same alert.
          </p>
          {conditions === null && <p className="small muted">Checking…</p>}
          {conditions?.length === 0 && (
            <Empty icon="i-check">Nothing is currently breaching a rule</Empty>
          )}
          {conditions?.map((c, n) => (
            <div className="card pad" key={`${c.rule}-${c.agent_id}-${n}`}>
              <div className="row-split">
                <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                  <SevChip sev={c.sev} />
                  <b className="mono small">{c.rule}</b>
                  <span className="chip line">{c.agent_name}</span>
                  {c.already_open
                    ? <span className="chip">already open — suppressed</span>
                    : <span className="chip blue">would raise</span>}
                </div>
              </div>
              <p style={{ margin: '10px 0 0', fontSize: 13 }}>{c.summary}</p>
            </div>
          ))}
        </div>
      ) : tab === 'channels' ? (
        <div className="grid" style={{ gap: 12 }}>
          <p className="small muted" style={{ margin: 0 }}>
            Alerts are posted to the channels below. A channel scoped to one agent receives only that
            agent&rsquo;s alerts; a severity floor keeps quieter notices out of an on-call channel.
          </p>
          {destinations.length === 0 && (
            <Empty icon="i-bell">
              No channels yet — alerts stay inside the platform until you add one
            </Empty>
          )}
          {destinations.map((d) => (
            <div className="card pad" key={d.id}>
              <div className="row-split">
                <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                  <b>{d.name}</b>
                  <span className="chip line">{d.kind === 'teams' ? 'Teams' : 'Slack'}</span>
                  {!d.enabled && <span className="chip">disabled</span>}
                  {d.last_status === 'ok' && <span className="chip green">last send ok</span>}
                  {d.last_status === 'failed' && <span className="chip red">last send failed</span>}
                </div>
                <div className="row-split" style={{ gap: 8, justifyContent: 'flex-end' }}>
                  <button className="btn ghost sm" disabled={testing === d.id} onClick={() => sendTest(d)}>
                    <Icon name="i-test" /> {testing === d.id ? 'Sending…' : 'Send test'}
                  </button>
                  <button className="icon-action" title={`Edit ${d.name}`} aria-label={`Edit ${d.name}`}
                    onClick={() => setEditing(d)}>
                    <Icon name="i-pencil" />
                  </button>
                  <button className="icon-action danger" title={`Remove ${d.name}`} aria-label={`Remove ${d.name}`}
                    onClick={() => remove(d)}>
                    <Icon name="i-trash" />
                  </button>
                </div>
              </div>
              <p className="small muted" style={{ margin: '10px 0 0' }}>
                Receives <b>{SEV_LABEL[d.min_severity] || d.min_severity}</b> from{' '}
                <b>{d.agent_id ? (agentNames[d.agent_id] || d.agent_id) : 'all agents'}</b>
                {' · '}<span className="mono">{d.url_hint || 'no URL set'}</span>
              </p>
              {d.last_attempt_at && (
                <p className="small muted" style={{ margin: '6px 0 0' }}>
                  Last attempt {new Date(d.last_attempt_at).toLocaleString()} — {d.last_detail}
                </p>
              )}
            </div>
          ))}
          <Pager page={channelPage} noun="channels" />
        </div>
      ) : (
        <div className="grid" style={{ gap: 12 }}>
          {tab === 'open' && (
            <p className="small muted" style={{ margin: 0 }}>
              Alerts sent to a channel and not yet acknowledged or resolved. Acting on one
              from Slack or Teams moves it to the tab for that action.
            </p>
          )}
          {tab === 'open' && undelivered > 0 && (
            <div className="card pad" style={{ borderColor: 'var(--amber)' }}>
              <b className="small">
                {undelivered} alert{undelivered === 1 ? '' : 's'} reached no channel yet
              </b>
              <p className="small muted" style={{ margin: '6px 0 0' }}>
                Either no channel matches the agent, or delivery failed. They are not listed
                above until they are sent. The scheduler retries a few each sweep — check
                <b> Channels</b> for a failing webhook.
              </p>
            </div>
          )}
          {alertPage.loading && alerts.length === 0 && <p className="small muted">Loading…</p>}
          {!alertPage.loading && alerts.length === 0 && <Empty icon="i-check">No {tab} alerts</Empty>}
          {alerts.map((a) => {
            const last = a.history[a.history.length - 1];
            return (
              <div className="card pad click" key={a.id} style={{ cursor: 'pointer' }} onClick={() => openDrawer('alertDetail', a.id)}>
                <div className="row-split">
                  <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                    <SevChip sev={a.sev} /><b className="mono small">{a.rule}</b><span className="chip line">{a.agent_name}</span>
                    {a.acked && a.state === 'open' && <span className="chip blue">acknowledged</span>}
                  </div>
                  <span className="small muted mono">{a.ts}</span>
                </div>
                <p style={{ margin: '10px 0 0', fontSize: 13 }}>{a.summary}</p>
                {last && <p className="small muted" style={{ margin: '8px 0 0' }}>Last: <b>{last.action}</b> by {last.by} · {last.ts}</p>}
              </div>
            );
          })}

          <Pager page={alertPage} noun="alerts" />
        </div>
      )}

      {confirming && (
        <ConfirmPanel title={confirming.title} body={confirming.body}
          confirmLabel={confirming.label} onConfirm={confirming.run}
          onClose={() => setConfirming(null)} />
      )}
      {(addingRule || editingRule) && (
        <AlertRuleModal
          existing={editingRule || undefined}
          onClose={() => { setAddingRule(false); setEditingRule(null); }}
          onSaved={loadConditions}
        />
      )}
      {(adding || editing) && (
        <AlertDestinationModal
          existing={editing || undefined}
          onClose={() => { setAdding(false); setEditing(null); }}
          onSaved={channelPage.reload}
        />
      )}
    </div>
  );
}
