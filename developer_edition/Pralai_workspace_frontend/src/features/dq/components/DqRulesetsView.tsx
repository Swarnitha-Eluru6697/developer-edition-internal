/**
 * Rulesets tab: the rulesets a binding attaches to a job. A ruleset pins rules
 * from the library to columns of one table; it is engine-agnostic, and the DQ
 * service renders it into dbt tests, a FlinkSQL quality job or a GX / Soda suite
 * depending on what it is bound to.
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import {
  useAddDqRulesetItemMutation,
  useCreateDqRulesetMutation,
  useDeleteDqRulesetItemMutation,
  useDeleteDqRulesetMutation,
  useListDqRulesQuery,
  useListDqRulesetItemsQuery,
  useListDqRulesetsQuery,
  useUpdateDqRulesetItemMutation,
} from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import { askConfirm } from "../../../shared/ui/dialogStore";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import SearchSelect from "../../../shared/ui/SearchSelect";
import type { AppDispatch } from "../../../store";
import type { DqRulesetSummary } from "../../../types";
import { cellEllipsis, chip, dimStyle, parseJsonObject, sevStyle, tableHead, tableRow } from "../dqUi";

const ITEM_COLS = "1.3fr 1fr 1fr 0.9fr 0.8fr 1fr 70px 40px";
const fail = (e: unknown, what: string) => notify({ kind: "error", message: apiErrorMessage(e, what, { prefer: "detail" }) });

export function DqRulesetsView({ canEdit, creating, onCloseCreate }: {
  canEdit: boolean; creating: boolean; onCloseCreate: () => void;
}) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: rulesets, isLoading, error } = useListDqRulesetsQuery();
  const [createRuleset, { isLoading: saving }] = useCreateDqRulesetMutation();
  const [deleteRuleset] = useDeleteDqRulesetMutation();
  const [form, setForm] = useState({ name: "", description: "" });
  const [open, setOpen] = useState<string | null>(null);

  const close = () => { setForm({ name: "", description: "" }); onCloseCreate(); };
  const create = async () => {
    try {
      const rs = await createRuleset({ name: form.name.trim(), description: form.description.trim() || null }).unwrap();
      dispatch(notify({ kind: "success", message: `Created ruleset "${rs.name}"` }));
      setOpen(rs.id);
      close();
    } catch (e) { dispatch(fail(e, "Could not create the ruleset")); }
  };
  const remove = async (rs: DqRulesetSummary) => {
    if (!(await askConfirm({ title: "Delete ruleset", message: `Delete "${rs.name}"? Any job bound to it stops being checked.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await deleteRuleset(rs.id).unwrap();
      if (open === rs.id) setOpen(null);
      dispatch(notify({ kind: "success", message: `Deleted "${rs.name}"` }));
    } catch (e) { dispatch(fail(e, "Could not delete the ruleset")); }
  };

  return (
    <div>
      {isLoading && <div style={{ padding: 24, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading rulesets…</div>}
      {error && <ErrorBox error={error} what="Could not load the rulesets." />}
      {!isLoading && !error && !rulesets?.length && (
        <Empty title="No rulesets yet"
          text="Create a ruleset, add rules from the Rule library, then bind it to a job under Job bindings." />
      )}
      {rulesets?.map((rs) => (
        <div key={rs.id} style={{ borderBottom: "0.5px solid #E3E8E2" }}>
          <div onClick={() => setOpen(open === rs.id ? null : rs.id)}
            style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", cursor: "pointer", background: open === rs.id ? "#fffaf5" : "transparent" }}>
            <i className={`ti ti-chevron-${open === rs.id ? "down" : "right"}`} style={{ fontSize: 14, color: "#73827a" }} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontSize: 13, fontWeight: 600, ...cellEllipsis }}>{rs.name}</div>
              {rs.description && <div style={{ fontSize: 11.5, color: "#5b6d62", ...cellEllipsis }}>{rs.description}</div>}
            </div>
            <span style={chip("#4b5a53", "#eef2ed")}>{rs.item_count} rule{rs.item_count === 1 ? "" : "s"}</span>
            {canEdit && <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete ruleset" danger
              onClick={(e) => { e.stopPropagation(); remove(rs); }} />}
          </div>
          {open === rs.id && <RulesetItems ruleset={rs} canEdit={canEdit} />}
        </div>
      ))}

      {creating && canEdit && (
        <Drawer title="New ruleset" icon="ti-list-check" onClose={close} busy={saving}
          footer={<>
            <button onClick={create} disabled={saving || !form.name.trim()}
              style={{ ...drawerBtn(true), flex: 1, opacity: saving || !form.name.trim() ? 0.55 : 1 }}>
              {saving ? "Creating…" : "Create ruleset"}</button>
            <button onClick={close} style={drawerBtn(false)}>Cancel</button>
          </>}>
          <div style={{ fontSize: 12, color: "#5b6d62", lineHeight: 1.55, marginBottom: 14 }}>
            Name it after the table it will check. Rules are added next, from the library.
          </div>
          <Field label="Name" required hint="Unique within your organisation.">
            <input autoFocus value={form.name} placeholder="orders_core_checks" style={inp}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Description">
            <textarea rows={3} value={form.description} placeholder="Null, range and freshness checks on the orders table."
              style={{ ...inp, height: "auto", padding: 8 }} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </Field>
        </Drawer>
      )}
    </div>
  );
}

/** One ruleset's rules; fetched only when the ruleset is opened. */
function RulesetItems({ ruleset, canEdit }: { ruleset: DqRulesetSummary; canEdit: boolean }) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: items, isLoading } = useListDqRulesetItemsQuery(ruleset.id);
  const [adding, setAdding] = useState(false);
  const [updateItem] = useUpdateDqRulesetItemMutation();
  const [deleteItem] = useDeleteDqRulesetItemMutation();

  const toggle = async (id: string, enabled: boolean) => {
    try { await updateItem({ rulesetId: ruleset.id, itemId: id, body: { enabled: !enabled } }).unwrap(); }
    catch (e) { dispatch(fail(e, "Could not change the rule")); }
  };
  const remove = async (id: string) => {
    try {
      await deleteItem({ rulesetId: ruleset.id, itemId: id }).unwrap();
      dispatch(notify({ kind: "success", message: "Rule removed from the ruleset" }));
    } catch (e) { dispatch(fail(e, "Could not remove the rule")); }
  };

  return (
    <div style={{ background: "#fff", paddingBottom: 10 }}>
      {isLoading && <div style={{ padding: "8px 16px 8px 40px", fontSize: 12, color: "#73827a" }}>Loading rules…</div>}
      {items && items.length > 0 && (
        <>
          <div style={tableHead(ITEM_COLS)}>
            <div>Rule</div><div>Type</div><div>Column</div><div>Dimension</div><div>Severity</div><div>Overrides</div><div>Enabled</div><div />
          </div>
          {items.map((it) => {
            const ds = dimStyle(it.rule?.dimension); const ss = sevStyle(it.rule?.severity);
            const ov = Object.keys(it.parameter_overrides ?? {}).length ? JSON.stringify(it.parameter_overrides) : "—";
            return (
              <div key={it.id} style={{ ...tableRow(ITEM_COLS), opacity: it.enabled ? 1 : 0.55 }}>
                <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12, fontWeight: 600 }}>{it.rule?.name ?? it.rule_id}</div>
                <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12 }}>{it.rule?.rule_type ?? "—"}</div>
                <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12 }}>{it.column_name || <span style={{ color: "#73827a" }}>table-level</span>}</div>
                <div><span style={chip(ds.fg, ds.bg)}>{it.rule?.dimension ?? "—"}</span></div>
                <div><span style={chip(ss.fg, ss.bg)}>{it.rule?.severity ?? "—"}</span></div>
                <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 11.5, color: "#5b6d62" }} title={ov}>{ov}</div>
                <div>
                  <input type="checkbox" checked={it.enabled} disabled={!canEdit} aria-label="Enabled in this ruleset"
                    title="Enable or disable in this ruleset" onChange={() => toggle(it.id, it.enabled)} style={{ accentColor: "#0A7D5A" }} />
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  {canEdit && <IconAction size="sm" icon={ACTION_ICON.delete} label="Remove from ruleset" danger onClick={() => remove(it.id)} />}
                </div>
              </div>
            );
          })}
        </>
      )}
      {items && !items.length && (
        <div style={{ padding: "8px 16px 8px 40px", fontSize: 12.5, color: "#5b6d62" }}>This ruleset is empty. Add a rule from the library.</div>
      )}
      {canEdit && (
        <button onClick={() => setAdding(true)}
          style={{ margin: "8px 0 0 40px", background: "none", border: "0.5px solid #cfd8d1", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", color: "#2E3D36", display: "inline-flex", gap: 5, alignItems: "center" }}>
          <i className="ti ti-plus" style={{ fontSize: 13 }} />Add rule
        </button>
      )}
      {adding && <AddRuleDrawer ruleset={ruleset} onClose={() => setAdding(false)} />}
    </div>
  );
}

function AddRuleDrawer({ ruleset, onClose }: { ruleset: DqRulesetSummary; onClose: () => void }) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: rules } = useListDqRulesQuery();
  const [addItem, { isLoading }] = useAddDqRulesetItemMutation();
  const [pick, setPick] = useState({ rule_id: "", column_name: "", overrides: "{}" });
  const overrides = parseJsonObject(pick.overrides);
  const ok = !!pick.rule_id && !!overrides && !isLoading;

  const add = async () => {
    try {
      await addItem({ rulesetId: ruleset.id, body: { rule_id: pick.rule_id, column_name: pick.column_name.trim(), parameter_overrides: overrides } }).unwrap();
      dispatch(notify({ kind: "success", message: `Rule added to ${ruleset.name}` }));
      onClose();
    } catch (e) { dispatch(fail(e, "Could not add the rule")); }
  };

  return (
    <Drawer title={`Add a rule to ${ruleset.name}`} icon="ti-list-check" onClose={onClose} busy={isLoading}
      footer={<>
        <button onClick={add} disabled={!ok} style={{ ...drawerBtn(true), flex: 1, opacity: ok ? 1 : 0.55 }}>
          {isLoading ? "Adding…" : "Add rule"}</button>
        <button onClick={onClose} style={drawerBtn(false)}>Cancel</button>
      </>}>
      <Field label="Rule" required>
        <SearchSelect value={pick.rule_id} ariaLabel="Rule" noun="rule" placeholder="Choose a rule"
          onChange={(v) => setPick({ ...pick, rule_id: v })}
          options={(rules ?? []).map((r) => ({ value: r.id, label: r.name, hint: `${r.rule_type} · ${r.dimension}` }))} />
      </Field>
      <Field label="Column" hint="Leave empty for a table-level rule such as row_count or freshness.">
        <input value={pick.column_name} placeholder="amount" style={{ ...inp, fontFamily: "monospace" }}
          onChange={(e) => setPick({ ...pick, column_name: e.target.value })} />
      </Field>
      <Field label="Parameter overrides (JSON)"
        hint={overrides ? "Merged over the rule's own parameters, for this ruleset only." : "Not a valid JSON object."}>
        <textarea rows={3} value={pick.overrides} placeholder='{"min": 0}'
          style={{ ...inp, height: "auto", padding: 8, fontFamily: "monospace", borderColor: overrides ? "#cfd8d1" : "#b23b3b" }}
          onChange={(e) => setPick({ ...pick, overrides: e.target.value })} />
      </Field>
    </Drawer>
  );
}

export function Empty({ title, text, icon = "ti-checkup-list" }: { title: string; text: string; icon?: string }) {
  return (
    <div style={{ padding: 32, textAlign: "center" }}>
      <i className={`ti ${icon}`} style={{ fontSize: 32, color: "#cfd8d1", display: "block", marginBottom: 10 }} />
      <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 5 }}>{title}</div>
      <div style={{ fontSize: 12.5, color: "#5b6d62" }}>{text}</div>
    </div>
  );
}

export function ErrorBox({ error, what }: { error: unknown; what: string }) {
  return (
    <div style={{ margin: 16, padding: "10px 12px", background: "#fbe9e9", border: "0.5px solid #fecaca", borderRadius: 6, fontSize: 12.5, color: "#b23b3b", lineHeight: 1.5 }}>
      {apiErrorMessage(error, what, { prefer: "detail" })}
    </div>
  );
}
