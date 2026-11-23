import { useEffect, useMemo, useState } from "react";
import { useGetDbtLineageQuery, useListDbtProjectsQuery } from "../../dbt";
import { flinkSinks, useListFlinkJobsQuery } from "../../flink";
import { leafName } from "../dqUtils";
import type { DqEnforcementMode, DqJobType, UUID } from "../../../types";

export const EMPTY_BINDING_FORM = {
  ruleset_id: "" as UUID | "",
  job_type: "dbt_project" as DqJobType,
  job_id: "" as UUID | "",
  target_table: "",
  ucs_fqn: "",
  enforcement_mode: "warn" as DqEnforcementMode,
};
export type BindingForm = typeof EMPTY_BINDING_FORM;

/**
 * State for the "New quality binding" drawer. The job pickers are backed by
 * the real project/job lists so a binding points at a job that exists, and
 * "Target table" offers the tables that job actually writes:
 *
 *  dbt   — the lineage graph. Only the *target* side of an edge is a model the
 *          project builds; a source that never appears as a target is an
 *          upstream table the project reads but doesn't produce.
 *  Flink — the job's own SQL (there is no target_table column on a FlinkJob),
 *          so the sink is whatever it INSERTs INTO.
 */
export type BindingFormState = ReturnType<typeof useBindingForm>;

export function useBindingForm() {
  const [form, setForm] = useState(EMPTY_BINDING_FORM);
  // True once the user picks "Other…", so the field stays a text box while they
  // type instead of snapping back to the select on every keystroke.
  const [customTable, setCustomTable] = useState(false);

  const { data: dbtProjects } = useListDbtProjectsQuery({});
  const { data: flinkJobs } = useListFlinkJobsQuery();

  // Job pickers are per-type; switching type invalidates whatever was chosen.
  const jobOptions = useMemo(() => {
    if (form.job_type === "dbt_project") return (dbtProjects ?? []).map((p) => ({ id: p.id, label: p.name }));
    if (form.job_type === "flink_job") return (flinkJobs ?? []).map((j) => ({ id: j.id, label: j.name }));
    return []; // Python jobs aren't registered in Workspace — the UUID is typed.
  }, [form.job_type, dbtProjects, flinkJobs]);

  // Lineage is the only place dbt exposes what a project builds. Fetched per
  // selected project, and only for dbt bindings.
  const { data: lineage, isFetching: loadingLineage } = useGetDbtLineageQuery(
    form.job_id,
    { skip: form.job_type !== "dbt_project" || !form.job_id },
  );

  const tableOptions = useMemo<string[]>(() => {
    if (!form.job_id) return [];
    if (form.job_type === "dbt_project") {
      const built = new Set((lineage?.edges ?? []).map((e) => leafName(e.target_table)));
      return [...built].sort();
    }
    if (form.job_type === "flink_job") {
      const job = (flinkJobs ?? []).find((j) => j.id === form.job_id);
      return job ? flinkSinks(job.sql || "").sort() : [];
    }
    return [];
  }, [form.job_id, form.job_type, lineage, flinkJobs]);

  // A table from the previous job is meaningless against the new one.
  useEffect(() => {
    setForm((f) => ({ ...f, target_table: "" }));
    setCustomTable(false);
  }, [form.job_id, form.job_type]);

  // Sole option is the answer — preselect rather than making them open a
  // one-item dropdown.
  useEffect(() => {
    if (tableOptions.length === 1 && !form.target_table && !customTable) {
      setForm((f) => ({ ...f, target_table: tableOptions[0] }));
    }
  }, [tableOptions, form.target_table, customTable]);

  const update = (patch: Partial<BindingForm>) => setForm((f) => ({ ...f, ...patch }));
  const reset = () => { setForm(EMPTY_BINDING_FORM); setCustomTable(false); };
  const valid = Boolean(form.ruleset_id && form.job_id && form.target_table.trim() && form.ucs_fqn.trim());

  return { form, update, reset, valid, customTable, setCustomTable, jobOptions, tableOptions, loadingLineage };
}
