/* Add / edit an alert rule — the right-hand drawer. A rule says what counts as trouble worth an alert, over which window, for which pipeline. */
import { useState } from "react";
import { useApp } from "../../context/AppContext";
import { firstFieldError } from "../../errors";
import { useCreateAlertRuleMutation, useUpdateAlertRuleMutation } from "../../store/apiSlice";
import type { AlertRule, AlertRuleKind, AlertRuleKindInfo, AlertSeverity } from "../../types/api";
import { DIRECTION_LABEL, KIND_USES, RULE_KIND_LABEL, RULE_KIND_ORDER, SEVERITIES, WINDOWS } from "../../alertWords";
import Select from "../Select";
import { Drawer, FieldError } from "../ui";

export interface PipelineChoice { id: string; name: string; direction: string }

const toInt = (s: string): number => (/^\d+$/.test(s.trim()) ? Number(s.trim()) : NaN);

export function RuleDrawer({ rule, kinds, pipelines, onClose }: {
  /** null = a new rule. */
  rule: AlertRule | null; kinds: AlertRuleKindInfo[] | undefined; pipelines: PipelineChoice[]; onClose: () => void;
}) {
  const { ok, err } = useApp();
  const [name, setName] = useState(rule?.name ?? "");
  const [kind, setKind] = useState<AlertRuleKind>(rule?.kind ?? "pipeline_failed");
  const [severity, setSeverity] = useState<AlertSeverity>(rule?.severity ?? "medium");
  const [threshold, setThreshold] = useState(String(rule?.threshold ?? 1));
  const [windowSeconds, setWindowSeconds] = useState(String(rule?.window_seconds ?? 0));
  const [scope, setScope] = useState(rule?.scope_pipeline_id ?? "");
  const [fieldError, setFieldError] = useState<{ field: string; message: string } | null>(null);
  const [create, { isLoading: creating }] = useCreateAlertRuleMutation();
  const [update, { isLoading: updating }] = useUpdateAlertRuleMutation();
  const busy = creating || updating;

  const uses = KIND_USES[kind];
  const info = kinds?.find((k) => k.kind === kind);
  const t = toInt(threshold);
  const thresholdBad = !(t >= 1 && t <= uses.maxThreshold);
  const blocked = busy || !name.trim() || thresholdBad;
  const scopePipeline = pipelines.find((p) => p.id === scope);
  const errorFor = (f: string) => (fieldError?.field === f ? <FieldError id={`rule-${f}-error`} message={fieldError.message} /> : null);

  const pickKind = (k: AlertRuleKind) => {
    setKind(k);
    const u = KIND_USES[k];
    if (!u.window) setWindowSeconds("0");
    setThreshold(String(u.fixed ? 1 : Math.min(toInt(threshold) || 1, u.maxThreshold)));
  };

  const save = async () => {
    if (blocked) return;
    setFieldError(null);
    const body = {
      name: name.trim(), kind, severity, threshold: t, window_seconds: uses.window ? Number(windowSeconds) : 0, min_runs: 1,
      scope_pipeline_name: scopePipeline?.name ?? null,
    };
    try {
      if (rule) {
        await update({ id: rule.id, body: { ...body, scope_pipeline_id: scope } }).unwrap();
        ok(`Rule "${body.name}" saved`);
      } else {
        await create({ ...body, scope_pipeline_id: scope || null, enabled: true }).unwrap();
        ok(`Rule "${body.name}" created`);
      }
      onClose();
    } catch (e) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ field: fe.field, message: fe.message });
      else err(e);
    }
  };

  return (
    <Drawer
      title={rule ? `Edit ${rule.name}` : "New alert rule"}
      onClose={onClose}
      footer={(
        <>
          <button type="button" className="btn-s" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="btn-p" style={{ marginLeft: "auto" }} onClick={save} disabled={blocked}>
            {busy ? "Saving…" : rule ? "Save changes" : "Create rule"}
          </button>
        </>
      )}
    >
      <div className="al-form">
        <p className="ab" style={{ marginBottom: 14 }}>
          Rules are checked every minute against what DataConnect knows about your pipelines. A rule limited to one pipeline holds that pipeline to a
          tighter bar than the rest.
        </p>
        <div className="fld">
          <label htmlFor="rule-name">Rule name *</label>
          <input id="rule-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. orders-cdc-tight" maxLength={120}
            aria-invalid={fieldError?.field === "name"} />
          {errorFor("name")}
        </div>
        <div className="fg">
          <div className="fld">
            <label htmlFor="rule-kind">Kind</label>
            <Select id="rule-kind" value={kind} onChange={(e) => pickKind(e.target.value as AlertRuleKind)}>
              {RULE_KIND_ORDER.map((k) => <option key={k} value={k}>{kinds?.find((x) => x.kind === k)?.label ?? RULE_KIND_LABEL[k]}</option>)}
            </Select>
          </div>
          <div className="fld">
            <label htmlFor="rule-severity">Severity</label>
            <Select id="rule-severity" value={severity} onChange={(e) => setSeverity(e.target.value as AlertSeverity)}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
          </div>
        </div>
        {info && <p className="ab" style={{ margin: "-4px 0 12px" }}>{info.description}</p>}
        <div className={uses.window ? "fg" : undefined}>
          <div className="fld">
            <label htmlFor="rule-threshold">Threshold</label>
            <input id="rule-threshold" inputMode="numeric" value={threshold} onChange={(e) => setThreshold(e.target.value)} disabled={uses.fixed}
              aria-invalid={thresholdBad || fieldError?.field === "threshold"} aria-describedby="rule-threshold-hint" />
            <div className="ab" id="rule-threshold-hint" style={{ marginTop: 4 }}>{info?.threshold_hint}</div>
            {thresholdBad && <FieldError id="rule-threshold-range" message={`Enter a whole number from 1 to ${uses.maxThreshold.toLocaleString()}.`} />}
            {errorFor("threshold")}
          </div>
          {uses.window && (
            <div className="fld">
              <label htmlFor="rule-window">Window</label>
              <Select id="rule-window" value={windowSeconds} onChange={(e) => setWindowSeconds(e.target.value)}>
                {WINDOWS.map((w) => <option key={w.value} value={String(w.value)}>{w.label}</option>)}
              </Select>
              <div className="ab" style={{ marginTop: 4 }}>How far back each check looks.</div>
              {errorFor("window_seconds")}
            </div>
          )}
        </div>
        <div className="fld">
          <label htmlFor="rule-scope">For pipeline</label>
          <Select id="rule-scope" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">All pipelines</option>
            {pipelines.map((p) => <option key={p.id} value={p.id}>{`${p.name} (${DIRECTION_LABEL[p.direction] ?? p.direction})`}</option>)}
          </Select>
          <div className="ab" style={{ marginTop: 4 }}>Limit this rule to one pipeline, or leave it across all of them.</div>
        </div>
      </div>
    </Drawer>
  );
}
