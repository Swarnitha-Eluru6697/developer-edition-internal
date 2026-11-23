/*
 * Alerts: trouble with this organization's pipelines — a connector failed or degraded, a deploy failed, the error threshold paused it,
 * dead-letter errors, failing quality checks — raised by rules and posted to Slack / Microsoft Teams channels (app/api/routes/alerts.py).
 *
 *   Open · Acknowledged · Resolved   the alerts themselves (a card each; click one for its history and what to do)
 *   Rules                            what raises an alert, per kind of trouble, window and pipeline
 *   Channels                         the Slack / Teams webhooks alerts are sent to (Send test, edit, remove)
 *
 * Ported from the Workspace's Alerts page (InaiEra_workspace_frontend/src/features/alerts), rebuilt with DataConnect's own pieces. Everything
 * that adds or edits something opens in the right-hand drawer, never a centred dialog. The tab survives a refresh; arriving from the sidebar
 * opens Open (DataConnectApp.tsx marks that arrival).
 */
import React, { useEffect, useMemo, useState } from "react";
import { useApp } from "../context/AppContext";
import { usePersistedChoice } from "../hooks/usePersistedChoice";
import { ALERT_COUNTS_POLL_MS } from "../pollingPolicy";
import type { PageFocusProps, SearchTarget } from "../searchTarget";
import {
  useDeleteAlertChannelMutation, useDeleteAlertRuleMutation, useGetAlertCountsQuery, useGetPipelinesQuery, useListAlertChannelsQuery,
  useListAlertRuleKindsQuery, useListAlertRulesQuery, useListAlertsQuery, useRunAlertRulesMutation, useTestAlertChannelMutation,
  useUpdateAlertRuleMutation,
} from "../store/apiSlice";
import type { Alert, AlertChannel, AlertRule, AlertState } from "../types/api";
import {
  ALERT_TABS, type AlertTab, PAGE_SIZE, RULE_KIND_LABEL, SEVERITY_FLOOR_SHORT, describeRule, formatTs, isAlertState, showing,
} from "../alertWords";
import { ErrBox, IconDel, IconEdit, IconTest, PH, Pill } from "../components/ui";
import RowMenu from "../components/RowMenu";
import { AlertDetailDrawer } from "../components/alerts/AlertDetailDrawer";
import { ChannelDrawer } from "../components/alerts/ChannelDrawer";
import { RuleDrawer, type PipelineChoice } from "../components/alerts/RuleDrawer";
import { SeverityPill, Toggle } from "../components/alerts/AlertBits";

/** Set by the shell when Alerts is opened from the sidebar (not by a refresh): open the Open tab then, whatever was remembered. */
export const ALERTS_FROM_NAV_KEY = "dc_alerts_from_nav";

const INTRO: Record<AlertState, string> = {
  open: "Alerts sent to a channel and not yet acknowledged or resolved.",
  acknowledged: "Alerts someone has acknowledged but not resolved yet.",
  resolved: "Alerts that were resolved by a person, or cleared by themselves when the trouble stopped.",
};
const EMPTY: Record<AlertState, string> = {
  open: "No open alerts. A failed or degraded pipeline raises one here once it has been sent to a channel.",
  acknowledged: "Nothing is acknowledged right now.",
  resolved: "Nothing has been resolved yet.",
};

/** Reads the sidebar's mark without clearing it: React may run a state initializer twice, and the second run must see the same answer. */
function initialTab(remembered: AlertTab): AlertTab {
  try {
    if (sessionStorage.getItem(ALERTS_FROM_NAV_KEY)) return "open";
  } catch { /* storage unavailable: keep what was remembered */ }
  return remembered;
}

interface Props extends PageFocusProps { openTarget?: (t: SearchTarget) => void }

export default function Alerts({ openTarget }: Props) {
  const { openModal, closeModal, ok, err, toast, can } = useApp();
  const canManage = can("dataconnect:alerts:manage");
  const canAck = can("dataconnect:alerts:respond");
  const [remembered, remember] = usePersistedChoice<AlertTab>("dc_alerts_tab", ALERT_TABS, "open");
  const [tab, setTabState] = useState<AlertTab>(() => initialTab(remembered));
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const setTab = (t: AlertTab) => { setTabState(t); remember(t); setOffset(0); };
  useEffect(() => {                                                   // record the opening tab once; a refresh from here keeps it
    remember(tab);
    try { sessionStorage.removeItem(ALERTS_FROM_NAV_KEY); } catch { /* nothing to clear */ }
  }, []);                                                            // eslint-disable-line react-hooks/exhaustive-deps

  const listing = isAlertState(tab);
  const { data: counts } = useGetAlertCountsQuery(undefined, { pollingInterval: ALERT_COUNTS_POLL_MS });
  const { data: page, isLoading: loadingAlerts, error: alertsError } = useListAlertsQuery(
    { state: listing ? tab : "open", from: from || undefined, to: to || undefined, limit: PAGE_SIZE, offset },
    { skip: !listing, pollingInterval: ALERT_COUNTS_POLL_MS },
  );
  const { data: rules, isLoading: loadingRules, error: rulesError } = useListAlertRulesQuery(undefined, { skip: tab !== "rules" });
  const { data: kinds } = useListAlertRuleKindsQuery();
  const { data: channels, isLoading: loadingChannels, error: channelsError } = useListAlertChannelsQuery(undefined, { skip: tab !== "channels" });
  const { data: pipesData } = useGetPipelinesQuery();
  const pipelines: PipelineChoice[] = useMemo(() => (pipesData ?? [])
    .map((p) => ({ id: p.id, name: p.name, direction: (p.direction ?? "SOURCE").toUpperCase() }))
    .sort((a, b) => a.name.localeCompare(b.name)), [pipesData]);
  const [runRules, { isLoading: running }] = useRunAlertRulesMutation();

  const setDate = (set: (v: string) => void) => (v: string) => { set(v); setOffset(0); };
  const run = async () => {
    try {
      const r = await runRules().unwrap();
      if (!r.ran) toast(r.detail);
      else ok(`Rules checked: ${r.raised} raised, ${r.cleared} cleared, ${r.delivered} sent${r.detail === "ok" ? "" : ` (${r.detail})`}`);
    } catch (e) { err(e); }
  };
  const openAlert = (a: Alert) => openModal(
    <AlertDetailDrawer alert={a} canAct={canAck} onClose={closeModal}
      onOpenPipeline={() => { closeModal(); openTarget?.({ kind: "pipeline", page: "pip", id: a.pipeline_id, name: a.pipeline_name }); }} />,
  );
  const editRule = (rule: AlertRule | null) => openModal(<RuleDrawer rule={rule} kinds={kinds} pipelines={pipelines} onClose={closeModal} />);
  const editChannel = (channel: AlertChannel | null) => openModal(<ChannelDrawer channel={channel} pipelines={pipelines} onClose={closeModal} />);

  const total = page?.total ?? 0;
  const shown = page?.items.length ?? 0;
  const tabKey = (e: React.KeyboardEvent) => {
    const i = ALERT_TABS.indexOf(tab);
    const next = e.key === "ArrowRight" ? ALERT_TABS[(i + 1) % ALERT_TABS.length] : e.key === "ArrowLeft" ? ALERT_TABS[(i + ALERT_TABS.length - 1) % ALERT_TABS.length] : null;
    if (!next) return;
    e.preventDefault();
    setTab(next);
    document.getElementById(`alerts-tab-${next}`)?.focus();
  };

  return (
    <div className="page-fill">
      <PH title="Alerts" sub="Failures of your pipelines, raised by rules and sent to Slack or Microsoft Teams."
        right={tab === "rules" && canManage ? (
          <>
            <button type="button" className="btn-s" onClick={run} disabled={running}>{running ? "Checking…" : "Run rules now"}</button>
            <button type="button" className="btn-p" onClick={() => editRule(null)}>+ New rule</button>
          </>
        ) : tab === "channels" && canManage ? (
          <button type="button" className="btn-p" onClick={() => editChannel(null)}>+ Add channel</button>
        ) : null} />

      <div className="page-scroll">
        <div className="dc-tabs ptabs al-tabs" role="tablist" aria-label="Alert views" onKeyDown={tabKey}>
          {ALERT_TABS.map((t) => {
            const n = isAlertState(t) ? counts?.[t] : undefined;
            return (
              <button key={t} id={`alerts-tab-${t}`} type="button" role="tab" aria-selected={tab === t} aria-controls="alerts-panel"
                tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>
                {t.charAt(0).toUpperCase() + t.slice(1)}
                {!!n && <span className={`badge ${t === "open" ? "br" : t === "acknowledged" ? "bb" : "bg"}`}>{n}</span>}
              </button>
            );
          })}
        </div>

        <div id="alerts-panel" role="tabpanel" aria-labelledby={`alerts-tab-${tab}`} className="al-panel">
          {listing && (
            <>
              <div className="al-dates">
                <label>From <input type="date" aria-label="From date" value={from} max={to || undefined} onChange={(e) => setDate(setFrom)(e.target.value)} /></label>
                <label>To <input type="date" aria-label="To date" value={to} min={from || undefined} onChange={(e) => setDate(setTo)(e.target.value)} /></label>
                {(from || to) && <button type="button" className="chip-x" onClick={() => { setFrom(""); setTo(""); setOffset(0); }}><span>Clear dates</span><span aria-hidden="true">×</span></button>}
              </div>
              <p className="ab al-intro">{INTRO[tab]}</p>
              {tab === "open" && <UndeliveredNote count={page?.undelivered ?? 0} onChannels={() => setTab("channels")} />}
              {alertsError ? <ErrBox error={alertsError} /> : (
                <AlertCards alerts={page?.items} loading={loadingAlerts} emptyText={from || to ? "No alerts in these dates." : EMPTY[tab]} onOpen={openAlert} />
              )}
              {total > 0 && (
                <div className="al-pager">
                  <span>{showing(offset, shown, total, "alert")}</span>
                  <span>
                    <button type="button" className="btn-s btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
                    <button type="button" className="btn-s btn-sm" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
                  </span>
                </div>
              )}
            </>
          )}
          {tab === "rules" && (rulesError ? <ErrBox error={rulesError} /> : (
            <RulesTable rules={rules} loading={loadingRules} canManage={canManage} onEdit={editRule} kindLabel={(k) => kinds?.find((x) => x.kind === k)?.label ?? RULE_KIND_LABEL[k]} />
          ))}
          {tab === "channels" && (channelsError ? <ErrBox error={channelsError} /> : (
            <ChannelCards channels={channels} loading={loadingChannels} canManage={canManage} onEdit={editChannel} />
          ))}
        </div>
      </div>
    </div>
  );
}

function AlertCards({ alerts, loading, emptyText, onOpen }: { alerts: Alert[] | undefined; loading: boolean; emptyText: string; onOpen: (a: Alert) => void }) {
  if (loading && !alerts) return <div className="empty">Loading alerts…</div>;
  if (!alerts?.length) return <div className="empty al-empty"><b>Nothing here</b><span>{emptyText}</span></div>;
  return (
    <div className="al-cards">
      {alerts.map((a) => (
        <button key={a.id} type="button" className="al-card" onClick={() => onOpen(a)} aria-label={`Open alert ${a.rule_name} for ${a.pipeline_name}`}>
          <span className="al-card-top">
            <SeverityPill severity={a.severity} />
            <b className="mono">{a.rule_name}</b>
            <Pill cls="bgr">{a.pipeline_name}</Pill>
            {a.state === "acknowledged" && <Pill cls="bb">acknowledged</Pill>}
            <span className="al-when mono">{formatTs(a.raised_at)}</span>
          </span>
          <span className="al-card-sum">{a.summary}</span>
          <span className="al-card-last">Last: <b>{a.last_action}</b> by {a.last_actor} · {formatTs(a.last_at)}</span>
        </button>
      ))}
    </div>
  );
}

/** Open alerts no channel has received (none matches, or delivery failed): they are not listed until they are sent. */
function UndeliveredNote({ count, onChannels }: { count: number; onChannels: () => void }) {
  if (!count) return null;
  return (
    <div role="status" className="alert alert-w al-undelivered">
      <div className="at">{count} alert{count === 1 ? "" : "s"} reached no channel yet</div>
      <div className="ab">
        Either no channel matches the pipeline, or delivery failed. They are not listed below until they are sent; the sweep retries a few times. Add a channel
        or check <button type="button" className="al-link" onClick={onChannels}>Channels</button> for a failing webhook.
      </div>
    </div>
  );
}

function RulesTable({ rules, loading, canManage, onEdit, kindLabel }: {
  rules: AlertRule[] | undefined; loading: boolean; canManage: boolean; onEdit: (r: AlertRule) => void; kindLabel: (k: AlertRule["kind"]) => string;
}) {
  const { ok, err } = useApp();
  const [update] = useUpdateAlertRuleMutation();
  const [remove] = useDeleteAlertRuleMutation();
  const toggle = async (r: AlertRule, enabled: boolean) => {
    try { await update({ id: r.id, body: { enabled } }).unwrap(); } catch (e) { err(e); }
  };
  const del = async (r: AlertRule) => {
    if (!window.confirm(`Delete rule "${r.name}"? It will stop raising alerts. Alerts it already raised stay as they are.`)) return;
    try { await remove(r.id).unwrap(); ok(`Deleted rule "${r.name}"`); } catch (e) { err(e); }
  };
  return (
    <>
      <p className="ab al-intro">
        What raises an alert — checked every minute against what DataConnect knows about your pipelines. A rule limited to one pipeline holds that pipeline
        to a tighter bar than the rest.
      </p>
      <div className="tbl" role="table" aria-label="Alert rules" style={{ "--cols": "1.4fr 1.2fr 2fr 1.2fr minmax(110px, 0.6fr)" } as React.CSSProperties}>
        <div className="thead" role="row">
          <div className="th" role="columnheader">Rule</div><div className="th" role="columnheader">Kind</div>
          <div className="th" role="columnheader">Fires when</div><div className="th" role="columnheader">Pipeline</div>
          <div className="th th-actions" role="columnheader">Actions</div>
        </div>
        <div className="tbody" role="rowgroup">
          {loading && !rules && <div className="empty">Loading rules…</div>}
          {rules?.length === 0 && <div className="empty">No rules: nothing will raise an alert.</div>}
          {rules?.map((r) => (
            <div key={r.id} className={`tr${r.enabled ? "" : " al-off"}`} role="row">
              <div className="td mono" role="cell" style={{ fontWeight: 600 }}>{r.name}</div>
              <div className="td" role="cell"><Pill cls="bgr">{kindLabel(r.kind).toLowerCase()}</Pill></div>
              <div className="td" role="cell" style={{ gap: 7, flexWrap: "wrap" }}><SeverityPill severity={r.severity} /><span className="tdm" style={{ padding: 0 }}>{describeRule(r)}</span></div>
              <div className="td" role="cell">{r.scope_pipeline_name ?? <span className="tdm" style={{ padding: 0 }}>all pipelines</span>}</div>
              <div className="acts" role="cell" style={{ justifyContent: "center", gap: 8 }}>
                <Toggle checked={r.enabled} onChange={(on) => toggle(r, on)} disabled={!canManage} label={`${r.enabled ? "Disable" : "Enable"} ${r.name}`} />
                {canManage && (
                  <RowMenu label={`Actions for ${r.name}`} items={[
                    { key: "edit", label: "Edit", ariaLabel: `Edit ${r.name}`, icon: <IconEdit />, onSelect: () => onEdit(r) },
                    { key: "delete", label: "Delete", ariaLabel: `Delete ${r.name}`, icon: <IconDel />, danger: true, onSelect: () => del(r) },
                  ]} />
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

const CHANNEL_KIND_LABEL = { slack: "Slack", teams: "Teams" } as const;

function ChannelCards({ channels, loading, canManage, onEdit }: {
  channels: AlertChannel[] | undefined; loading: boolean; canManage: boolean; onEdit: (c: AlertChannel) => void;
}) {
  const { ok, err } = useApp();
  const [test, { isLoading: testing, originalArgs }] = useTestAlertChannelMutation();
  const [remove] = useDeleteAlertChannelMutation();
  const sendTest = async (c: AlertChannel) => {
    try {
      const r = await test(c.id).unwrap();
      if (r.ok) ok(`Test message sent to ${c.name}`);
      else err(new Error(`Test to ${c.name} failed: ${r.detail}`));
    } catch (e) { err(e); }
  };
  const del = async (c: AlertChannel) => {
    if (!window.confirm(`Remove channel "${c.name}"? It will stop receiving alerts. The webhook itself is not touched.`)) return;
    try { await remove(c.id).unwrap(); ok(`Removed channel "${c.name}"`); } catch (e) { err(e); }
  };
  if (loading && !channels) return <div className="empty">Loading channels…</div>;
  return (
    <>
      <p className="ab al-intro">
        Alerts are posted to the channels below. A channel limited to one pipeline receives only that pipeline's alerts; a severity floor keeps quieter
        notices out of an on-call channel.
      </p>
      {channels?.length === 0 && (
        <div className="empty al-empty"><b>No channels yet</b>
          <span>Add a Slack or Microsoft Teams channel: alerts are only sent to channels, and wait on the Open tab until there is one.</span></div>
      )}
      <div className="al-cards">
        {channels?.map((c) => {
          const status = c.last_ok === null ? { cls: "bgr", text: "not used yet" } : c.last_ok ? { cls: "bg", text: "last send ok" } : { cls: "br", text: "last send failed" };
          return (
            <div key={c.id} className={`al-channel${c.enabled ? "" : " al-off"}`} aria-label={`Channel ${c.name}`}>
              <div className="al-card-top">
                <b>{c.name}</b>
                <Pill cls="bgr">{CHANNEL_KIND_LABEL[c.kind]}</Pill>
                <Pill cls={status.cls}>{status.text}</Pill>
                {!c.enabled && <Pill cls="bgr">off</Pill>}
                {canManage && (
                  <span className="al-channel-acts">
                    <button type="button" className="btn-s btn-sm al-test" onClick={() => sendTest(c)} disabled={testing && originalArgs === c.id}
                      title={`Send a test message to ${c.name}`}>
                      <IconTest />{testing && originalArgs === c.id ? "Sending…" : "Send test"}
                    </button>
                    <RowMenu label={`Actions for ${c.name}`} items={[
                      { key: "edit", label: "Edit", ariaLabel: `Edit ${c.name}`, icon: <IconEdit />, onSelect: () => onEdit(c) },
                      { key: "delete", label: "Remove", ariaLabel: `Remove ${c.name}`, icon: <IconDel />, danger: true, onSelect: () => del(c) },
                    ]} />
                  </span>
                )}
              </div>
              <p className="ab">
                Receives <b>{SEVERITY_FLOOR_SHORT[c.min_severity]}</b> from <b>{c.scope_pipeline_name ?? "all pipelines"}</b> · <span className="mono">{c.url_hint}</span>
              </p>
              <p className="tdm" style={{ padding: 0 }}>{c.last_attempt_at ? <>Last attempt {formatTs(c.last_attempt_at)} — {c.last_detail ?? ""}</> : "No messages sent yet."}</p>
            </div>
          );
        })}
      </div>
      {channels && channels.length > 0 && <div className="al-pager"><span>{showing(0, channels.length, channels.length, "channel")}</span></div>}
    </>
  );
}
