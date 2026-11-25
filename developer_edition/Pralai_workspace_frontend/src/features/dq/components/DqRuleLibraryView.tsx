/**
 * Rule library tab. A rule is a reusable definition with no table attached
 * ("amount must be positive"); a ruleset pins it to a column. System rules ship
 * with the DQ service and are read-only; your organisation's rules can be edited
 * and deleted. Name, type and dimension are fixed once a rule exists (a new type
 * would invalidate every artifact already rendered from it).
 */
import { Fragment, useState } from "react";
import { useDispatch } from "react-redux";
import { useCreateDqRuleMutation, useDeleteDqRuleMutation, useListDqRulesQuery, useUpdateDqRuleMutation } from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import { askConfirm } from "../../../shared/ui/dialogStore";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import SearchSelect from "../../../shared/ui/SearchSelect";
import type { AppDispatch } from "../../../store";
import type { DqDimension, DqRule, DqRuleType, DqSeverity } from "../../../types";
import {
  DIMENSIONS, RULE_TYPES, SEVERITIES, cellEllipsis, chip, codeBlock, dimStyle, parseJsonObject, sevStyle, tableHead, tableRow,
} from "../dqUi";
import { Empty, ErrorBox } from "./DqRulesetsView";

type Scope = "system" | "org";
const COLS = "1.6fr 1fr 0.9fr 0.8fr 1.2fr 70px";
const EMPTY = {
  name: "", display_name: "", description: "", rule_type: "not_null" as DqRuleType, dimension: "nulls" as DqDimension,
  severity: "ERROR" as DqSeverity, parameters: "{}", tags: "",
};
const tagList = (s: string) => s.split(",").map((t) => t.trim()).filter(Boolean);

export function DqRuleLibraryView({ canEdit, creating, onCloseCreate, scope, onScope }: {
  canEdit: boolean; creating: boolean; onCloseCreate: () => void; scope: Scope; onScope: (s: Scope) => void;
}) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: rules = [], isLoading, error } = useListDqRulesQuery();
  const [deleteRule] = useDeleteDqRuleMutation();
  const [dim, setDim] = useState("");
  const [sev, setSev] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState<DqRule | null>(null);

  const nSystem = rules.filter((r) => r.is_system_rule).length;
  const isSystem = scope === "system";
  const shown = rules.filter((r) => (isSystem ? r.is_system_rule : !r.is_system_rule)
    && (!dim || r.dimension === dim) && (!sev || r.severity === sev));
  const editable = canEdit && !isSystem;

  const remove = async (r: DqRule) => {
    if (!(await askConfirm({ title: "Delete rule", message: `Delete "${r.name}"? Rulesets that use it lose this check.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await deleteRule(r.id).unwrap();
      dispatch(notify({ kind: "success", message: `Deleted "${r.name}"` }));
    } catch (e) { dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not delete the rule", { prefer: "detail" }) })); }
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 16px", borderBottom: "0.5px solid #E3E8E2", flexWrap: "wrap" }}>
        {(["system", "org"] as const).map((s) => (
          <button key={s} onClick={() => onScope(s)}
            style={{ border: "0.5px solid", borderColor: scope === s ? "#0A7D5A" : "#cfd8d1", background: scope === s ? "#E3F6EA" : "#fff",
              color: scope === s ? "#086347" : "#2E3D36", borderRadius: 999, padding: "4px 12px", fontSize: 12, cursor: "pointer" }}>
            {s === "system" ? `System rules (${nSystem})` : `Your rules (${rules.length - nSystem})`}
          </button>
        ))}
        <span style={{ fontSize: 11.5, color: "#73827a", marginLeft: 4 }}>
          {isSystem ? "Ship with the platform: usable in any ruleset, not editable." : "Defined by you: edit or delete them here."}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <SearchSelect size="sm" ariaLabel="Filter by dimension" noun="dimension" style={{ width: 150 }} value={dim} onChange={setDim}
            options={[{ value: "", label: "All dimensions" }, ...DIMENSIONS.map((d) => ({ value: d, label: d }))]} />
          <SearchSelect size="sm" ariaLabel="Filter by severity" noun="severity" style={{ width: 150 }} value={sev} onChange={setSev}
            options={[{ value: "", label: "All severities" }, ...SEVERITIES.map((s) => ({ value: s, label: s }))]} />
        </div>
      </div>

      {isLoading && <div style={{ padding: 24, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading the rule library…</div>}
      {error && <ErrorBox error={error} what="Could not load the rule library." />}
      {!isLoading && !error && !shown.length && (
        <Empty icon="ti-list-details"
          title={dim || sev ? "No rules match these filters" : isSystem ? "No system rules are installed" : "No rules of your own yet"}
          text={isSystem ? "System rules appear here once the Data Quality service has started." : "Create one with New rule."} />
      )}
      {shown.length > 0 && (
        <>
          <div style={tableHead(COLS)}><div>Name</div><div>Type</div><div>Dimension</div><div>Severity</div><div>Tags</div><div /></div>
          {shown.map((r) => {
            const ds = dimStyle(r.dimension); const ss = sevStyle(r.severity);
            return (
              <Fragment key={r.id}>
                <div onClick={() => setOpen(open === r.id ? null : r.id)}
                  style={{ ...tableRow(COLS), cursor: "pointer", background: open === r.id ? "#fffaf5" : "transparent" }}>
                  <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12 }}>
                    <i className={`ti ti-chevron-${open === r.id ? "down" : "right"}`} style={{ fontSize: 12, color: "#73827a", marginRight: 4 }} />
                    <b>{r.name}</b>{r.display_name && r.display_name !== r.name && <span style={{ color: "#73827a" }}> · {r.display_name}</span>}
                  </div>
                  <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12 }}>{r.rule_type}</div>
                  <div><span style={chip(ds.fg, ds.bg)}>{r.dimension}</span></div>
                  <div><span style={chip(ss.fg, ss.bg)}>{r.severity}</span></div>
                  <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>{r.tags?.map((t) => <span key={t} style={chip("#4b5a53", "#eef2ed")}>{t}</span>)}</div>
                  <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                    {editable && <>
                      <IconAction size="sm" icon={ACTION_ICON.edit} label="Edit rule" onClick={(e) => { e.stopPropagation(); setEditing(r); }} />
                      <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete rule" danger onClick={(e) => { e.stopPropagation(); remove(r); }} />
                    </>}
                  </div>
                </div>
                {open === r.id && (
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, padding: "10px 16px 14px 34px", borderBottom: "0.5px solid #f0f0ed", background: "#fffdfa" }}>
                    <div>
                      <div style={{ fontSize: 11, color: "#73827a", marginBottom: 4 }}>Description</div>
                      <div style={{ fontSize: 12.5, color: "#2E3D36", lineHeight: 1.5 }}>{r.description || <i style={{ color: "#73827a" }}>none</i>}</div>
                    </div>
                    <div>
                      <div style={{ fontSize: 11, color: "#73827a", marginBottom: 4 }}>Parameters</div>
                      <pre style={codeBlock}>{JSON.stringify(r.parameters ?? {}, null, 2)}</pre>
                    </div>
                  </div>
                )}
              </Fragment>
            );
          })}
        </>
      )}

      {creating && canEdit && <RuleDrawer onClose={onCloseCreate} onCreated={() => onScope("org")} />}
      {editing && <RuleDrawer rule={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

/** New rule, or edit one of yours (only severity, parameters, display name, description and tags). */
function RuleDrawer({ rule, onClose, onCreated }: { rule?: DqRule; onClose: () => void; onCreated?: () => void }) {
  const dispatch = useDispatch<AppDispatch>();
  const [createRule, { isLoading: creating }] = useCreateDqRuleMutation();
  const [updateRule, { isLoading: updating }] = useUpdateDqRuleMutation();
  const [f, setF] = useState(rule ? {
    ...EMPTY, name: rule.name, display_name: rule.display_name ?? "", description: rule.description ?? "",
    rule_type: rule.rule_type, dimension: rule.dimension, severity: rule.severity,
    parameters: JSON.stringify(rule.parameters ?? {}, null, 2), tags: (rule.tags ?? []).join(", "),
  } : EMPTY);
  const busy = creating || updating;
  const params = parseJsonObject(f.parameters);
  const ok = !busy && !!params && (!!rule || /^[a-z][a-z0-9_]*$/.test(f.name.trim()));
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });

  const save = async () => {
    const common = {
      display_name: f.display_name.trim() || f.name.trim(), description: f.description.trim() || null,
      severity: f.severity, parameters: params, tags: tagList(f.tags),
    };
    try {
      if (rule) {
        await updateRule({ id: rule.id, body: common }).unwrap();
        dispatch(notify({ kind: "success", message: `Updated "${rule.name}". Re-render any binding that uses it.` }));
      } else {
        const r = await createRule({ ...common, name: f.name.trim(), rule_type: f.rule_type, dimension: f.dimension }).unwrap();
        dispatch(notify({ kind: "success", message: `Created rule "${r.name}"` }));
        onCreated?.();
      }
      onClose();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, rule ? "Could not save the rule" : "Could not create the rule", { prefer: "detail" }) }));
    }
  };

  return (
    <Drawer title={rule ? `Edit ${rule.name}` : "New rule"} icon="ti-list-details" onClose={onClose} busy={busy} width={460}
      footer={<>
        <button onClick={save} disabled={!ok} style={{ ...drawerBtn(true), flex: 1, opacity: ok ? 1 : 0.55 }}>
          {busy ? "Saving…" : rule ? "Save changes" : "Create rule"}</button>
        <button onClick={onClose} style={drawerBtn(false)}>Cancel</button>
      </>}>
      <div style={{ fontSize: 12, color: "#5b6d62", lineHeight: 1.55, marginBottom: 14 }}>
        {rule ? "Changes apply to every ruleset that includes this rule. Name, type and dimension can't change; create a new rule instead."
          : "A reusable definition. Any ruleset can include it and override its parameters per column."}
      </div>
      <Field label="Name" required={!rule} hint={rule ? undefined : "Unique, lowercase snake_case."}>
        <input value={f.name} disabled={!!rule} placeholder="amount_positive" style={{ ...inp, fontFamily: "monospace" }}
          onChange={(e) => set({ name: e.target.value })} />
      </Field>
      <Field label="Display name">
        <input value={f.display_name} placeholder="Amount positive" style={inp} onChange={(e) => set({ display_name: e.target.value })} />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <Field label="Rule type">
          {rule ? <input value={f.rule_type} disabled style={inp} />
            : <SearchSelect value={f.rule_type} ariaLabel="Rule type" noun="rule type" onChange={(v) => set({ rule_type: v as DqRuleType })}
                options={RULE_TYPES.map((t) => ({ value: t, label: t }))} />}
        </Field>
        <Field label="Dimension">
          {rule ? <input value={f.dimension} disabled style={inp} />
            : <SearchSelect value={f.dimension} ariaLabel="Dimension" noun="dimension" onChange={(v) => set({ dimension: v as DqDimension })}
                options={DIMENSIONS.map((d) => ({ value: d, label: d }))} />}
        </Field>
      </div>
      <Field label="Severity">
        <SearchSelect value={f.severity} ariaLabel="Severity" noun="severity" onChange={(v) => set({ severity: v as DqSeverity })}
          options={SEVERITIES.map((s) => ({ value: s, label: s }))} />
      </Field>
      <Field label="Parameters (JSON)" hint={params ? "Defaults; a ruleset can override them per column." : "Not a valid JSON object."}>
        <textarea rows={4} value={f.parameters} placeholder='{"min": 0}'
          style={{ ...inp, height: "auto", padding: 8, fontFamily: "monospace", borderColor: params ? "#cfd8d1" : "#b23b3b" }}
          onChange={(e) => set({ parameters: e.target.value })} />
      </Field>
      <Field label="Description">
        <textarea rows={2} value={f.description} placeholder="Value must be greater than zero."
          style={{ ...inp, height: "auto", padding: 8 }} onChange={(e) => set({ description: e.target.value })} />
      </Field>
      <Field label="Tags" hint="Comma-separated.">
        <input value={f.tags} placeholder="financial, orders" style={inp} onChange={(e) => set({ tags: e.target.value })} />
      </Field>
    </Drawer>
  );
}
