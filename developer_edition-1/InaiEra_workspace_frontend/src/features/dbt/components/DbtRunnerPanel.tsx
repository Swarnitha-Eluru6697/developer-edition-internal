/**
 * DbtRunnerPanel — the dbt Transformer surface inside Workspace.
 *
 * Surfaces the inaiera-dbt-runner API via the workspace proxy
 * (/api/v1/dbt/*). Two levels, list → detail:
 *
 *   Project list  — every registered project; "Register project" opens the
 *                   Git drawer. Clicking a row drills in.
 *   Project screen — that project's own tabs, with a back arrow and "Run now":
 *                     Runs      run history, expand a run for its node events
 *                     Lineage   source → model → mart graph
 *                     Schedule  schedule_cron + schedule_enabled
 *
 * Runs/Lineage/Schedule are deliberately NOT top-level tabs: they are
 * meaningless without a project, so they only exist inside one.
 *
 * API contract (all calls go through workspace proxy):
 *   GET    /api/v1/dbt/projects
 *   POST   /api/v1/dbt/projects/register
 *   GET    /api/v1/dbt/projects/{id}/runs
 *   POST   /api/v1/dbt/projects/{id}/run
 *   GET    /api/v1/dbt/projects/{id}/runs/{rid}/events
 *   GET    /api/v1/dbt/projects/{id}/lineage
 *   PATCH  /api/v1/dbt/projects/{id}
 */
import { useEffect, useState } from "react";
import { isStringOrNull, oneOf, usePersistedState } from "../../../shared/usePersistedState";
import { useDispatch, useSelector } from "react-redux";
import { PageHeader } from "../../../shared/ui/PageHeader";
import { pageBtn } from "../../../shared/ui/pageStyles";
import {
  useDeleteDbtProjectMutation,
  useGetDbtLineageQuery,
  useListDbtProjectsQuery,
  useListDbtRunEventsQuery,
  useListDbtRunsQuery,
  useTriggerDbtRunMutation,
  useUpdateDbtProjectMutation,
} from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { clearOpenJob, notify, type NavSection } from "../../../store/uiSlice";
import { useHasPermission } from "../../../rbac/hooks";
import { askConfirm } from "../../../shared/ui/dialogStore";
import type { AppDispatch, RootState } from "../../../store";
import type { DbtProject, UUID } from "../../../types";
import { EMPTY_PAD, GUTTER } from "../styles";
import { DbtLineageGraph } from "./DbtLineageGraph";
import { DbtProjectHeader, type DbtTab } from "./DbtProjectHeader";
import { DbtProjectList } from "./DbtProjectList";
import { DbtRegisterDrawer } from "./DbtRegisterDrawer";
import { DbtRunsTab } from "./DbtRunsTab";
import { DbtScheduleForm } from "./DbtScheduleForm";
import { DbtDependentsTab } from "./DbtDependentsTab";
import type { JobLinks } from "./DbtJobLinks";
import { JOB_KINDS, JOB_PERMISSIONS, kindOf, type JobKind } from "../jobKinds";

/** The sidebar page each kind of job lives on. */
const JOB_SECTION: Record<JobKind, NavSection> = { dbt: "dbt", sql: "sqljobs", python: "pythonjobs" };

/** `kind` picks which jobs this screen is for: dbt projects (Jobs → dbt) or SQL jobs (Jobs → SQL). */
export function DbtRunnerPanel({ kind = "dbt" }: { kind?: JobKind }) {
  const k = JOB_KINDS[kind];
  const dispatch = useDispatch<AppDispatch>();
  // Tabs exist only *inside* a project: the panel shows the project list, and
  // picking a project drills into that project's own screen. Which project is open and which tab (per kind) are remembered across a
  // refresh and a trip to another page (src/shared/usePersistedState.ts): without it a reload dropped you back at the list.
  const [tab, setTab] = usePersistedState<DbtTab>(`jobs.${kind}.tab`, "runs", oneOf<DbtTab>("runs", "lineage", "schedule", "dependents"));
  const [selectedId, setSelectedId] = usePersistedState<UUID | null>(`jobs.${kind}.project`, null, isStringOrNull);
  const [selectedRunId, setSelectedRunId] = useState<UUID | null>(null);
  // A job picked in the header search opens here (also when this page is already showing).
  const openJob = useSelector((s: RootState) => s.ui.openJob);
  useEffect(() => {
    if (openJob?.section !== JOB_SECTION[kind]) return;
    setSelectedId(openJob.id);
    setTab("runs");
    setSelectedRunId(null);
    dispatch(clearOpenJob());
  }, [openJob, kind, setSelectedId, setTab, dispatch]);
  const [showRegister, setShowRegister] = useState(false);

  // While any project's latest run is still queued/running/cancelling, keep refreshing the list so its
  // "Last run" cell goes queued → running → succeeded/failed/cancelled by itself (also for runs started
  // elsewhere, e.g. by the scheduler). Stops polling once everything is settled.
  const [listPollMs, setListPollMs] = useState(0);
  // Every project of every kind (the Dependents picker links across kinds); the list shows this screen's kind only.
  const { data: allProjects, isLoading: loadingProjects, refetch: refetchProjects } = useListDbtProjectsQuery({}, { pollingInterval: listPollMs });
  const projects = allProjects?.filter((p) => kindOf(p) === kind);
  // The open project, read from the list itself (the list polls, so its status stays live; null while the list loads).
  const selectedProject = (selectedId && projects?.find((p) => p.id === selectedId)) || null;
  // The remembered project is gone (deleted elsewhere): back to the list. Only once the list has really loaded.
  useEffect(() => {
    if (selectedId && projects && !selectedProject) { setSelectedId(null); setTab("runs"); }
  }, [selectedId, projects, selectedProject, setSelectedId, setTab]);
  // A remembered Lineage tab means nothing for a kind that has no lineage.
  useEffect(() => { if (tab === "lineage" && !k.hasLineage) setTab("runs"); }, [tab, k.hasLineage, setTab]);
  const anyRunActive = !!projects?.some((p) => ["queued", "running", "cancelling"].includes(p.last_run?.status ?? ""));
  useEffect(() => { setListPollMs(anyRunActive ? 3000 : 0); }, [anyRunActive]);
  // Poll so a run's status updates live (queued → running → succeeded/failed)
  // without the user having to reselect the project.
  const { data: runs } = useListDbtRunsQuery(selectedProject?.id ?? "", { skip: !selectedProject, pollingInterval: 3000 });
  const { data: events } = useListDbtRunEventsQuery(
    { projectId: selectedProject?.id ?? "", runId: selectedRunId ?? "" },
    { skip: !selectedProject || !selectedRunId, pollingInterval: 3000 },
  );
  const { data: lineage } = useGetDbtLineageQuery(selectedProject?.id ?? "", {
    skip: !selectedProject || tab !== "lineage" || !k.hasLineage,
  });

  const [triggerRun] = useTriggerDbtRunMutation();
  const [updateProject, { isLoading: savingProject }] = useUpdateDbtProjectMutation();
  // The drilled-in project as the list last returned it (its links change after a save or a run).
  const current = selectedProject;

  const handleSaveLinks = async ({ upstream, downstream }: JobLinks) => {
    if (!current) return;
    try {
      // Both lists in one request: the Runner judges the result as a whole (a job moved from one side to the other is not a loop).
      await updateProject({ id: current.id, body: { upstream_project_ids: upstream, dependent_project_ids: downstream } }).unwrap();
      const count = (n: number, word: string) => `${n} ${word} job${n > 1 ? "s" : ""}`;
      dispatch(notify({ kind: "success", message: upstream.length || downstream.length
        ? `Saved ${[upstream.length && count(upstream.length, "upstream"), downstream.length && count(downstream.length, "downstream")].filter(Boolean).join(" and ")} for "${current.name}"`
        : `Removed the upstream and downstream jobs of "${current.name}"` }));
      refetchProjects();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not save the upstream and downstream jobs", { prefer: "detail" }) }));
    }
  };
  const [deleteProject] = useDeleteDbtProjectMutation();
  // Each kind of job has its own permissions: dbt projects workspace:dbt:edit / :run, SQL jobs workspace:sql-jobs:*, Python jobs
  // workspace:python-jobs:*. "Run now" needs `run`; registering and deleting need `edit` (this page opens with either).
  const canRunJob = useHasPermission(JOB_PERMISSIONS[kind].run);
  const canEditJob = useHasPermission(JOB_PERMISSIONS[kind].edit);

  const handleDeleteProject = async (p: DbtProject) => {
    if (!(await askConfirm({ title: "Delete dbt project", message: `Delete project "${p.name}"? This removes its run history and lineage too.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await deleteProject(p.id).unwrap();
      if (selectedProject?.id === p.id) setSelectedId(null);
      dispatch(notify({ kind: "success", message: `Deleted "${p.name}"` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Delete failed", { prefer: "detail" }) }));
    }
  };

  // Drill into a project: reset the tab and any expanded run from a previous one.
  const openProject = (p: DbtProject) => { setSelectedId(p.id); setTab("runs"); setSelectedRunId(null); };

  const handleTriggerRun = async () => {
    if (!selectedProject) return;
    try {
      await triggerRun({ projectId: selectedProject.id }).unwrap();
      dispatch(notify({ kind: "success", message: `Run queued for "${selectedProject.name}"` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not start the run", { prefer: "detail" }) }));
    }
  };

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "#fff" }}>
      {/* Header — the project list, or the selected project with a way back. */}
      {!selectedProject ? (
        <PageHeader
          eyebrow="Jobs"
          title={k.title}
          subtitle={k.emptyHint}
          actions={canEditJob ? (
            <button onClick={() => setShowRegister((open) => !open)} style={pageBtn()}>
              <i className="ti ti-plus" style={{ fontSize: 14 }} />{k.registerLabel}
            </button>
          ) : undefined}
        />
      ) : (
        <DbtProjectHeader
          project={selectedProject}
          tab={tab}
          onTab={setTab}
          onBack={() => { setSelectedId(null); setTab("runs"); }}
          onRun={canRunJob ? handleTriggerRun : undefined}
          hasLineage={k.hasLineage}
        />
      )}

      {showRegister && canEditJob && <DbtRegisterDrawer kind={kind} projects={allProjects ?? []} onClose={() => setShowRegister(false)} onRegistered={refetchProjects} />}

      <div style={{ flex: 1, overflow: "auto" }}>
        {!selectedProject && (
          <DbtProjectList
            projects={projects}
            loading={loadingProjects}
            onOpen={openProject}
            onDelete={canEditJob ? handleDeleteProject : undefined}
            kind={kind}
          />
        )}

        {selectedProject && tab === "runs" && (
          <DbtRunsTab runs={runs} events={events} selectedRunId={selectedRunId} onSelectRun={setSelectedRunId} unit={k.unit} />
        )}

        {selectedProject && tab === "lineage" && k.hasLineage && (
          <div style={{ padding: GUTTER }}>
            {!lineage?.edges?.length ? (
              <div style={{ textAlign: "center", color: "#9ca3af", fontSize: 13, padding: EMPTY_PAD }}>
                No lineage data yet. Run the project successfully to generate it.
              </div>
            ) : (
              <DbtLineageGraph edges={lineage.edges} />
            )}
          </div>
        )}

        {current && tab === "dependents" && (
          <div style={{ padding: GUTTER }}>
            <DbtDependentsTab
              key={`${current.id}:${(current.upstream_project_ids ?? []).join(",")}:${(current.dependent_project_ids ?? []).join(",")}`}
              project={current}
              projects={allProjects ?? []}
              saving={savingProject}
              onSave={handleSaveLinks}
            />
          </div>
        )}

        {selectedProject && tab === "schedule" && (
          <div style={{ padding: GUTTER }}>
            <DbtScheduleForm
              key={selectedProject.id}
              project={selectedProject}
              onSave={(cron, enabled) => {
                updateProject({ id: selectedProject.id, body: { schedule_cron: cron, schedule_enabled: enabled } });
                dispatch(notify({ kind: "success", message: enabled ? `Schedule saved: ${cron}` : "Schedule disabled" }));
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
