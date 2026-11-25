/**
 * FlinkPanel — the FlinkSQL surface inside Workspace.
 *
 * Lists FlinkSQL jobs and lets the user register one from a Git repository —
 * the same contract as the dbt Transformer's "Register project": you point at a
 * repo + ref, the workspace service clones it, assembles the FlinkSQL, and
 * submits it to the runner. There is deliberately no SQL editor here; author
 * FlinkSQL in the workspace (Home → FlinkSQL editor), push it to Git, then
 * register the repo.
 *
 * All calls go through the workspace proxy at /api/v1/flink/*.
 */
import { useEffect, useState } from "react";
import { isStringOrNull, usePersistedState } from "../../../shared/usePersistedState";
import { useDispatch, useSelector } from "react-redux";
import { PageHeader } from "../../../shared/ui/PageHeader";
import { pageBtn } from "../../../shared/ui/pageStyles";
import {
  useListFlinkJobsQuery,
  useCreateFlinkJobFromGitMutation,
  useSyncFlinkJobMutation,
  useDeleteFlinkJobMutation,
  useValidateFlinkJobMutation,
  useDeployFlinkJobMutation,
  useStopFlinkJobMutation,
  useGetFlinkUiUrlQuery,
  useLazyGetFlinkJobStatusQuery,
} from "../api";
import { clearOpenJob, notify } from "../../../store/uiSlice";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import { apiErrorMessage, asApiError } from "../../../api/errors";
import { provenanceOf, userDescription } from "../provenance";
import { flinkUiJobUrl } from "../flinkSql";
import { useHasPermission } from "../../../rbac/hooks";
import type { AppDispatch, RootState } from "../../../store";
import type { FlinkJob, UUID } from "../../../types";
import { askConfirm } from "../../../shared/ui/dialogStore";
import { Glyph } from "../../../shared/ui/Glyph";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

// There is no engine choice for a Flink job: every job runs on Flink (the SQL Gateway). The runner still stores a "sink engine" label per job
// (its default is "trino", which also switches on the Iceberg/Polaris settings), so every registration sends the same fixed value, exactly
// what the form's default used to be. Remove this once the backend drops the field.
const REGISTERED_SINK_ENGINE = "pg_lake";

const STATUS_COLOR: Record<string, string> = {
  draft: "#5b6d62", validating: "#d97706", validated: "#0A7D5A",
  deploying: "#d97706", running: "#16a34a", stopped: "#5b6d62",
  failed: "#b23b3b", cancelled: "#5b6d62",
};

const EMPTY_FORM = {
  git_url: "", git_ref: "", sql_path: "", name: "",
  parallelism: 1,
};

export function FlinkPanel() {
  const dispatch = useDispatch<AppDispatch>();
  const { data: jobs, isLoading, isError, refetch } = useListFlinkJobsQuery(undefined, { pollingInterval: 4000 });
  const [createFromGit, { isLoading: creating }] = useCreateFlinkJobFromGitMutation();
  const [syncJob, { isLoading: syncing }] = useSyncFlinkJobMutation();
  const [deleteJob] = useDeleteFlinkJobMutation();
  const [validateJob] = useValidateFlinkJobMutation();
  const [deployJob] = useDeployFlinkJobMutation();
  const [stopJob] = useStopFlinkJobMutation();
  const { data: flinkUi } = useGetFlinkUiUrlQuery();
  const [fetchStatus] = useLazyGetFlinkJobStatusQuery();
  // Deploy/stop need workspace:flink:deploy; registering, validating and deleting a job need workspace:flink:edit.
  const canDeployFlink = useHasPermission("workspace:flink:deploy");
  const canEditFlink = useHasPermission("workspace:flink:edit");

  const [showNew, setShowNew] = useState(false);
  // Hold the id, not the row object: the list polls every 4s, so deriving the
  // selected job from fresh data keeps the detail screen's status live (and
  // drops back to the list if the job is deleted underneath us).
  // Remembered across a refresh (src/shared/usePersistedState.ts), so a reload keeps the job's page open.
  const [selectedId, setSelectedId] = usePersistedState<UUID | null>("flink.job", null, isStringOrNull);
  const [form, setForm] = useState(EMPTY_FORM);
  // A job picked in the header search opens here (also when this page is already showing).
  const openJob = useSelector((s: RootState) => s.ui.openJob);
  useEffect(() => {
    if (openJob?.section !== "flink") return;
    setSelectedId(openJob.id);
    dispatch(clearOpenJob());
  }, [openJob, setSelectedId, dispatch]);

  const selected = jobs?.find((j) => j.id === selectedId) ?? null;
  // The remembered job is gone (deleted elsewhere): forget it. Only once the list has loaded.
  useEffect(() => { if (selectedId && jobs && !selected) setSelectedId(null); }, [selectedId, jobs, selected, setSelectedId]);

  const err = (e: unknown, fallback: string) =>
    dispatch(notify({ kind: "error", message: apiErrorMessage(e, asApiError(e).error ?? fallback, { prefer: "detail" }) }));

  // Close the drawer and drop whatever was typed, so reopening starts clean.
  const closeDrawer = () => { setShowNew(false); setForm(EMPTY_FORM); };

  const handleRegister = async () => {
    if (!form.git_url.trim()) return;
    try {
      const job = await createFromGit({
        git_url: form.git_url.trim(),
        // W-014: empty = "the repository's default branch"; a ref that is typed is a requirement (the backend never substitutes another).
        git_ref: form.git_ref.trim() || undefined,
        sql_path: form.sql_path.trim() || undefined,
        name: form.name.trim() || undefined,
        parallelism: Number(form.parallelism) || 1,
        sink_engine: REGISTERED_SINK_ENGINE,
      }).unwrap();
      dispatch(notify({ kind: "success", message: `Registered job "${job.name}" from Git` }));
      closeDrawer(); // success → dismiss; on failure it stays open with the input intact
    } catch (e) { err(e, "Registration failed"); }
  };

  // Open this job's page in the Flink Web UI, where its exceptions/stack traces
  // and the TaskManager log links live. Flink keys its pages on the cluster's
  // own JobID, which only the runner's /status endpoint exposes — so resolve
  // that first rather than guessing from our UUID.
  const openLogs = async (job: FlinkJob) => {
    const base = flinkUi?.url;
    if (!base) {
      dispatch(notify({ kind: "info", message: "Flink UI URL isn't configured for this deployment (set FLINK_UI_URL)." }));
      return;
    }
    let flinkJobId: string | null = null;
    let flinkState: string | null = null;
    try {
      const st = await fetchStatus(job.id).unwrap();
      flinkJobId = st.flink_job_id;
      flinkState = st.flink_state;
    } catch {
      /* fall through to the cluster-wide view below */
    }
    if (!flinkJobId) {
      // Never deployed, or the cluster has forgotten it. Flink has no per-job
      // log file, so the JobManager log is the only thing left to show.
      dispatch(notify({
        kind: "info",
        message: `"${job.name}" has no Flink job id yet — deploy it first. Opening the JobManager log.`,
      }));
      window.open(`${base}/#/job-manager/logs`, "_blank", "noopener,noreferrer");
      return;
    }
    window.open(flinkUiJobUrl(base, flinkJobId, flinkState), "_blank", "noopener,noreferrer");
  };

  const act = async (label: string, fn: () => Promise<unknown>) => {
    try { await fn(); dispatch(notify({ kind: "success", message: label })); }
    catch (e) { err(e, `${label} failed`); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "#fff", overflow: "hidden" }}>
      {/* Header — the job list, or the selected job with a way back. Every
          per-job action lives in the detail screen, not on the list rows. */}
      {!selected ? (
        <PageHeader
          eyebrow="Jobs"
          title="FlinkSQL Jobs"
          subtitle="Register, validate and deploy streaming SQL jobs to the Flink cluster."
          actions={(
            <>
              <IconAction icon={ACTION_ICON.refresh} label="Refresh jobs" style={{ alignSelf: "center" }} onClick={() => refetch()} />
              {canEditFlink && <button onClick={() => (showNew ? closeDrawer() : setShowNew(true))} style={pageBtn()}><i className="ti ti-plus" style={{ fontSize: 14 }} /> Register job</button>}
            </>
          )}
        />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", borderBottom: "0.5px solid #E3E8E2", flexShrink: 0 }}>
            <button
              onClick={() => setSelectedId(null)}
              title="Back to all jobs"
              style={{ background: "none", border: "0.5px solid #cfd8d1", borderRadius: 6, cursor: "pointer", color: "#5b6d62", height: 26, width: 26, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}
            ><i className="ti ti-arrow-left" style={{ fontSize: 14 }} /></button>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{selected.name}</div>
              <div style={{ fontSize: 11, color: "#73827a", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {(() => { const g = provenanceOf(selected); return g.url ? `${g.url}@${g.ref}` : "not registered from Git"; })()}
              </div>
            </div>
            <span style={{ color: STATUS_COLOR[selected.status] ?? "#5b6d62", fontWeight: 500, fontSize: 12, flexShrink: 0 }}>● {selected.status}</span>
          </div>

          {/* Per-job actions */}
          <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "8px 16px", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9", flexShrink: 0, flexWrap: "wrap" }}>
            {canEditFlink && (
              <button onClick={() => act("Validated", () => validateJob(selected.id).unwrap())} style={btn("#fff", "#2E3D36", true)}>
                <i className="ti ti-checkup-list" style={{ fontSize: 13 }} /> Validate
              </button>
            )}
            {canDeployFlink && (<>
              <button onClick={() => act("Deploy requested", () => deployJob(selected.id).unwrap())} style={{ ...btn("#fff", "#16a34a", true) }}>
                <i className="ti ti-rocket" style={{ fontSize: 13 }} /> Deploy
              </button>
              <button onClick={() => act("Stop requested", () => stopJob(selected.id).unwrap())} style={{ ...btn("#fff", "#a8470a", true) }}>
                <i className="ti ti-player-stop" style={{ fontSize: 13 }} /> Stop
              </button>
            </>)}
            {canEditFlink && (() => {
              const g = provenanceOf(selected);
              return (
                <button
                  onClick={() => act("Synced from Git", () => syncJob({ id: selected.id }).unwrap())}
                  disabled={!g.url || syncing}
                  title={g.url ? "Re-read the repository and update this job's SQL" : "This job was not registered from Git"}
                  style={{ ...btn("#fff", g.url ? "#0e7490" : "#73827a", true), cursor: g.url ? "pointer" : "not-allowed" }}
                >
                  <Glyph name="pi-refresh" size={13} /> {syncing ? "Syncing…" : "Sync from Git"}
                </button>
              );
            })()}
            <button
              onClick={() => openLogs(selected)}
              title={flinkUi?.url ? "Open this job in the Flink UI" : "Flink UI URL isn't configured"}
              style={{ ...btn("#fff", flinkUi?.url ? "#7c3aed" : "#73827a", true), cursor: flinkUi?.url ? "pointer" : "not-allowed" }}
            >
              <i className="ti ti-file-text" style={{ fontSize: 13 }} /> View logs
            </button>
            {canEditFlink && (
              <IconAction icon={ACTION_ICON.delete} label="Delete job" danger style={{ marginLeft: "auto" }}
                onClick={async () => { if (await askConfirm({ title: "Delete Flink job", message: `Delete job "${selected.name}"?`, confirmLabel: "Delete", danger: true })) { act("Deleted", () => deleteJob(selected.id).unwrap()); setSelectedId(null); } }} />
            )}
          </div>
        </>
      )}

      {/* Register-from-Git drawer — slides in from the right so the jobs list
          stays visible behind it. Closes on success, Escape, ×, or backdrop. */}
      {showNew && canEditFlink && (
        <Drawer
          title="Register FlinkSQL job"
          icon="ti-git-fork"
          onClose={closeDrawer}
          busy={creating}
          footer={<>
            <button onClick={handleRegister} disabled={creating || !form.git_url.trim()}
              style={{ ...drawerBtn(true), flex: 1, opacity: creating || !form.git_url.trim() ? 0.55 : 1, cursor: creating || !form.git_url.trim() ? "not-allowed" : "pointer" }}>
              {creating ? "Cloning…" : "Register job"}
            </button>
            <button onClick={closeDrawer} style={drawerBtn(false)}>Cancel</button>
          </>}
        >
          <div style={{ fontSize: 12, color: "#5b6d62", lineHeight: 1.55, marginBottom: 14 }}>
            The repository's FlinkSQL is cloned and submitted to the runner. Author it in the
            FlinkSQL editor and push it first.
          </div>

          <Field label="Git repository URL" required>
            <input autoFocus placeholder="https://github.com/acme/flink-jobs.git" value={form.git_url}
              onChange={(e) => setForm((f) => ({ ...f, git_url: e.target.value }))}
              style={{ ...inp, fontFamily: "monospace" }} />
          </Field>

          <Field label="Branch or tag" hint="Leave empty for the repository's default branch. A name that cannot be found is an error, not a silent switch to another branch.">
            <input placeholder="default branch" value={form.git_ref}
              onChange={(e) => setForm((f) => ({ ...f, git_ref: e.target.value }))}
              style={{ ...inp, fontFamily: "monospace" }} />
          </Field>

          <Field label="SQL path in repo" hint="A file or folder. Leave empty to auto-detect job.sql / main.sql, or concatenate every .sql file.">
            <input placeholder="jobs/orders.flink.sql" value={form.sql_path}
              onChange={(e) => setForm((f) => ({ ...f, sql_path: e.target.value }))}
              style={{ ...inp, fontFamily: "monospace" }} />
          </Field>

          <Field label="Job name" hint="Defaults to the repository name.">
            <input placeholder="orders_stream" value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} style={inp} />
          </Field>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <Field label="Parallelism">
              <input type="number" min={1} max={512} value={form.parallelism}
                onChange={(e) => setForm((f) => ({ ...f, parallelism: Number(e.target.value) }))}
                style={inp} />
            </Field>
          </div>

          {/* No token field: a private repo is cloned with the Git token
              already saved for this user under Files → Changes. */}
          <div style={{ fontSize: 11.5, color: "#73827a", lineHeight: 1.5, marginTop: 6 }}>
            Private repo? Save a Git token once under <strong style={{ fontWeight: 600 }}>Changes → Set / update Git token</strong>.
          </div>
        </Drawer>
      )}

      <div style={{ flex: 1, overflow: "auto" }}>
        {/* ── Job detail ── */}
        {selected && (
          <div style={{ padding: "12px 16px 16px" }}>
            {selected.validation_errors?.length > 0 && (
              <div style={{ marginBottom: 12, padding: "8px 10px", background: "#fbe9e9", border: "0.5px solid #fecaca", borderRadius: 6, color: "#b23b3b", fontSize: 12, whiteSpace: "pre-wrap" }}>
                <div style={{ fontWeight: 600, marginBottom: 3 }}>Validation errors</div>
                {selected.validation_errors.join("\n")}
              </div>
            )}
            {userDescription(selected.description) && (
              <div style={{ fontSize: 12.5, color: "#4b5a53", marginBottom: 12, lineHeight: 1.5 }}>{userDescription(selected.description)}</div>
            )}
            {(() => {
              const g = provenanceOf(selected);
              return (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "10px 18px", marginBottom: 14, padding: "10px 12px", background: "#f8fbff", border: "0.5px solid #E3E8E2", borderRadius: 8 }}>
                  {g.url && <Meta label="Repository" value={g.url} mono />}
                  {(g.ref || g.checkedOutRef) && <Meta label="Ref" value={g.requestedRef ? g.requestedRef : `${g.checkedOutRef ?? "default"} (default branch)`} mono />}
                  {g.path && g.path !== "." && <Meta label="SQL path" value={g.path} mono />}
                  {g.sha && <Meta label="Synced sha" value={g.sha.slice(0, 7)} mono />}
                  {g.importedAt && <Meta label="Imported" value={new Date(g.importedAt).toLocaleString()} />}
                  <Meta label="Parallelism" value={String(selected.parallelism)} />
                </div>
              );
            })()}
            {/* Read-only: the SQL comes from the repo, so edit it there. */}
            <div style={{ fontSize: 10.5, color: "#73827a", textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 4 }}>
              SQL from repository (read-only)
            </div>
            <pre style={{ margin: 0, padding: 12, background: "#0f1117", color: "#e0e0e0", borderRadius: 6, fontSize: 12, lineHeight: 1.5, overflow: "auto" }}>{selected.sql}</pre>
          </div>
        )}

        {/* ── Job list ── */}
        {!selected && isLoading && <div style={{ padding: 24, color: "#73827a", fontSize: 13 }}>Loading jobs…</div>}
        {!selected && isError && <div style={{ padding: 24, color: "#b23b3b", fontSize: 13 }}>Couldn't reach the FlinkSQL Runner.</div>}
        {!selected && jobs && jobs.length === 0 && (
          <div style={{ padding: 40, textAlign: "center" }}>
            <i className="ti ti-wave-sine" style={{ fontSize: 32, color: "#cfd8d1", display: "block", marginBottom: 10 }} />
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>No FlinkSQL jobs yet</div>
            <div style={{ fontSize: 12.5, color: "#5b6d62" }}>Register a Git repository containing your FlinkSQL to get started.</div>
          </div>
        )}
        {!selected && jobs && jobs.length > 0 && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: GRID, padding: "6px 16px", fontSize: 10.5, color: "#73827a", textTransform: "uppercase", letterSpacing: ".06em", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9" }}>
              <div>Name</div><div>Status</div><div>Repository</div><div />
            </div>
            {jobs.map((j) => {
              const git = provenanceOf(j);
              return (
                <div
                  key={j.id}
                  onClick={() => setSelectedId(j.id)}
                  style={{ display: "grid", gridTemplateColumns: GRID, padding: "10px 16px", alignItems: "center", fontSize: 13, cursor: "pointer", borderBottom: "0.5px solid #eef2ed", background: "white" }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "#F8FAF9")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "white")}
                >
                  <div style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.name}</div>
                  <div><span style={{ color: STATUS_COLOR[j.status] ?? "#5b6d62", fontWeight: 500, fontSize: 12 }}>● {j.status}</span></div>
                  <div style={{ fontSize: 11.5, color: "#5b6d62", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {git.url ? <>{shortRepo(git.url)}<span style={{ color: "#73827a" }}>@{git.ref}</span></> : <span style={{ color: "#cfd8d1" }}>—</span>}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end" }}>
                    <i className="ti ti-chevron-right" style={{ color: "#cfd8d1", fontSize: 14 }} />
                  </div>
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}

// One label/value pair in the job detail's metadata grid.
function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10, color: "#73827a", textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 2 }}>{label}</div>
      <div
        title={value}
        style={{ fontSize: 12, color: "#2E3D36", fontFamily: mono ? "monospace" : "inherit", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
      >{value}</div>
    </div>
  );
}

const GRID = "1.5fr 0.8fr 1.8fr 0.3fr";

function shortRepo(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\.git$/, "").split("/").slice(-2).join("/");
}

function btn(bg: string, color: string, bordered = false): React.CSSProperties {
  return { height: 28, padding: "0 11px", fontSize: 12, background: bg, color, border: bordered ? "0.5px solid #cfd8d1" : "none", borderRadius: 6, cursor: "pointer", display: "flex", alignItems: "center", gap: 5 };
}

