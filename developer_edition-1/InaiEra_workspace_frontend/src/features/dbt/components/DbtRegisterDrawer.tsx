/**
 * Register-from-Git drawer — same slide-over as the FlinkSQL job form. Closes
 * on success, Escape, ×, or backdrop; the form state lives here, so closing
 * (unmounting) always drops whatever was typed and reopening starts clean.
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { useRegisterDbtProjectMutation } from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import type { AppDispatch } from "../../../store";
import type { DbtEngine, DbtProject, UUID } from "../../../types";
import { JOB_KINDS, type JobKind } from "../jobKinds";
import { DbtJobLinks } from "./DbtJobLinks";
import SearchSelect from "../../../shared/ui/SearchSelect";

// Trino (the default) and ClickHouse are offered for new dbt, SQL and Python jobs; more are added here later. (Jobs registered earlier on
// pg_lake keep running on it: the Runner still supports it, it is just not offered any more.)
const ENGINE_OPTIONS = [{ value: "trino", label: "Trino" }, { value: "clickhouse", label: "ClickHouse" }];

const EMPTY_REG_FORM = {
  git_url: "", project_name: "", git_ref: "main", engine: "trino" as DbtEngine, schedule_cron: "",
  upstream_project_ids: [] as UUID[],
  dependent_project_ids: [] as UUID[],
};


interface Props {
  /** The jobs already registered: the ones that can be chosen as upstream or downstream jobs. */
  projects: DbtProject[];
  onClose: () => void;
  onRegistered: () => void;
  /** "dbt" registers a project folder; "sql" registers one .sql file as a SQL job. */
  kind?: JobKind;
}

export function DbtRegisterDrawer({ projects, onClose, onRegistered, kind = "dbt" }: Props) {
  const sql = kind === "sql";
  const python = kind === "python";
  const dispatch = useDispatch<AppDispatch>();
  const [regForm, setRegForm] = useState(EMPTY_REG_FORM);
  const [registerProject, { isLoading: registering }] = useRegisterDbtProjectMutation();
  const blocked = registering || !regForm.git_url.trim() || !regForm.project_name.trim();

  const handleRegister = async () => {
    if (blocked) return;
    try {
      const project = await registerProject({
        git_url: regForm.git_url.trim(),
        // The dbt Runner builds "<repo>#subdirectory=<project name>" from these two.
        project_path: regForm.project_name.trim(),
        kind,
        git_ref: regForm.git_ref.trim() || "main",
        engine: regForm.engine,
        schedule_cron: regForm.schedule_cron || undefined,
        upstream_project_ids: regForm.upstream_project_ids,
        dependent_project_ids: regForm.dependent_project_ids,
      }).unwrap();
      dispatch(notify({ kind: "success", message: `Registered project "${project?.name ?? regForm.git_url}"` }));
      onClose(); // success → dismiss; on failure it stays open with the input intact
      onRegistered();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Registration failed", { prefer: "detail" }) }));
    }
  };

  return (
    <Drawer
      title={JOB_KINDS[kind].registerTitle}
      icon="ti-git-fork"
      onClose={onClose}
      busy={registering}
      hideScrollbar
      footer={<>
        <button onClick={handleRegister} disabled={blocked}
          style={{ ...drawerBtn(true), flex: 1, opacity: blocked ? 0.55 : 1, cursor: blocked ? "not-allowed" : "pointer" }}>
          {registering ? "Registering…" : JOB_KINDS[kind].registerLabel}
        </button>
        <button onClick={onClose} style={drawerBtn(false)}>Cancel</button>
      </>}
    >
      <div style={{ fontSize: 12, color: "#6b7280", lineHeight: 1.55, marginBottom: 14 }}>
        {sql
          ? "The Runner clones this repository and runs the SQL file's statements in order against the engine. Push the file to Git first, then register it here."
          : python
          ? "The Runner clones this repository and runs the Python script (.py) with Python 3.11. Put a requirements.txt next to the script and its packages are installed for each run. Push the files to Git first, then register it here."
          : "The dbt Runner clones this repository and builds the project from it. Push your models to Git first, then register the repo here."}
      </div>

      <Field label="Git repository URL" required>
        <input autoFocus placeholder="https://github.com/acme/dbt-project.git" value={regForm.git_url}
          onChange={(e) => setRegForm((f) => ({ ...f, git_url: e.target.value }))}
          style={{ ...inp, fontFamily: "monospace" }} />
      </Field>

      <Field label={sql ? "SQL file" : python ? "Python script" : "Project name"} required
        hint={sql
          ? "Path of the .sql file from the repo root, e.g. jobs/daily_totals.sql."
          : python
          ? "Path of the .py file from the repo root, e.g. jobs/pipeline.py. The script reads Trino from the environment (INAIERA_TRINO_HOST / _PORT / _CATALOG) — never put credentials in code."
          : "The folder in the repo that holds this dbt project (the one with dbt_project.yml), e.g. customers. Use . if the project is at the top of the repo."}>
        <input placeholder={sql ? "jobs/daily_totals.sql" : python ? "jobs/pipeline.py" : "customers"} value={regForm.project_name}
          onChange={(e) => setRegForm((f) => ({ ...f, project_name: e.target.value }))}
          style={{ ...inp, fontFamily: "monospace" }} />
      </Field>

      <Field label="Branch, tag or commit">
        <input placeholder="main" value={regForm.git_ref}
          onChange={(e) => setRegForm((f) => ({ ...f, git_ref: e.target.value }))}
          style={{ ...inp, fontFamily: "monospace" }} />
      </Field>

      <Field label="Engine">
        <SearchSelect value={regForm.engine} ariaLabel="Engine" noun="engine"
          onChange={(v) => setRegForm((f) => ({ ...f, engine: v as DbtEngine }))}
          options={ENGINE_OPTIONS} />
      </Field>

      <Field label="Cron schedule" hint="Optional — leave empty to run manually. e.g. 0 */6 * * * (every 6h) · 0 1 * * * (daily 1am)">
        <input placeholder="0 */6 * * *" value={regForm.schedule_cron}
          onChange={(e) => setRegForm((f) => ({ ...f, schedule_cron: e.target.value }))}
          style={{ ...inp, fontFamily: "monospace" }} />
      </Field>

      <DbtJobLinks selfId={null} kind={kind} projects={projects}
        value={{ upstream: regForm.upstream_project_ids, downstream: regForm.dependent_project_ids }}
        onChange={({ upstream, downstream }) => setRegForm((f) => ({ ...f, upstream_project_ids: upstream, dependent_project_ids: downstream }))} />
    </Drawer>
  );
}
