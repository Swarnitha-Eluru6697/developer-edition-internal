import { useEffect, useMemo, useState } from "react";
import { usePageSearch } from "../context/PageSearchContext";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import { TestDrawer } from "../components/TestDrawer";
import { SearchNote } from "../components/SearchNote";
import { domainBadge, kindBadge, matchesDomainView, CATEGORY_LABELS } from "../components/modelBadges";
import { openModelOrTrainingRun } from "../components/modelNavigation";
import type { BaseModel, CatalogModel, TrainingRun } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

type Lifecycle = "base" | "training" | "trained" | "deployed" | "failed";
const LIFECYCLE: { key: Lifecycle; label: string }[] = [
  { key: "base", label: "Base models" },
  { key: "training", label: "In-training" },
  { key: "trained", label: "Trained" },
  { key: "deployed", label: "Deployed" },
  { key: "failed", label: "Failed" },
];

const DOMAIN_VIEWS = [
  { key: "all", label: "All" },
  { key: "ml", label: "Classical ML" },
  { key: "deep-learning", label: "Deep Learning" },
  { key: "llm", label: "LLM" },
] as const;
type DomainView = (typeof DOMAIN_VIEWS)[number]["key"];

// Lifecycle is derived from what the model actually has, not from a status
// column that would need keeping in sync:
//
//   deployed  something is genuinely serving it — a deployment row, or a
//             version promoted to champion. The API decides this, because
//             "deployed" is a fact about the deployments table, not about an
//             alias string. In this platform a newly-trained version is
//             registered as `challenger`, a promotion CANDIDATE that has never
//             served a request; counting that as deployed would put every
//             model ever trained here under Deployed and leave Trained empty.
//   trained   a real version exists, and nothing is serving it yet — the
//             candidate sitting in Deploy & Promote waiting for a decision.
//   training  a run is queued or running RIGHT NOW. That lives in
//             training_runs, not in the catalog, and the API joins it in — a
//             model being trained for the first time has no catalog row at all
//             and arrives as a `pending` entry with no versions.
//
// A model can legitimately match more than one chip (serving v2 while v3
// trains), exactly as the lifecycle itself allows.
const isDeployed = (m: CatalogModel) => m.deployed;
const isTrained = (m: CatalogModel) => !m.deployed && m.versions.length > 0;
const isTraining = (m: CatalogModel) => m.training === true;

function matchesSearch(haystack: string, q: string) {
  if (!q.trim()) return true;
  return haystack.toLowerCase().includes(q.trim().toLowerCase());
}

export default function ModelCatalogPage() {
  const { can } = usePermissions();
  const navigate = useNavigate();
  const models = useApi(api.getModels, []);
  const baseModels = useApi(api.getBaseModels, []);
  // Failed lives on the training run, not the catalog: a model whose only run failed
  // never got a catalog row, so the run list is the one place that knows about it.
  const runs = useApi(api.getTrainingRuns, []);

  // The chip lives in the URL so going into a model and coming back returns to
  // the tab you left, and so a link to "the in-training models" is a link
  // rather than an instruction to click something.
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTab = searchParams.get("tab") as Lifecycle | null;
  // `?model=` opens straight onto one model's drawer — how the base-model
  // page's "derived models" list hands off, instead of rendering its own copy
  // of the same drawer.
  const linkedModelId = searchParams.get("model");

  const [search, setSearch] = usePageSearch("Search models…");
  // Opens on Deployed: what is actually serving traffic is a more useful
  // landing state than the 85-entry base library.
  const [lifecycle, setLifecycle] = useState<Lifecycle>(
    urlTab && LIFECYCLE.some((l) => l.key === urlTab) ? urlTab : "deployed"
  );
  const [domainView, setDomainView] = useState<DomainView>("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [taskFilter, setTaskFilter] = useState("all");
  const [filterPanelOpen, setFilterPanelOpen] = useState(false);
  const [viewMode, setViewMode] = useState<"list" | "card">("card");
  const [drawerModel, setDrawerModel] = useState<CatalogModel | null>(null);
  const [testModel, setTestModel] = useState<CatalogModel | null>(null);
  const [editingModel, setEditingModel] = useState<CatalogModel | null>(null);
  // Editing a model's description is a build action (mlstudio:train:run).
  const canWrite = can(PERM.train);

  const all = baseModels.data ?? [];
  const categories = useMemo(
    () => [...new Set(all.map((b) => b.category).filter((c): c is string => !!c))],
    [all]
  );
  const llmTasks = useMemo(
    () => [...new Set(all.filter((b) => b.domain === "llm").map((b) => b.task))],
    [all]
  );

  // The Filters button only appears once a specific domain is chosen: "All"
  // mixes both domains, and one panel can't cleanly offer Category (which only
  // applies to classical ML) and Task (only LLM) at the same time.
  const filtersAvailable = lifecycle === "base" && (domainView === "ml" || domainView === "llm");
  const activeFilterCount = (categoryFilter !== "all" ? 1 : 0) + (taskFilter !== "all" ? 1 : 0);

  function changeLifecycle(f: Lifecycle) {
    setLifecycle(f);
    setSearchParams(f === "deployed" ? {} : { tab: f }, { replace: true });
    setFilterPanelOpen(false);
    if (f !== "base") {
      setDomainView("all");
      setCategoryFilter("all");
      setTaskFilter("all");
    }
  }
  function changeDomainView(d: DomainView) {
    setDomainView(d);
    setFilterPanelOpen(false);
    if (d !== "ml") setCategoryFilter("all");
    if (d !== "llm") setTaskFilter("all");
  }

  const filteredBase = useMemo(
    () =>
      all.filter(
        (b) =>
          matchesDomainView(b, domainView) &&
          (categoryFilter === "all" || b.category === categoryFilter) &&
          (taskFilter === "all" || b.task === taskFilter) &&
          matchesSearch([b.title, b.task, b.description, b.framework].join(" "), search)
      ),
    [all, domainView, categoryFilter, taskFilter, search]
  );

  const filteredModels = useMemo(
    () =>
      (models.data ?? []).filter((m) => {
        if (lifecycle === "training" && !isTraining(m)) return false;
        if (lifecycle === "trained" && !isTrained(m)) return false;
        if (lifecycle === "deployed" && !isDeployed(m)) return false;
        return matchesSearch([m.id, m.task, m.base_ref || "", m.framework || "", m.description || ""].join(" "), search);
      }),
    [models.data, lifecycle, search]
  );

  // A failure counts only while it is still the model's latest run: once the model is
  // re-trained (or trained again from the wizard), the newer run supersedes it and it
  // leaves this tab — the new run shows under In-training, then Trained.
  const currentFailures = useMemo(() => {
    const latest = new Map<string, TrainingRun>();
    for (const r of runs.data ?? []) {
      const seen = latest.get(r.modelName);
      if (!seen || +new Date(r.createdAt) > +new Date(seen.createdAt)) latest.set(r.modelName, r);
    }
    return [...latest.values()]
      .filter((r) => r.status === "failed")
      .sort((a, b) => +new Date(b.createdAt) - +new Date(a.createdAt));
  }, [runs.data]);
  const failedRuns = useMemo(
    () => currentFailures.filter((r) =>
      matchesSearch([r.modelName, r.baseModelId, r.datasetName, r.error || ""].join(" "), search)),
    [currentFailures, search]
  );
  const failedTotal = currentFailures.length;

  const isBase = lifecycle === "base";
  const isFailed = lifecycle === "failed";
  const count = isBase ? filteredBase.length : isFailed ? failedRuns.length : filteredModels.length;
  const countLabel =
    `${count} ${isBase ? "base model" : isFailed ? "failed run" : "model"}${count === 1 ? "" : "s"}` +
    (search.trim() ? ` matching "${search.trim()}"` : "");

  const loading = isBase ? baseModels.loading : isFailed ? runs.loading : models.loading;
  const error = isBase ? baseModels.error : isFailed ? runs.error : models.error;

  // Shared with the base-model page's derived list — see modelNavigation.ts.
  const openModel = (m: CatalogModel) => openModelOrTrainingRun(navigate, m, setDrawerModel);

  // The top-bar search lands here with ?tab= and ?q=; apply them while already on
  // this page too, not only on first render.
  const urlQuery = searchParams.get("q");
  useEffect(() => {
    if (urlTab && LIFECYCLE.some((l) => l.key === urlTab)) setLifecycle(urlTab);
    if (urlQuery !== null) setSearch(urlQuery);
  }, [urlTab, urlQuery]);

  // Deep link, resolved once the catalog has actually loaded. Cleared from the
  // URL on open so closing the drawer and re-clicking behaves normally.
  useEffect(() => {
    if (!linkedModelId || !models.data) return;
    const found = models.data.find((m) => m.id === linkedModelId);
    if (found) setDrawerModel(found);
    setSearchParams(
      (prev) => { const next = new URLSearchParams(prev); next.delete("model"); return next; },
      { replace: true }
    );
  }, [linkedModelId, models.data, setSearchParams]);

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Model catalog</h1>
            <p>
              Registered once in the Unified Catalog Service — base library, in-training, trained, and
              deployed, all versions of a model in one place.
            </p>
          </div>
          <button hidden={!can(PERM.train)} className="btn btn-primary" onClick={() => navigate("/train")}>
            <Icon name="plus" />
            Train a new model
          </button>
        </div>

        <div style={{ display: "flex", gap: 10, marginBottom: 14, position: "relative" }}>
          <div style={{ flex: 1, display: "flex", alignItems: "center" }}>
            <SearchNote term={search} onClear={() => { setSearch(""); setSearchParams((p) => { const n = new URLSearchParams(p); n.delete("q"); return n; }, { replace: true }); }} />
          </div>
          {filtersAvailable && (
            <button
              className={"dd-trigger" + (filterPanelOpen ? " open" : "")}
              aria-expanded={filterPanelOpen}
              onClick={() => setFilterPanelOpen((o) => !o)}
            >
              <Icon name="filter" />
              Filters
              {activeFilterCount > 0 && <span className="dd-count">{activeFilterCount}</span>}
              <Icon name="chevron-down" className="dd-chev" />
            </button>
          )}

          {filtersAvailable && filterPanelOpen && (
            <div
              className="dd-panel"
              style={{ position: "absolute", top: "100%", right: 0, width: 340, padding: "14px 16px", zIndex: 10 }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <div className="drawer-section-title" style={{ marginBottom: 0 }}>
                  {domainView === "ml" ? "Filter by category" : "Filter by task"}
                </div>
                <IconAction icon="x" label="Clear filters" size="sm" align="end" onClick={() => { setCategoryFilter("all"); setTaskFilter("all"); }} />
              </div>
              <div className="filter-bar" style={{ marginBottom: 0 }}>
                {domainView === "ml" ? (
                  <>
                    <div className={"chip" + (categoryFilter === "all" ? " active" : "")} onClick={() => setCategoryFilter("all")}>
                      All categories
                    </div>
                    {categories.map((c) => (
                      <div key={c} className={"chip" + (categoryFilter === c ? " active" : "")} onClick={() => setCategoryFilter(c)}>
                        {CATEGORY_LABELS[c] ?? c}
                      </div>
                    ))}
                  </>
                ) : (
                  <>
                    <div className={"chip" + (taskFilter === "all" ? " active" : "")} onClick={() => setTaskFilter("all")}>
                      All tasks
                    </div>
                    {llmTasks.map((t) => (
                      <div key={t} className={"chip" + (taskFilter === t ? " active" : "")} onClick={() => setTaskFilter(t)}>
                        {t}
                      </div>
                    ))}
                  </>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="tabs tabs-with-tools">
          <div className="tab-group" role="tablist" aria-label="Model lifecycle">
            {LIFECYCLE.map((f) => (
              <button key={f.key} type="button" role="tab" aria-selected={lifecycle === f.key}
                className={"tab" + (lifecycle === f.key ? " active" : "")} onClick={() => changeLifecycle(f.key)}>
                {f.label}
                {f.key === "failed" && failedTotal > 0 && <span className="tab-count">{failedTotal}</span>}
              </button>
            ))}
          </div>
          {/* Model type narrows the base catalogue only, so it shows only
              under Base models — as a second, smaller tab group on the row's
              right end rather than a second tab row. */}
          {isBase && (
            <div className="tab-group tab-group-end tab-group-sub" role="tablist" aria-label="Filter base models by type">
              {DOMAIN_VIEWS.map((d) => (
                <button key={d.key} type="button" role="tab" aria-selected={domainView === d.key}
                  className={"tab" + (domainView === d.key ? " active" : "")} onClick={() => changeDomainView(d.key)}>
                  {d.label}
                </button>
              ))}
            </div>
          )}
        </div>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "-6px 0 14px" }}>
          <span style={{ fontSize: 12, color: "var(--text-faint)" }}>{countLabel}</span>
          <div className="view-toggle">
            <button className={"has-tip" + (viewMode === "list" ? " active" : "")} onClick={() => setViewMode("list")} data-tip="List view" aria-label="List view">
              <Icon name="list" />
            </button>
            <button className={"has-tip tip-end" + (viewMode === "card" ? " active" : "")} onClick={() => setViewMode("card")} data-tip="Card view" aria-label="Card view">
              <Icon name="grid" />
            </button>
          </div>
        </div>
      </div>

      {loading && <div className="empty-hint">Loading…</div>}
      {error && <div className="callout callout-amber">Couldn't load the catalog: {error}</div>}

      {!loading && !error && isBase && (
        <BaseModelResults
          models={filteredBase}
          viewMode={viewMode}
          onOpen={(id) => navigate(`/base-models/${encodeURIComponent(id)}`)}
        />
      )}

      {!loading && !error && isFailed && (
        <FailedRunResults runs={failedRuns} viewMode={viewMode} onOpen={(id) => navigate(`/training-runs/${encodeURIComponent(id)}`)} />
      )}

      {!loading && !error && !isBase && !isFailed && (
        <CatalogModelResults models={filteredModels} viewMode={viewMode} onOpen={openModel} onEdit={canWrite ? setEditingModel : undefined} />
      )}

      {drawerModel && (
        <ModelDrawer
          model={drawerModel}
          onClose={() => setDrawerModel(null)}
          onTest={() => { setTestModel(drawerModel); setDrawerModel(null); }}
          onEdit={canWrite ? () => setEditingModel(drawerModel) : undefined}
        />
      )}

      {editingModel && (
        <EditModelDescriptionDrawer
          model={editingModel}
          onClose={() => setEditingModel(null)}
          onSaved={(description) => {
            setEditingModel(null);
            if (drawerModel?.id === editingModel.id) setDrawerModel({ ...drawerModel, description });
            models.reload();
          }}
        />
      )}

      {testModel && (
        <TestDrawer
          label={`${testModel.id} · v${testModel.versions[0]?.v ?? "—"}`}
          task={testModel.task}
          domain={testModel.domain}
          modelId={testModel.id}
          onRun={(input) => api.testPredict(testModel.id, input)}
          onClose={() => setTestModel(null)}
        />
      )}
    </div>
  );
}

/* ---------------- base library results ---------------- */

function BaseModelResults({
  models, viewMode, onOpen,
}: { models: BaseModel[]; viewMode: "list" | "card"; onOpen: (id: string) => void }) {
  if (viewMode === "list") {
    return (
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: "26%" }}>Model</th>
              <th style={{ width: "24%" }}>Type</th>
              <th style={{ width: "14%" }}>Task</th>
              <th style={{ width: "22%" }}>Source</th>
              <th style={{ width: "14%" }}>Weights</th>
            </tr>
          </thead>
          <tbody>
            {models.map((b) => (
              <tr key={b.id} onClick={() => onOpen(b.id)}>
                <td>
                  <div className="cell-name">{b.title}</div>
                  <div className="cell-sub">{b.description}</div>
                </td>
                <td><div className="badge-row">{domainBadge(b.domain, b.framework)}</div></td>
                <td><span className="tag">{b.task}</span></td>
                <td><span className="badge badge-outline">Base library</span></td>
                <td>
                  {b.stored_weights_uri ? (
                    <span style={{ color: "var(--teal-700)", fontSize: 12 }}>Downloaded — callable</span>
                  ) : (
                    <span style={{ color: "var(--text-faint)", fontSize: 12 }}>Not fetched — train from it</span>
                  )}
                </td>
              </tr>
            ))}
            {models.length === 0 && (
              <tr><td colSpan={5}><div className="empty-hint">No base models match this search or filter.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className="dataset-card-grid">
      {models.map((b) => (
        <div className="dataset-card" key={b.id} onClick={() => onOpen(b.id)}>
          <div className="dataset-card-head">
            <div className={"dataset-card-icon " + (b.domain === "llm" ? "icon-bg-amber" : "icon-bg-teal")}>
              <Icon name="grid" />
            </div>
            <div>
              <div className="dataset-card-title">{b.title}</div>
              <div className="dataset-card-owner">{b.task}</div>
            </div>
          </div>
          <div className="dataset-card-tags">
            {domainBadge(b.domain, b.framework)}
            <span className="badge badge-outline">Base library</span>
          </div>
          <div className="dataset-card-stats">
            <div className="dataset-card-stat"><div className="n">{b.framework || "—"}</div><div className="l">Framework</div></div>
            <div className="dataset-card-stat"><div className="n">{b.gpu ? "Yes" : "No"}</div><div className="l">GPU</div></div>
            {/* The design originally labelled this column "Deployed: Yes" for every
                entry. That would be untrue for most of the library — an oci:// or
                sklearn:// reference names a trainer image or estimator class, with no
                pretrained artifact behind it — so it reports what is actually
                recorded: whether real weights have been fetched into storage. */}
            <div className="dataset-card-stat">
              <div className="n" style={{ color: b.stored_weights_uri ? "var(--teal-700)" : "var(--text-faint)" }}>
                {b.stored_weights_uri ? "Yes" : "No"}
              </div>
              <div className="l">Weights</div>
            </div>
          </div>
        </div>
      ))}
      {models.length === 0 && (
        <div className="empty-hint" style={{ gridColumn: "1/-1" }}>No base models match this search or filter.</div>
      )}
    </div>
  );
}

/* ---------------- trained / deployed results ---------------- */

function CatalogModelResults({
  models, viewMode, onOpen, onEdit,
}: { models: CatalogModel[]; viewMode: "list" | "card"; onOpen: (m: CatalogModel) => void; onEdit?: (m: CatalogModel) => void }) {
  // In the list the button sits in an overflow-clipped table, so its tooltip opens upward.
  const editButton = (m: CatalogModel, tip?: "up") => onEdit && (
    <div className="dataset-hover-actions" onClick={(e) => e.stopPropagation()}>
      <IconAction icon="pencil" label={`Edit description of ${m.id}`} tip={tip} align="end" onClick={() => onEdit(m)} />
    </div>
  );
  // Archived versions are omitted (they say nothing about the model's current
  // state), and an in-flight run adds a live "training" pill beside them — the
  // one piece of status here that isn't an alias.
  const aliases = (m: CatalogModel) => (
    <>
      {m.versions.filter((v) => v.alias !== "archived").map((v) => (
        <span className={`alias alias-${v.alias}`} key={v.v}>{v.alias}</span>
      ))}
      {m.training && (
        <span className={"status-pill " + (m.trainingStatus === "queued" ? "status-queued" : "status-running")}
          title={`A training run is ${m.trainingStatus} for this model`}>
          <span className="status-dot" /> {m.trainingStatus === "queued" ? "queued" : "training"}
        </span>
      )}
    </>
  );

  if (viewMode === "list") {
    return (
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: "26%" }}>Model</th>
              <th style={{ width: "24%" }}>Type</th>
              <th style={{ width: "14%" }}>Task</th>
              <th style={{ width: "22%" }}>Aliases</th>
              <th style={{ width: "14%" }}>Updated</th>
              <th style={{ width: 48 }} />
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr key={m.id} className="dataset-row" onClick={() => onOpen(m)}>
                <td>
                  <div className="cell-name">{m.id}</div>
                  <div className="cell-sub">
                    {m.versions.length} version{m.versions.length === 1 ? "" : "s"}
                    {m.base_ref ? ` · base: ${m.base_ref}` : ""}
                  </div>
                  {m.description && <div className="cell-sub model-desc-line" title={m.description}>{m.description}</div>}
                </td>
                <td><div className="badge-row">{domainBadge(m.domain, m.framework)}{kindBadge(m.kind)}</div></td>
                <td><span className="tag">{m.task}</span></td>
                <td><div className="alias-row">{aliases(m)}</div></td>
                <td style={{ color: "var(--text-faint)", fontSize: 12 }}>{m.updated}</td>
                <td>{editButton(m, "up")}</td>
              </tr>
            ))}
            {models.length === 0 && (
              <tr><td colSpan={6}><div className="empty-hint">No models match this search or filter.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className="dataset-card-grid">
      {models.map((m) => (
        <div className="dataset-card" key={m.id} onClick={() => onOpen(m)}>
          {editButton(m)}
          <div className="dataset-card-head">
            <div className={"dataset-card-icon " + (m.domain === "llm" ? "icon-bg-amber" : "icon-bg-teal")}>
              <Icon name="grid" />
            </div>
            <div>
              <div className="dataset-card-title">{m.id}</div>
              <div className="dataset-card-owner">{m.task}{m.base_ref ? ` · base: ${m.base_ref}` : ""}</div>
            </div>
          </div>
          {m.description && <div className="dataset-card-desc" title={m.description}>{m.description}</div>}
          <div className="dataset-card-tags">{domainBadge(m.domain, m.framework)}{kindBadge(m.kind)}</div>
          <div className="dataset-card-stats" style={{ alignItems: "center" }}>
            <div className="dataset-card-stat">
              <div className="n">{m.versions.length}</div>
              <div className="l">Versions</div>
            </div>
            <div style={{ flex: 1, display: "flex", gap: 5, flexWrap: "wrap", justifyContent: "flex-end" }}>
              {aliases(m)}
            </div>
          </div>
        </div>
      ))}
      {models.length === 0 && (
        <div className="empty-hint" style={{ gridColumn: "1/-1" }}>No models match this search or filter.</div>
      )}
    </div>
  );
}

/* ---------------- model detail drawer ---------------- */

function ModelDrawer({
  model, onClose, onTest, onEdit,
}: { model: CatalogModel; onClose: () => void; onTest: () => void; onEdit?: () => void }) {
  const navigate = useNavigate();
  const endpoints = useApi(api.getEndpoints, []);
  const endpoint = (endpoints.data ?? []).find((e) => e.modelId === model.id);

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>{model.id}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>ml.acme-corp.{model.id}</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <div className="drawer-section">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <div className="drawer-section-title" style={{ marginBottom: 0 }}>Description</div>
              {onEdit && <IconAction icon="pencil" label={`Edit description of ${model.id}`} align="end" onClick={onEdit} />}
            </div>
            {model.description
              ? <div style={{ fontSize: 13, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>{model.description}</div>
              : <div className="empty-hint" style={{ padding: 0, textAlign: "left" }}>No description yet.</div>}
          </div>
          <div className="drawer-section">
            <div className="drawer-section-title">Properties</div>
            <div className="badge-row" style={{ marginBottom: 10 }}>{domainBadge(model.domain, model.framework)}{kindBadge(model.kind)}</div>
            <div className="kv-row"><span className="k">Task</span><span className="v">{model.task}</span></div>
            <div className="kv-row"><span className="k">Framework</span><span className="v">{model.framework}</span></div>
            <div className="kv-row"><span className="k">Base model ref</span><span className="v">{model.base_ref || "—"}</span></div>
          </div>

          {model.kind === "adapter" && (
            <div className="drawer-section">
              <div className="callout callout-amber">
                <strong>Adapter model.</strong> Served via vLLM multi-LoRA against a shared base-model
                deployment — no dedicated GPU deployment for this version alone.
              </div>
            </div>
          )}

          <div className="drawer-section">
            <div className="drawer-section-title">Version history</div>
            {model.versions.length ? (
              model.versions.map((v) => (
                <div className="version-item" key={v.v}>
                  <div className="version-num">v{v.v}</div>
                  <div className="version-meta">
                    <div className="vm-top"><span className={`alias alias-${v.alias}`}>{v.alias}</span></div>
                    <div className="vm-sub">{v.note}</div>
                  </div>
                </div>
              ))
            ) : (
              <div className="empty-hint">
                {model.pending
                  ? "No versions yet — this model's first training run is still going. A version is registered only once that run succeeds."
                  : "No versions registered yet."}
              </div>
            )}
          </div>

          {/* Shown only when something is genuinely serving this model. A model
              that has never been promoted has no endpoint, and the section is
              absent rather than showing a URL that would 404. */}
          {endpoint && (
            <div className="drawer-section">
              <div className="drawer-section-title">Endpoint</div>
              <div
                className="input"
                style={{
                  fontFamily: "var(--font-mono)", fontSize: 11.5,
                  background: "var(--surface-sunken)", marginBottom: 10, wordBreak: "break-all",
                }}
              >
                POST {endpoint.url}
              </div>
              <button className="btn btn-secondary btn-block" onClick={() => { onClose(); navigate("/endpoints"); }}>
                <Icon name="server" />
                View endpoint — URL, auth, rate limits
              </button>
            </div>
          )}

          {model.latestRunId && (
            <div className="drawer-section">
              <button
                className="btn btn-secondary btn-block"
                onClick={() => navigate(`/training-runs/${model.latestRunId}`)}
              >
                <Icon name="flask" />
                {model.training ? "View the run in progress" : "View the run that produced this"}
              </button>
            </div>
          )}

          <div className="drawer-section">
            <button className="btn btn-primary btn-block" disabled={!model.versions.length} onClick={onTest}>
              <Icon name="test" />
              {model.versions.length ? "Test this model" : "Not testable yet — no version registered"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------- edit description ---------------- */

function EditModelDescriptionDrawer({ model, onClose, onSaved }: {
  model: CatalogModel; onClose: () => void; onSaved: (description: string | null) => void;
}) {
  const [text, setText] = useState(model.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save() {
    setBusy(true); setError(null);
    try {
      const r = await api.updateModelDescription(model.id, text);
      onSaved(r.description);
    } catch (e) { setError(e instanceof Error ? e.message : "Couldn't save the description"); }
    finally { setBusy(false); }
  }
  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>Edit description</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3, fontFamily: "var(--font-mono)" }}>{model.id}</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <div className="field">
            <label>Description</label>
            <textarea className="textarea" style={{ minHeight: 220 }} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="What this model predicts, what it was trained on, how well it does, and how to call it" />
            <div className="hint">{text.length.toLocaleString()} / 4,000 characters</div>
          </div>
          {error && <div className="callout callout-amber">{error}</div>}
        </div>
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={busy || text.trim() === (model.description ?? "").trim()}>
            <Icon name="check" />{busy ? "Saving…" : "Save description"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** The failure's own words, minus the code prefix the trainer puts first
 *  ("TRAINING_EXECUTION_FAILED: …"), which is shown as its own pill. */
function splitRunError(error?: string): { code: string; reason: string } {
  const e = (error || "").trim();
  const m = e.match(/^([A-Z][A-Z0-9_]{3,}):\s*([\s\S]*)$/);
  return m ? { code: m[1], reason: m[2] } : { code: "", reason: e || "No error was recorded for this run." };
}

/** Failed runs in the same two layouts as every other tab: cards, or a table. */
function FailedRunResults({ runs, viewMode, onOpen }:
  { runs: TrainingRun[]; viewMode: "list" | "card"; onOpen: (id: string) => void }) {
  const attempts = (r: TrainingRun) => r.attemptCount ? `${r.attemptCount}${r.maxAttempts ? `/${r.maxAttempts}` : ""}` : "—";
  const pill = (code: string) => code && (
    <span className="status-pill status-failed"><span className="status-dot" /> {code.replace(/_/g, " ").toLowerCase()}</span>
  );
  if (viewMode === "card") {
    return (
      <div className="dataset-card-grid">
        {runs.map((r) => {
          const { code, reason } = splitRunError(r.error);
          return (
            <div className="dataset-card" key={r.id} onClick={() => onOpen(r.id)}
              title="Open this run — stages, log and how to reproduce it">
              <div className="dataset-card-head">
                <div className="dataset-card-icon icon-bg-teal"><Icon name="grid" /></div>
                <div style={{ minWidth: 0 }}>
                  <div className="dataset-card-title">{r.modelName}</div>
                  <div className="dataset-card-owner">base: {r.baseModelId} · dataset: {r.datasetName}</div>
                </div>
              </div>
              <div className="dataset-card-tags">{pill(code)}</div>
              <div className="failed-reason" title={reason} style={{ marginBottom: 12 }}>{reason}</div>
              <div className="dataset-card-stats" style={{ alignItems: "center" }}>
                <div className="dataset-card-stat">
                  <div className="n">{attempts(r)}</div>
                  <div className="l">Attempts</div>
                </div>
                <div style={{ flex: 1, textAlign: "right", fontSize: 12, color: "var(--text-faint)" }}>
                  Failed {new Date(r.createdAt).toLocaleString()}
                </div>
              </div>
            </div>
          );
        })}
        {runs.length === 0 && (
          <div className="empty-hint" style={{ gridColumn: "1/-1" }}>No failed training runs match this search.</div>
        )}
      </div>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th style={{ width: "24%" }}>Model</th>
            <th style={{ width: "16%" }}>Dataset</th>
            <th>Why it failed</th>
            <th style={{ width: "15%" }}>Failed</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const { code, reason } = splitRunError(r.error);
            return (
              <tr key={r.id} onClick={() => onOpen(r.id)} style={{ cursor: "pointer" }}
                title="Open this run — stages, log and how to reproduce it">
                <td>
                  <div className="cell-name">{r.modelName}</div>
                  <div className="cell-sub">
                    from {r.baseModelId}
                    {r.attemptCount ? ` · attempt ${attempts(r)}` : ""}
                  </div>
                </td>
                <td><span className="tag">{r.datasetName}</span></td>
                <td>
                  {code && <div style={{ marginBottom: 4 }}>{pill(code)}</div>}
                  <div className="failed-reason" title={reason}>{reason}</div>
                </td>
                <td style={{ color: "var(--text-faint)", fontSize: 12 }}>{new Date(r.createdAt).toLocaleString()}</td>
              </tr>
            );
          })}
          {runs.length === 0 && (
            <tr><td colSpan={4}><div className="empty-hint">No failed training runs match this search.</div></td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
