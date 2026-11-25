/**
 * DataQualityPanel — the Data Quality surface inside Workspace.
 *
 * All of Data Quality in one place: authoring the rule library and the
 * rulesets built from it, attaching a ruleset to a job that runs here, and
 * reading back the artifact that gets injected into that job.
 *
 * Four tabs:
 *   Rulesets     — rules from the library pinned to columns of one table.
 *   Rule library — system rules (read-only) and your own reusable rules.
 *   Bindings — list / create / remove bindings (ruleset ↔ dbt project, Flink
 *              job or Python job), filterable by job type.
 *   Render   — the stack-specific artifact rendered from the selected
 *              binding's ruleset: a dbt schema.yml block, a FlinkSQL quality
 *              job, or a GX suite + Soda checks.
 *
 * The job-type dropdowns are backed by the real project/job lists, so a
 * binding points at a job that exists rather than a UUID typed by hand.
 *
 * API contract (all calls go through the workspace proxy at /api/v1/dq/*):
 *   GET    /api/v1/dq/rulesets
 *   GET    /api/v1/dq/bindings[?job_type=]
 *   POST   /api/v1/dq/bindings
 *   DELETE /api/v1/dq/bindings/{id}
 *   GET    /api/v1/dq/render/{binding_id}
 *   POST   /api/v1/dq/render/{binding_id}/refresh
 */
import { useEffect, useState } from "react";
import { isStringOrNull, oneOf, usePersistedState } from "../../../shared/usePersistedState";
import { useHasPermission } from "../../../rbac/hooks";
import { useListDqBindingsQuery } from "../api";
import type { DqBinding, DqJobType } from "../../../types";
import { JOB_TYPES } from "../dqUi";
import { useBindingForm } from "../hooks/useBindingForm";
import { useDqBindingActions } from "../hooks/useDqBindingActions";
import { DqBindingsTable } from "./DqBindingsTable";
import { DqCreateBindingDrawer } from "./DqCreateBindingDrawer";
import { DqRenderView } from "./DqRenderView";
import { DqRulesetsView } from "./DqRulesetsView";
import { DqRuleLibraryView } from "./DqRuleLibraryView";
import { PageHeader } from "../../../shared/ui/PageHeader";
import { pageBtn } from "../../../shared/ui/pageStyles";
import SearchSelect from "../../../shared/ui/SearchSelect";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

type DqTab = "rulesets" | "library" | "bindings" | "render";
const DQ_TABS: DqTab[] = ["rulesets", "library", "bindings", "render"];
const TAB_LABEL: Record<DqTab, string> = { rulesets: "Rulesets", library: "Rule library", bindings: "Job bindings", render: "Render" };

/** `navItem` is the product-nav selection ("Job Bindings" / "Render"), so a
 *  second sidebar and this panel's tabs stay one control rather than two that
 *  disagree. */
export function DataQualityPanel({ navItem }: { navItem?: string }) {
  // The tab, the job-type filter and the open binding are remembered across a refresh (src/shared/usePersistedState.ts). The binding is
  // kept by id and looked up in the list once it arrives.
  const [tab, setTab] = usePersistedState<DqTab>("dq.tab", "rulesets", oneOf<DqTab>(...DQ_TABS));
  const [filterType, setFilterType] = usePersistedState<DqJobType | "">("dq.filter", "", oneOf<DqJobType | "">("", ...JOB_TYPES.map((j) => j.value)));
  const [savedBindingId, setSavedBindingId] = usePersistedState<string | null>("dq.binding", null, isStringOrNull);
  const [selectedBinding, setOpenBinding] = useState<DqBinding | null>(null);
  const setSelectedBinding = (b: DqBinding | null) => { setOpenBinding(b); setSavedBindingId(b?.id ?? null); };
  const [showCreate, setShowCreate] = useState(false);
  const [newRuleset, setNewRuleset] = useState(false);
  const [newRule, setNewRule] = useState(false);
  const [ruleScope, setRuleScope] = useState<"system" | "org">("system");
  // W-009: binding/resolve/delete change what runs (dq_publish); re-rendering is dq_execute; viewing is dq_read (the nav gate).
  // The backend enforces the same permissions; this only hides what would be refused.
  const canPublish = useHasPermission("workspace:dq:publish");
  const canExecute = useHasPermission("workspace:dq:run");
  const canEdit = useHasPermission("workspace:dq:edit");
  const binding = useBindingForm();
  const actions = useDqBindingActions(selectedBinding, setSelectedBinding);

  const { data: bindings, isLoading: loadingBindings } = useListDqBindingsQuery(
    filterType ? { jobType: filterType } : undefined,
  );

  // Reopen the remembered binding once the list is in; forget it if it is gone (deleted, or not in this filter any more).
  const restoring = !!savedBindingId && !selectedBinding;                // a remembered binding not reopened yet (or about to be forgotten)
  useEffect(() => {
    if (!savedBindingId || selectedBinding || !bindings) return;
    const found = bindings.find((b) => b.id === savedBindingId);
    if (found) setOpenBinding(found);
    else setSavedBindingId(null);
  }, [savedBindingId, selectedBinding, bindings, setSavedBindingId]);

  // Follow the product nav when it changes.
  useEffect(() => {
    if (navItem === "Rulesets") setTab("rulesets");
    else if (navItem === "Rule library") setTab("library");
    else if (navItem === "Job Bindings") setTab("bindings");
    else if (navItem === "Render") setTab("render");
  }, [navItem, setTab]);

  // The Render tab is about one binding — without a selection there is nothing
  // to show, so fall back to the list rather than an empty screen.
  useEffect(() => {
    if (tab === "render" && !selectedBinding && !restoring) setTab("bindings");
  }, [tab, selectedBinding, restoring, setTab]);

  const { reset: resetForm } = binding;
  const closeDrawer = () => { setShowCreate(false); resetForm(); };
  const openRender = (b: DqBinding) => { setSelectedBinding(b); setTab("render"); };
  const handleDelete = async (b: DqBinding) => {
    if (await actions.remove(b) && selectedBinding?.id === b.id) { setSelectedBinding(null); setTab("bindings"); }
  };

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "#fff" }}>
      {/* Header */}
      <PageHeader
        eyebrow="Govern"
        title="Data Quality"
        subtitle="Write quality rules, group them into rulesets, and bind a ruleset to a dbt, Flink or Python job."
        actions={(tab === "bindings" && canPublish) || (tab === "render" && selectedBinding && canExecute)
          || ((tab === "rulesets" || tab === "library") && canEdit) ? (
          <>
            {tab === "rulesets" && canEdit && (
              <button onClick={() => setNewRuleset(true)} style={pageBtn()}>
                <i className="ti ti-plus" style={{ fontSize: 14 }} />New ruleset
              </button>
            )}
            {tab === "library" && canEdit && (
              <button onClick={() => setNewRule(true)} style={pageBtn()}>
                <i className="ti ti-plus" style={{ fontSize: 14 }} />New rule
              </button>
            )}
            {tab === "bindings" && canPublish && (
              <button onClick={() => (showCreate ? closeDrawer() : setShowCreate(true))} style={pageBtn()}>
                <i className="ti ti-plus" style={{ fontSize: 14 }} />New binding
              </button>
            )}
            {tab === "render" && selectedBinding && canExecute && (
              <IconAction icon={ACTION_ICON.refresh} label={actions.refreshing ? "Rendering…" : "Re-render"}
                disabled={actions.refreshing} style={{ alignSelf: "center" }} onClick={actions.refresh} />
            )}
          </>
        ) : undefined}
      />

      {/* Tabs */}
      <div style={{ display: "flex", alignItems: "center", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9", flexShrink: 0 }}>
        {DQ_TABS.map((t) => (
          <div
            key={t}
            onClick={() => { if (t !== "render" || selectedBinding) setTab(t); }}
            title={t === "render" && !selectedBinding ? "Pick a binding first" : undefined}
            style={{
              padding: "9px 16px", fontSize: 12.5,
              cursor: t === "render" && !selectedBinding ? "not-allowed" : "pointer",
              color: tab === t ? "#14201B" : t === "render" && !selectedBinding ? "#c4c4c0" : "#5b6d62",
              borderBottom: tab === t ? "2px solid #0A7D5A" : "2px solid transparent",
              fontWeight: tab === t ? 500 : 400,
            }}
          >{TAB_LABEL[t]}</div>
        ))}
        {tab === "bindings" && (
          <SearchSelect
            size="sm"
            ariaLabel="Filter by job type"
            noun="job type"
            menuMinWidth={220}
            style={{ marginLeft: "auto", marginRight: 16, width: 170 }}
            value={filterType}
            onChange={(v) => setFilterType(v as DqJobType | "")}
            options={[{ value: "", label: "All job types" }, ...JOB_TYPES]}
          />
        )}
      </div>

      {showCreate && canPublish && <DqCreateBindingDrawer binding={binding} onClose={closeDrawer} />}

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {tab === "rulesets" && (
          <DqRulesetsView canEdit={canEdit} creating={newRuleset} onCloseCreate={() => setNewRuleset(false)} />
        )}
        {tab === "library" && (
          <DqRuleLibraryView canEdit={canEdit} creating={newRule} onCloseCreate={() => setNewRule(false)}
            scope={ruleScope} onScope={setRuleScope} />
        )}
        {tab === "bindings" && (
          <DqBindingsTable
            bindings={bindings}
            loading={loadingBindings}
            filtered={!!filterType}
            selectedId={selectedBinding?.id ?? null}
            resolving={actions.resolving}
            canPublish={canPublish}
            onOpen={openRender}
            onResolve={actions.resolve}
            onDelete={handleDelete}
          />
        )}
        {tab === "render" && selectedBinding && (
          <DqRenderView binding={selectedBinding} onBack={() => setTab("bindings")} />
        )}
      </div>
    </div>
  );
}
