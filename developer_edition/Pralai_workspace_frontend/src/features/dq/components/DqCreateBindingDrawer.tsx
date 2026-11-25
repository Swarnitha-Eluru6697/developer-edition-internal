/** "New quality binding" drawer: attach a ruleset to one table produced by one job. */
import { useDispatch } from "react-redux";
import { useCreateDqBindingMutation, useListDqRulesetsQuery } from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import type { AppDispatch } from "../../../store";
import type { DqEnforcementMode, DqJobType } from "../../../types";
import { JOB_TYPES, MODES } from "../dqUi";
import { OTHER_TABLE } from "../dqUtils";
import type { BindingFormState } from "../hooks/useBindingForm";
import SearchSelect from "../../../shared/ui/SearchSelect";

export function DqCreateBindingDrawer({ binding, onClose }: { binding: BindingFormState; onClose: () => void }) {
  const dispatch = useDispatch<AppDispatch>();
  const { form, update, valid: formValid, customTable, setCustomTable, jobOptions, tableOptions, loadingLineage } = binding;
  const { data: rulesets } = useListDqRulesetsQuery();
  const [createBinding, { isLoading: creating }] = useCreateDqBindingMutation();

  const handleCreate = async () => {
    if (!formValid || !form.ruleset_id || !form.job_id) return;
    try {
      await createBinding({
        ruleset_id: form.ruleset_id,
        job_type: form.job_type,
        job_id: form.job_id,
        target_table: form.target_table.trim(),
        ucs_fqn: form.ucs_fqn.trim(),
        enforcement_mode: form.enforcement_mode,
      }).unwrap();
      dispatch(notify({ kind: "success", message: `Bound ruleset to ${form.target_table.trim()}` }));
      onClose();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not create the binding", { prefer: "detail" }) }));
    }
  };

  return (
    <Drawer
      title="New quality binding"
      icon="ti-checkup-list"
      onClose={onClose}
      busy={creating}
      footer={<>
        <button onClick={handleCreate} disabled={creating || !formValid}
          style={{ ...drawerBtn(true), flex: 1, opacity: creating || !formValid ? 0.55 : 1, cursor: creating || !formValid ? "not-allowed" : "pointer" }}>
          {creating ? "Creating…" : "Create binding"}
        </button>
        <button onClick={onClose} style={drawerBtn(false)}>Cancel</button>
      </>}
    >
      <div style={{ fontSize: 12, color: "#5b6d62", lineHeight: 1.55, marginBottom: 14 }}>
        A binding attaches a ruleset to one table produced by one job. Rulesets are
        written under Data Quality → Rulesets.
      </div>

      <Field label="Ruleset" required>
        <SearchSelect value={form.ruleset_id} ariaLabel="Ruleset" noun="ruleset" placeholder="Choose a ruleset"
          onChange={(v) => update({ ruleset_id: v })}
          options={(rulesets ?? []).map((rs) => ({
            value: rs.id, label: rs.name, hint: rs.item_count ? `${rs.item_count} rules` : undefined,
          }))} />
        {rulesets && rulesets.length === 0 && (
          <div style={{ fontSize: 11, color: "#a8470a", marginTop: 4, lineHeight: 1.45 }}>
            No rulesets yet. Create one under the Rulesets tab first.
          </div>
        )}
      </Field>

      <Field label="Job type" required>
        <SearchSelect value={form.job_type} ariaLabel="Job type" noun="job type"
          onChange={(v) => update({ job_type: v as DqJobType, job_id: "" })}
          options={JOB_TYPES} />
      </Field>

      <Field
        label="Job"
        required
        hint={form.job_type === "python_job"
          ? "Python jobs aren't registered in Workspace — paste the job UUID."
          : undefined}
      >
        {form.job_type === "python_job" ? (
          <input placeholder="550e8400-e29b-41d4-a716-446655440000" value={form.job_id}
            onChange={(e) => update({ job_id: e.target.value })}
            style={{ ...inp, fontFamily: "monospace" }} />
        ) : (
          <SearchSelect value={form.job_id} ariaLabel="Job" noun="job" placeholder="Choose a job"
            onChange={(v) => update({ job_id: v })}
            options={jobOptions.map((o) => ({ value: o.id, label: o.label }))} />
        )}
      </Field>

      <Field
        label="Target table"
        required
        hint={
          !form.job_id
            ? "Pick a job first — the list comes from what that job writes."
            : form.job_type === "python_job"
              ? "Python jobs aren't registered in Workspace, so type the table name."
              : loadingLineage
                ? "Reading the project's models…"
                : customTable
                  ? "Typing a name not produced by this job means nothing will run these rules."
                  : tableOptions.length
                    ? form.job_type === "dbt_project"
                      ? "Models this project builds. Upstream sources are excluded — the job doesn't write them."
                      : "Tables this job writes, read from its INSERT INTO statements."
                    : "No tables found for this job — run it once, or type the name."
        }
      >
        {/* Free text only where there is nothing to choose from: python jobs,
            or a job whose tables couldn't be discovered. */}
        {form.job_type === "python_job" || customTable || (form.job_id && !loadingLineage && !tableOptions.length) ? (
          <input
            autoFocus={customTable}
            placeholder="stg_orders"
            value={form.target_table}
            onChange={(e) => update({ target_table: e.target.value })}
            style={{ ...inp, fontFamily: "monospace" }}
          />
        ) : (
          <SearchSelect
            value={form.target_table}
            ariaLabel="Target table"
            noun="table"
            disabled={!form.job_id || loadingLineage}
            placeholder={!form.job_id ? "Choose a job first" : loadingLineage ? "Loading…" : "Choose a table"}
            onChange={(v) => {
              if (v === OTHER_TABLE) {
                setCustomTable(true);
                update({ target_table: "" });
              } else {
                update({ target_table: v });
              }
            }}
            options={[
              ...tableOptions.map((t) => ({ value: t, label: t })),
              ...(tableOptions.length ? [{ value: OTHER_TABLE, label: "Other… (type a name)" }] : []),
            ]}
          />
        )}
      </Field>

      <Field label="Dataset name" required hint="The name results are reported under, e.g. database.schema.table.">
        <input placeholder="pg_sales.public.orders" value={form.ucs_fqn}
          onChange={(e) => update({ ucs_fqn: e.target.value })}
          style={{ ...inp, fontFamily: "monospace" }} />
      </Field>

      <Field
        label="Enforcement"
        hint={MODES.find((m) => m.value === form.enforcement_mode)?.hint}
      >
        <SearchSelect value={form.enforcement_mode} ariaLabel="Enforcement" noun="mode"
          onChange={(v) => update({ enforcement_mode: v as DqEnforcementMode })}
          options={MODES.map((m) => ({ value: m.value, label: m.label }))} />
      </Field>
    </Drawer>
  );
}
