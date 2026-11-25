/** Rules: what raises an alert. A table with an on/off switch, edit and delete per rule. */
import { useDispatch } from "react-redux";
import { askConfirm } from "../../shared/ui/dialogStore";
import { ACTION_ICON, IconAction } from "../../shared/ui/IconAction";
import { notify } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { AlertRule, AlertRuleKindInfo } from "../../types";
import { useDeleteAlertRuleMutation, useUpdateAlertRuleMutation } from "./api";
import { errorText } from "./errors";
import { RULE_KIND_LABEL, describeRule } from "./kinds";
import { Chip, SeverityChip, Toggle } from "./ui";
import { mono } from "./format";

const COLS = "minmax(0,1.5fr) minmax(0,1.1fr) minmax(0,1.8fr) minmax(0,1.1fr) 150px";
const row = { display: "grid", gridTemplateColumns: COLS, columnGap: 16, alignItems: "center", padding: "10px 16px" } as const;

interface Props {
  rules: AlertRule[] | undefined;
  loading: boolean;
  kinds: AlertRuleKindInfo[] | undefined;
  canManage: boolean;
  onEdit: (r: AlertRule) => void;
}

export function RulesTab({ rules, loading, kinds, canManage, onEdit }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const [update] = useUpdateAlertRuleMutation();
  const [remove] = useDeleteAlertRuleMutation();
  const label = (k: AlertRule["kind"]) => kinds?.find((x) => x.kind === k)?.label.toLowerCase() ?? RULE_KIND_LABEL[k];

  const toggle = async (r: AlertRule, enabled: boolean) => {
    try { await update({ id: r.id, body: { enabled } }).unwrap(); }
    catch (e) { dispatch(notify({ kind: "error", message: errorText(e, "Could not change the rule") })); }
  };
  const del = async (r: AlertRule) => {
    if (!(await askConfirm({ title: "Delete rule?", message: `"${r.name}" will stop raising alerts. Alerts it already raised stay as they are.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await remove(r.id).unwrap();
      dispatch(notify({ kind: "success", message: `Deleted rule "${r.name}"` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: errorText(e, "Delete failed") }));
    }
  };

  return (
    <div>
      <p style={{ fontSize: 12.5, color: "#5b6d62", margin: "0 0 12px", lineHeight: 1.5 }}>
        Configure what raises an alert — stored as data and checked every minute against the runs, Flink jobs and Dagster refusals the Workspace has seen.
        A rule limited to one job holds that job to a tighter bar than the rest.
      </p>
      <div style={{ border: "1px solid #E3E8E2", borderRadius: 12, overflow: "hidden", background: "#fff" }}>
        <div style={{ ...row, padding: "8px 16px", fontSize: 10.5, color: "#73827a", textTransform: "uppercase", letterSpacing: ".06em", background: "#F8FAF9", borderBottom: "0.5px solid #E3E8E2" }}>
          <div>Rule</div><div>Kind</div><div>Fires when</div><div>Job</div><div />
        </div>
        {loading && !rules && <div style={{ padding: 24, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading rules…</div>}
        {rules?.length === 0 && <div style={{ padding: 24, textAlign: "center", color: "#73827a", fontSize: 13 }}>No rules: nothing will raise an alert.</div>}
        {rules?.map((r) => (
          <div key={r.id} style={{ ...row, borderBottom: "0.5px solid #eef2ed", opacity: r.enabled ? 1 : 0.55 }}>
            <b style={{ ...mono, fontSize: 12.5, overflowWrap: "anywhere" }}>{r.name}</b>
            <div><Chip>{label(r.kind)}</Chip></div>
            <div style={{ fontSize: 12.5, color: "#5b6d62", display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
              <SeverityChip severity={r.severity} />{describeRule(r)}
            </div>
            <div style={{ fontSize: 12.5 }}>{r.scope_job_name ?? <span style={{ color: "#73827a" }}>all jobs</span>}</div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
              <Toggle checked={r.enabled} onChange={(on) => toggle(r, on)} disabled={!canManage} label={`${r.enabled ? "Disable" : "Enable"} ${r.name}`} />
              {canManage && <IconAction icon={ACTION_ICON.edit} label={`Edit ${r.name}`} onClick={() => onEdit(r)} />}
              {canManage && <IconAction icon={ACTION_ICON.delete} label={`Delete ${r.name}`} danger onClick={() => del(r)} />}
            </div>
          </div>
        ))}
      </div>

    </div>
  );
}
