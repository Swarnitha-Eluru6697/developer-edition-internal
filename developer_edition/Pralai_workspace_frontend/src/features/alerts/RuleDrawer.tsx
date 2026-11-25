/** Add / edit an alert rule — a right-hand slide-over. A rule says what counts as a failure worth an alert, over which window, for which job. */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { Drawer, Field } from "../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../shared/ui/drawerStyles";
import SearchSelect from "../../shared/ui/SearchSelect";
import { notify } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { AlertRule, AlertRuleKind, AlertRuleKindInfo, AlertSeverity } from "../../types";
import { useCreateAlertRuleMutation, useUpdateAlertRuleMutation } from "./api";
import { errorField, errorText } from "./errors";
import { JOB_KIND_LABEL, KIND_USES, RULE_KIND_LABEL, RULE_KIND_ORDER, SEVERITIES, WINDOWS } from "./kinds";
import type { JobChoice } from "./useJobChoices";

interface Props {
  /** null = a new rule. */
  rule: AlertRule | null;
  kinds: AlertRuleKindInfo[] | undefined;
  jobs: JobChoice[];
  onClose: () => void;
}

const toInt = (s: string): number => (/^\d+$/.test(s.trim()) ? Number(s.trim()) : NaN);

export function RuleDrawer({ rule, kinds, jobs, onClose }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const editing = !!rule;
  const [name, setName] = useState(rule?.name ?? "");
  const [kind, setKind] = useState<AlertRuleKind>(rule?.kind ?? "run_failed");
  const [severity, setSeverity] = useState<AlertSeverity>(rule?.severity ?? "medium");
  const [threshold, setThreshold] = useState(String(rule?.threshold ?? 1));
  const [windowSeconds, setWindowSeconds] = useState(String(rule?.window_seconds ?? 0));
  const [minRuns, setMinRuns] = useState(String(rule?.min_runs ?? 1));
  const [scope, setScope] = useState(rule?.scope_job_key ?? "");
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [create, { isLoading: creating }] = useCreateAlertRuleMutation();
  const [update, { isLoading: updating }] = useUpdateAlertRuleMutation();
  const busy = creating || updating;

  const uses = KIND_USES[kind];
  const info = kinds?.find((k) => k.kind === kind);
  const t = toInt(threshold);
  const m = toInt(minRuns);
  const thresholdBad = !(t >= 1 && t <= uses.maxThreshold);
  const minBad = uses.minRuns && !(m >= 1);
  const blocked = busy || !name.trim() || thresholdBad || minBad;
  const scopeJob = jobs.find((j) => j.key === scope);
  const field = (f: string) => (error?.field === f ? error.message : null);
  const errorLine = (f: string) => field(f) && <div role="alert" style={{ fontSize: 11.5, color: "var(--ws-bad, #B23B3B)", marginTop: 4 }}>{field(f)}</div>;

  const pickKind = (k: AlertRuleKind) => {
    setKind(k);
    const u = KIND_USES[k];
    if (!u.window) setWindowSeconds("0");
    if (!u.minRuns) setMinRuns("1");
    setThreshold(String(k === "flink_job_failed" ? 1 : Math.min(toInt(threshold) || 1, u.maxThreshold)));
  };

  const save = async () => {
    if (blocked) return;
    setError(null);
    const body = {
      name: name.trim(), kind, severity, threshold: t, window_seconds: uses.window ? Number(windowSeconds) : 0,
      min_runs: uses.minRuns ? m : 1, scope_job_name: scopeJob?.name ?? null,
    };
    try {
      if (rule) {
        await update({ id: rule.id, body: { ...body, scope_job_key: scope } }).unwrap();
        dispatch(notify({ kind: "success", message: `Rule "${body.name}" saved` }));
      } else {
        await create({ ...body, scope_job_key: scope || null, enabled: true }).unwrap();
        dispatch(notify({ kind: "success", message: `Rule "${body.name}" created` }));
      }
      onClose();
    } catch (e) {
      setError({ field: errorField(e), message: errorText(e, "Could not save the rule") });
    }
  };

  return (
    <Drawer
      title={editing ? `Edit ${rule.name}` : "New alert rule"}
      icon="pi-bell"
      onClose={onClose}
      busy={busy}
      footer={(
        <>
          <button onClick={onClose} disabled={busy} style={drawerBtn(false)}>Cancel</button>
          <button onClick={save} disabled={blocked} style={{ ...drawerBtn(true), marginLeft: "auto", opacity: blocked ? 0.5 : 1, cursor: blocked ? "not-allowed" : "pointer" }}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create rule"}
          </button>
        </>
      )}
    >
      <p style={{ fontSize: 12, color: "var(--ws-muted, #5b6d62)", margin: "0 0 14px", lineHeight: 1.5 }}>
        Rules are checked every minute against the runs the Workspace has seen. A rule limited to one job holds that job to a tighter bar than the rest.
      </p>

      <Field label="Rule name" required hint="Must be unique: a duplicate name is rejected.">
        <input aria-label="Rule name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. customers-tight" style={inp} maxLength={120} />
        {errorLine("name")}
      </Field>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <Field label="Kind">
          <SearchSelect ariaLabel="Kind" noun="kind" searchable={false} value={kind} onChange={(v) => pickKind(v as AlertRuleKind)}
            options={RULE_KIND_ORDER.map((k) => ({ value: k, label: kinds?.find((x) => x.kind === k)?.label ?? RULE_KIND_LABEL[k] }))} />
        </Field>
        <Field label="Severity">
          <SearchSelect ariaLabel="Severity" noun="severity" searchable={false} value={severity} onChange={(v) => setSeverity(v as AlertSeverity)}
            options={SEVERITIES.map((s) => ({ value: s, label: s }))} />
        </Field>
      </div>
      {info && <p style={{ fontSize: 11.5, color: "var(--ws-muted, #5b6d62)", margin: "-4px 0 12px", lineHeight: 1.5 }}>{info.description}</p>}

      <div style={{ display: "grid", gridTemplateColumns: uses.window ? "1fr 1fr" : "1fr", gap: 10 }}>
        <Field label="Threshold" hint={info?.threshold_hint}>
          <input aria-label="Threshold" inputMode="numeric" value={threshold} onChange={(e) => setThreshold(e.target.value)} disabled={kind === "flink_job_failed"}
            style={{ ...inp, ...(thresholdBad ? { border: "0.5px solid #b23b3b" } : {}) }} />
          {thresholdBad && <div role="alert" style={{ fontSize: 11.5, color: "var(--ws-bad, #B23B3B)", marginTop: 4 }}>Enter a whole number from 1 to {uses.maxThreshold}.</div>}
        </Field>
        {uses.window && (
          <Field label="Window" hint="How far back each check looks.">
            <SearchSelect ariaLabel="Window" noun="window" searchable={false} value={windowSeconds} onChange={setWindowSeconds}
              options={WINDOWS.map((w) => ({ value: String(w.value), label: w.label }))} />
          </Field>
        )}
      </div>

      {uses.minRuns && (
        <Field label="Minimum runs" hint="How much data before the rule may fire. 100% of two runs is noise; over eighty it is an outage.">
          <input aria-label="Minimum runs" inputMode="numeric" value={minRuns} onChange={(e) => setMinRuns(e.target.value)}
            style={{ ...inp, ...(minBad ? { border: "0.5px solid #b23b3b" } : {}) }} />
          {minBad && <div role="alert" style={{ fontSize: 11.5, color: "var(--ws-bad, #B23B3B)", marginTop: 4 }}>Enter a whole number, at least 1.</div>}
        </Field>
      )}

      <Field label="For job" hint="Limit this rule to one job, or leave it across all of them.">
        <SearchSelect ariaLabel="For job" noun="job" value={scope} onChange={setScope}
          options={[{ value: "", label: "All jobs" }, ...jobs.map((j) => ({ value: j.key, label: j.name, hint: JOB_KIND_LABEL[j.kind] }))]} />
      </Field>
      {error && !error.field && <div role="alert" style={{ fontSize: 12, color: "var(--ws-bad, #B23B3B)", marginTop: 12 }}>{error.message}</div>}
    </Drawer>
  );
}
