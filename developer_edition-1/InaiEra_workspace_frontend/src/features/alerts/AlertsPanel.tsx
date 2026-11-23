/**
 * Alerts page (Governance). Failures of the Workspace's jobs — dbt / SQL / Python runs, Flink jobs, runs Dagster refused — raised by rules
 * and posted to Slack / Microsoft Teams channels.
 *
 *   Open · Acknowledged · Resolved   the alerts themselves (a card each; click one for its history and what to do)
 *   Rules                            what raises an alert, per kind of failure, window and job
 *   Channels                         the Slack / Teams webhooks alerts are sent to (Send test, edit, remove)
 *
 * Everything that adds or edits something opens as a slide-over on the RIGHT (the Workspace's own drawer), never a centred dialog.
 * Which tab you are on is remembered across a refresh (src/shared/usePersistedState.ts).
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { PageHeader } from "../../shared/ui/PageHeader";
import { Glyph } from "../../shared/ui/Glyph";
import { pageBtn } from "../../shared/ui/pageStyles";
import { oneOf, usePersistedState } from "../../shared/usePersistedState";
import { useHasPermission } from "../../rbac/hooks";
import { notify } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { Alert, AlertChannel, AlertRule, AlertState } from "../../types";
import {
  useGetAlertCountsQuery, useListAlertChannelsQuery, useListAlertRuleKindsQuery, useListAlertRulesQuery,
  useListAlertsQuery, useRunAlertRulesMutation,
} from "./api";
import { AlertDetailDrawer } from "./AlertDetailDrawer";
import { AlertCards, UndeliveredNote } from "./AlertList";
import { ChannelDrawer } from "./ChannelDrawer";
import { ChannelsTab } from "./ChannelsTab";
import { errorText } from "./errors";
import { RuleDrawer } from "./RuleDrawer";
import { RulesTab } from "./RulesTab";
import { PAGE_SIZE, showing } from "./format";
import { useJobChoices } from "./useJobChoices";

export type AlertTab = AlertState | "rules" | "channels";
const TABS: AlertTab[] = ["open", "acknowledged", "resolved", "rules", "channels"];
const isAlertState = (t: AlertTab): t is AlertState => t === "open" || t === "acknowledged" || t === "resolved";

const INTRO: Record<AlertState, string> = {
  open: "Alerts sent to a channel and not yet acknowledged or resolved.",
  acknowledged: "Alerts someone has acknowledged but not resolved yet.",
  resolved: "Alerts that were resolved by a person, or cleared by themselves when the failure stopped.",
};
const EMPTY: Record<AlertState, string> = {
  open: "No open alerts. A failed job raises one here once it has been sent to a channel.",
  acknowledged: "Nothing is acknowledged right now.",
  resolved: "Nothing has been resolved yet.",
};

const dateInput = { height: 30, border: "0.5px solid #d1d5db", borderRadius: 6, padding: "0 8px", fontSize: 12.5, fontFamily: "inherit", background: "#fff" } as const;

export function AlertsPanel() {
  const dispatch = useDispatch<AppDispatch>();
  const canManage = useHasPermission("workspace:alerts:manage");
  const canAck = useHasPermission("workspace:alerts:edit");
  const [tab, setTab] = usePersistedState<AlertTab>("alerts.tab", "open", oneOf<AlertTab>(...TABS));
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Alert | null>(null);
  const [ruleDrawer, setRuleDrawer] = useState<{ rule: AlertRule | null } | null>(null);
  const [channelDrawer, setChannelDrawer] = useState<{ channel: AlertChannel | null } | null>(null);
  const jobs = useJobChoices();

  const listing = isAlertState(tab);
  const { data: counts } = useGetAlertCountsQuery(undefined, { pollingInterval: 30000 });
  const { data: page, isLoading: loadingAlerts } = useListAlertsQuery(
    { state: listing ? tab : "open", from: from || undefined, to: to || undefined, limit: PAGE_SIZE, offset },
    { skip: !listing, pollingInterval: 30000 },
  );
  const { data: rules, isLoading: loadingRules } = useListAlertRulesQuery(undefined, { skip: tab !== "rules" });
  const { data: kinds } = useListAlertRuleKindsQuery(undefined, { skip: tab !== "rules" && !ruleDrawer });
  const { data: channels, isLoading: loadingChannels } = useListAlertChannelsQuery(undefined, { skip: tab !== "channels" });
  const [runRules, { isLoading: running }] = useRunAlertRulesMutation();

  const goTab = (t: AlertTab) => { setTab(t); setOffset(0); };
  const setDate = (set: (v: string) => void) => (v: string) => { set(v); setOffset(0); };

  const run = async () => {
    try {
      const r = await runRules().unwrap();
      dispatch(notify({
        kind: r.ran && r.detail === "ok" ? "success" : "info",
        message: !r.ran ? r.detail : `Rules checked: ${r.raised} raised, ${r.cleared} cleared, ${r.delivered} sent${r.detail === "ok" ? "" : ` (${r.detail})`}`,
      }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: errorText(e, "Could not run the rules") }));
    }
  };

  const total = page?.total ?? 0;
  const shown = page?.items.length ?? 0;
  const noun = listing ? "alert" : tab === "rules" ? "rule" : "channel";

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "#fff" }}>
      <PageHeader
        eyebrow="Operate"
        title="Alerts"
        subtitle="Failures of your jobs — dbt, SQL, Python and Flink — raised by rules and sent to Slack or Microsoft Teams."
        actions={tab === "rules" && canManage ? (
          <>
            <button onClick={run} disabled={running} style={pageBtn("ghost", running)}>
              <i className="ti ti-bolt" style={{ fontSize: 14 }} />{running ? "Checking…" : "Run rules now"}
            </button>
            <button onClick={() => setRuleDrawer({ rule: null })} style={pageBtn()}>
              <i className="ti ti-plus" style={{ fontSize: 14 }} />New rule
            </button>
          </>
        ) : tab === "channels" && canManage ? (
          <button onClick={() => setChannelDrawer({ channel: null })} style={pageBtn()}>
            <i className="ti ti-plus" style={{ fontSize: 14 }} />Add channel
          </button>
        ) : undefined}
      />

      <div role="tablist" aria-label="Alert views" style={{ display: "flex", borderBottom: "0.5px solid #E5E7EB", background: "#F9FAFB", flexShrink: 0, padding: "0 12px" }}>
        {TABS.map((t) => {
          const n = isAlertState(t) ? counts?.[t] : undefined;
          return (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => goTab(t)}
              style={{
                padding: "10px 16px", fontSize: 12.5, cursor: "pointer", textTransform: "capitalize", background: "none", border: "none", fontFamily: "inherit",
                display: "flex", alignItems: "center", gap: 6, color: tab === t ? "#1F2937" : "#888", fontWeight: tab === t ? 500 : 400,
                borderBottom: tab === t ? "2px solid #F97316" : "2px solid transparent",
              }}>
              {t}
              {!!n && <span style={{ background: "#fef3c7", color: "#b45309", padding: "1px 6px", borderRadius: 4, fontSize: 10.5, fontWeight: 600 }}>{n}</span>}
            </button>
          );
        })}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "16px 28px 24px" }}>
        {listing && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap", fontSize: 12.5, color: "#6b7280" }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6 }}>From
                <input type="date" aria-label="From date" value={from} max={to || undefined} onChange={(e) => setDate(setFrom)(e.target.value)} style={dateInput} />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 6 }}>To
                <input type="date" aria-label="To date" value={to} min={from || undefined} onChange={(e) => setDate(setTo)(e.target.value)} style={dateInput} />
              </label>
              {(from || to) && (
                <button type="button" onClick={() => { setFrom(""); setTo(""); setOffset(0); }} title="Clear dates"
                  style={{ display: "inline-flex", alignItems: "center", gap: 4, height: 28, padding: "0 9px", fontSize: 12, background: "#fff", border: "1px solid #E5E7EB", borderRadius: 6, cursor: "pointer", color: "#374151" }}>
                  <Glyph name="pi-x" size={12} />Clear dates
                </button>
              )}
            </div>
            <p style={{ fontSize: 12.5, color: "#6b7280", margin: "0 0 12px" }}>{INTRO[tab]}</p>
            {tab === "open" && <UndeliveredNote count={page?.undelivered ?? 0} onChannels={() => goTab("channels")} />}
            <AlertCards alerts={page?.items} loading={loadingAlerts} emptyText={from || to ? "No alerts in these dates." : EMPTY[tab]} onOpen={setSelected} />
          </>
        )}
        {tab === "rules" && (
          <RulesTab rules={rules} loading={loadingRules} kinds={kinds} canManage={canManage} onEdit={(rule) => setRuleDrawer({ rule })} />
        )}
        {tab === "channels" && (
          <ChannelsTab channels={channels} loading={loadingChannels} canManage={canManage} onEdit={(channel) => setChannelDrawer({ channel })} />
        )}

        {listing && total > 0 && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 14, fontSize: 12, color: "#9ca3af" }}>
            <span>{showing(offset, shown, total, noun)}</span>
            <div style={{ display: "flex", gap: 8 }}>
              <PagerButton label="Previous" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} />
              <PagerButton label="Next" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)} />
            </div>
          </div>
        )}
        {tab === "rules" && rules && rules.length > 0 && <div style={{ marginTop: 14, fontSize: 12, color: "#9ca3af" }}>{showing(0, rules.length, rules.length, noun)}</div>}
        {tab === "channels" && channels && channels.length > 0 && <div style={{ marginTop: 14, fontSize: 12, color: "#9ca3af" }}>{showing(0, channels.length, channels.length, noun)}</div>}
      </div>

      {selected && <AlertDetailDrawer alert={selected} canAct={canAck} onClose={() => setSelected(null)} />}
      {ruleDrawer && <RuleDrawer rule={ruleDrawer.rule} kinds={kinds} jobs={jobs} onClose={() => setRuleDrawer(null)} />}
      {channelDrawer && <ChannelDrawer channel={channelDrawer.channel} jobs={jobs} onClose={() => setChannelDrawer(null)} />}
    </div>
  );
}

function PagerButton({ label, disabled, onClick }: { label: string; disabled: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      style={{ height: 28, padding: "0 12px", fontSize: 12, fontWeight: 600, background: "#fff", color: "#1F2937", border: "1px solid #E5E7EB", borderRadius: 8,
        cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1 }}>
      {label}
    </button>
  );
}
