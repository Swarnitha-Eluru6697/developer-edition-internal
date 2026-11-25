import React, { useState, useEffect, useRef, useMemo } from "react";
import Select from "../components/Select";
import { useApp } from "../context/AppContext";
import { useUiActions } from "../context/UiContext";
import {
  useListTemplatesQuery, useGetConnectionsQuery, useGetQualityPoliciesQuery,
  useGetPipelinesQuery, useGetPipelineQuery, useLazyGetPipelineQuery, useGetRuntimeFlowQuery,
  useCreatePipelineMutation, useUpdatePipelineMutation, useDeletePipelineMutation, usePipelineActionMutation,
  useGetSchemasQuery, useGetTransformsQuery, useCreateTransformMutation, useDeleteTransformMutation, useLazyGetDeployReadinessQuery, useLazyGetTopicNamePreviewQuery, dataConnectApi,
} from "../store/apiSlice";
import { CreatedAt, CutText, PH, StatCards, Pill, Loading, ErrBox, Ring, ProgressBar, Drawer, IconView, IconEdit, IconDel, IconDeploy, IconPause, IconResume, IconRestart, IconMetrics, FieldError } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import FailureDialog from "../components/FailureDialog";
import StillProcessingDialog from "../components/StillProcessingDialog";
import { PIPE_BADGE, ZONES } from "../constants";
import type { ConnectorTemplate, RuntimeFlowConnector, RuntimeFlowSink, Pipeline, PipelineCreate, PipelineUpdate, PipelineActionName } from "../types/api";
import { apiErrorShape, errorMessage, firstFieldError } from "../errors";
import { conflictFromDetails, operationResultSummary, qualityModeOf, type PipelineConflictView, type QualityMode } from "../pipelineAdapters";
import { flowFreshness, readFlowCache, writeFlowCache, type CachedFlow } from "../flowCache";
import FlowFreshnessNote from "../components/FlowFreshnessNote";
import { isParamRequired, missingTransformParams, modesForTemplate, templateCapabilities, transformsForTemplate, type TransformSpec } from "../templateAdapters";
import { availabilityOf, defaultChoice } from "../availability";
import AvailabilityNote from "../components/AvailabilityNote";
import { usePolling, useOnVisible } from "../hooks/usePolling";
import { useUnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { useAppDispatch } from "../store/hooks";
import { pollOperation } from "../store/pollOperation";
import { explainCode, toFailureInfo, toFailureInfoFromOperation } from "../errorCopy";
import { AUTO_SMT_DESCRIPTION, SMT_TYPE_LABEL, smtOriginOf } from "../transformOrigin";
import RefreshButton from "../components/RefreshButton";
import RowMenu from "../components/RowMenu";
import { usePersistedChoice } from "../hooks/usePersistedChoice";
import {
  CustomChainSection, TransformationsSection, chainChoice, emptySelection, selectionFromSteps, stepsFromSelection, stepsWithoutBox, type TransformSelection,
} from "../components/PipelineSmtFields";
import { ConnectorMetricsDrawer } from "../components/ConnectorMetricsPanel";
import { PIPELINE_LIST_POLL_MS, PIPELINE_DETAIL_POLL_MS } from "../pollingPolicy";
import { relativeTime, runtimeStateLabel } from "../runtimeState";
import { poolLabel } from "../connectPools";
import { inputTopics, searchableTopics, topicLabel, topicSummary } from "../pipelineTopics";
import { schemaIntrospectionView } from "../schemaIntrospection";

/** Live, fast-polling detail view of the reconciler's own desired-vs-observed
 * state for one pipeline (app/models/pipeline_runtime_state.py) — never
 * collapses the two into a single "Status: RUNNING" the way the table row's
 * own badge does. Polls independently of the list's own (paused-while-a-
 * modal-is-open) interval, at the "pipeline detail" tier — the whole
 * point of this view is watching a reconciliation actually resolve live.
 * Goes through usePolling like the list does: paused while the tab is hidden,
 * backed off while the backend is failing. */
function PipelineRuntimeDrawer({ pipelineId, onClose }: { pipelineId: string; onClose: () => void }) {
  const [pollingInterval, reportPollResult] = usePolling({ baseMs: PIPELINE_DETAIL_POLL_MS });
  const { data: p, isLoading, error, refetch } = useGetPipelineQuery(pipelineId, { pollingInterval });
  useEffect(() => { reportPollResult(error); }, [error, reportPollResult]);
  useOnVisible(() => { refetch(); });
  // FE-BUG-19: the pipeline's own static identity/config — read-only
  // key/value rows, not a form (disabled or otherwise). Reference data
  // already cached by the create wizard elsewhere on this page; RTK Query
  // dedupes the subscription rather than issuing a second request.
  const { data: conns } = useGetConnectionsQuery();
  const { data: tpls } = useListTemplatesQuery();
  // What the pipeline is wired to, by name (readable with dataconnect:schemas:view / quality:view / transforms:view).
  const { data: schemas } = useGetSchemasQuery();
  const { data: policies } = useGetQualityPoliciesQuery();
  const { data: smts } = useGetTransformsQuery();

  const conn = p && conns ? conns.find((c) => c.id === p.connection_profile_id) : undefined;
  const tpl = p && tpls ? tpls.find((t) => t.id === p.template_id) : undefined;
  const connCfg = conn?.connection_config || {};
  const schema = p?.schema_version_id ? schemas?.find((x) => x.id === p.schema_version_id) : undefined;
  const policy = p?.quality_policy_id ? policies?.find((x) => x.id === p.quality_policy_id) : undefined;
  const smt = p?.transform_policy_id ? smts?.find((x) => x.id === p.transform_policy_id) : undefined;

  return (
    <Drawer title="Pipeline detail" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      {isLoading ? <Loading /> : error || !p ? <ErrBox error={error} /> : (
        <>
          <div className="fsec">Configuration</div>
          <div className="kv"><span className="kvk">Source</span><span className="kvv">{conn ? conn.source_type : "—"}{tpl ? ` · ${tpl.name}` : ""}</span></div>
          {connCfg.hostname != null && (
            <div className="kv"><span className="kvk">Host</span><span className="kvv">{String(connCfg.hostname)}{connCfg.port ? `:${connCfg.port}` : ""}</span></div>
          )}
          {connCfg.database != null && (
            <div className="kv"><span className="kvk">Database</span><span className="kvv">{String(connCfg.database)}</span></div>
          )}
          <div className="kv"><span className="kvk">Connector template</span><span className="kvv">{tpl ? tpl.name : "—"}</span></div>
          <div className="kv"><span className="kvk">Created at</span><span className="kvv"><CreatedAt value={p.created_at} /></span></div>
          <div className="kv"><span className="kvk">Mode</span><span className="kvv">{p.mode.toUpperCase()}</span></div>
          {/* The Connect worker pool this connector runs in: set by the connector, fixed for the pipeline's life. */}
          <div className="kv"><span className="kvk">Worker pool</span><span className="kvv">{poolLabel(p.runtime_pool_id)}</span></div>
          <div className="kv"><span className="kvk">Zone</span><span className="kvv">{p.zone}</span></div>
          <div className="kv">
            <span className="kvk">{topicLabel(p)}</span>
            <span className="kvv" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{(p.direction === "SINK" ? inputTopics(p).join(", ") : p.topic_name) || "—"}</span>
          </div>
          {/* The schema contract, SMT and quality policy this pipeline uses — by name; the id only while the list is still loading or the item is gone. */}
          <div className="kv">
            <span className="kvk">Schema</span>
            <span className="kvv">{!p.schema_version_id ? "—" : schema ? `${schema.domain}.${schema.source_system}.${schema.entity} · ${schema.version}` : "…"}</span>
          </div>
          <div className="kv">
            <span className="kvk">SMT chain</span>
            <span className="kvv">
              {!p.transform_policy_id ? "—" : smt ? <>{smt.name} <Pill cls={smtOriginOf(smt) === "auto" ? "bp" : "bb"}>{SMT_TYPE_LABEL[smtOriginOf(smt)]}</Pill></> : "…"}
            </span>
          </div>
          {p.quality_policy_id && (
            <div className="kv">
              <span className="kvk">Quality Check</span>
              <span className="kvv">{policy ? `${policy.name} · ${qualityModeOf(p.quality_mode) === "ENFORCE" ? "Enforce" : "Observe"}` : "…"}</span>
            </div>
          )}
          {(() => {
            // BUG-10: why auto-introspection did or didn't register a schema — recorded at creation, kept visible here.
            const introspection = schemaIntrospectionView(p.schema_introspection_status, p.schema_introspection_message);
            if (!introspection) return null;
            return (
              <>
                <div className="kv">
                  <span className="kvk">Schema introspection</span>
                  <span className="kvv"><Pill cls={introspection.cls}>{introspection.label}</Pill></span>
                </div>
                {introspection.message && (
                  <div
                    className="ab" role={introspection.isFailure ? "alert" : undefined}
                    style={{ marginTop: 6, color: introspection.isFailure ? "var(--color-error-text)" : undefined }}
                  >
                    {introspection.message}
                  </div>
                )}
              </>
            );
          })()}

          <div className="fsec" style={{ marginTop: 14 }}>Runtime</div>
          {(() => {
            if (!p.desired_state) {
              return <div className="empty">This pipeline has never been deployed — no runtime state yet.</div>;
            }
            const desired = runtimeStateLabel(p.desired_state);
            const observed = runtimeStateLabel(p.observed_state);
            const unreachable = p.observed_state === "unknown";
            // "Synchronizing..." — the reconciler has seen a real (non-unknown)
            // observation, it just doesn't match desired yet (mid-convergence);
            // distinct from "unreachable", where there's no fresh observation
            // to compare against desired at all.
            const reconciling = !unreachable && p.desired_state !== p.observed_state;
            return (
              <>
                <div className="kv"><span className="kvk">Desired</span><span className="kvv">{desired}</span></div>
                <div className="kv"><span className="kvk">Runtime</span><span className="kvv">{observed}</span></div>
                {reconciling ? (
                  <div className="kv"><span className="kvk">State</span><span className="kvv">Synchronizing...</span></div>
                ) : (
                  <div className="kv">
                    <span className="kvk">{unreachable ? "Last observed" : "Last synced"}</span>
                    <span className="kvv">{relativeTime(p.last_observed_at)}</span>
                  </div>
                )}
                {p.runtime_error && (
                  <div className="ab" style={{ marginTop: 10, color: "var(--color-error-text)" }}>{p.runtime_error}</div>
                )}
              </>
            );
          })()}
        </>
      )}
    </Drawer>
  );
}

const STATE_MAP = { RUNNING: "running", PAUSED: "paused", FAILED: "failed", UNASSIGNED: "failed" };
/** The connector template the New pipeline form starts on. */
const DEFAULT_TEMPLATE_ID = "tpl_jdbc_source";
/** The status tabs above the pipeline list. */
type StatusTab = "all" | "running" | "deployed" | "paused" | "failed" | "draft";

const ACTION_LABEL: Record<PipelineActionName, string> = { deploy: "Deploy", pause: "Pause", resume: "Resume", restart: "Restart" };
const ACTION_ING_LABEL: Record<PipelineActionName, string> = { deploy: "Deploying…", pause: "Pausing…", resume: "Resuming…", restart: "Restarting…" };
// FE-BUG-25: the pipeline's own `status` — set atomically by the backend at
// submission time now, not only once a worker gets around to actually
// running the operation (see PipelineService.claim_lifecycle_transition) —
// is what a page refresh reads to rediscover an operation already in
// flight, via ACTION_FOR_TRANSITIONAL_STATUS below.
const ACTION_FOR_TRANSITIONAL_STATUS: Partial<Record<string, PipelineActionName>> = {
  deploying: "deploy", pausing: "pause", resuming: "resume", restarting: "restart",
};
// The stable statuses each action can start from — the backend's
// VALID_TRANSITIONS (pipeline_service.py) read in reverse. Anything else is
// always a 409 there, so the button is disabled with the reason instead of
// letting the click fail (e.g. Deploy on a paused pipeline).
const ACTION_ALLOWED_FROM: Record<PipelineActionName, string[]> = {
  deploy: ["draft", "failed", "unknown"],
  pause: ["deployed", "running", "degraded"],
  resume: ["paused"],
  restart: ["deployed", "running", "paused", "degraded", "failed", "unknown"],
};
const ACTION_BLOCKED_HINT: Record<PipelineActionName, string> = {
  deploy: "already deployed — use Resume or Restart",
  pause: "only a deployed or running pipeline can be paused",
  resume: "only a paused pipeline can be resumed",
  restart: "deploy it first",
};
const KNOWN_STABLE_STATUSES = new Set(Object.values(ACTION_ALLOWED_FROM).flat());
/** Why `action` can't start from `status`, or null when it can. A status
 *  this table doesn't know (or a transitional one, which `busy` already
 *  covers) is left to the backend. */
function actionBlockedReason(status: string | null | undefined, action: PipelineActionName): string | null {
  if (!status || !KNOWN_STABLE_STATUSES.has(status)) return null;
  if (ACTION_ALLOWED_FROM[action].includes(status)) return null;
  return `${ACTION_LABEL[action]} unavailable: pipeline is ${status} — ${ACTION_BLOCKED_HINT[action]}`;
}

function rowInfo(p: Pipeline, connByName: Record<string, RuntimeFlowConnector>, topicMsgs: Record<string, number>, sinkByConn: Record<string, RuntimeFlowSink>) {
  const conn = connByName[p.connector_name];
  const live = conn ? (STATE_MAP[conn.state as keyof typeof STATE_MAP] || "deployed") : p.status;
  let records = "—";
  let pct: number;
  if (conn && conn.type === "sink") {
    const sk: Pick<RuntimeFlowSink, "consumed" | "lag"> = sinkByConn[p.connector_name] || { consumed: 0, lag: 0 };
    records = String(sk.consumed ?? 0);
    const total = (sk.consumed || 0) + (sk.lag || 0);
    pct = total ? Math.round(((sk.consumed || 0) / total) * 100) : (live === "running" ? 100 : 0);
  } else if (conn) {
    const msgs = (p.topic_name ? topicMsgs[p.topic_name] : 0) ?? 0;
    records = String(msgs);
    pct = msgs > 0 ? 100 : (live === "running" ? 100 : 0);
  } else {
    pct = ["running", "deployed"].includes(p.status ?? "") ? 100 : 0;
  }
  return { conn, live, records, pct };
}

// FE-BUG-07: the list row's badge is the live Kafka Connect state when the
// connector is in the flow response. When it isn't (Connect unreachable, or
// the connector is gone), the database `status` is only the last mapped
// reconcile outcome — a deployed/running/paused value there must not be
// presented as if it were the actual runtime, so it is shown as "unknown".
// `syncingTo` flags a live connector that hasn't converged on the desired
// state yet (the drawer spells this out as "Synchronizing...").
const DB_STATUSES_THAT_CLAIM_A_RUNTIME = ["running", "deployed", "paused"];
const CONVERGEABLE = ["running", "paused"];
function rowRuntimeHonesty(p: Pipeline, hasLiveConnector: boolean, live: string): { unverified: boolean; syncingTo: string | null } {
  if (!hasLiveConnector) {
    return { unverified: DB_STATUSES_THAT_CLAIM_A_RUNTIME.includes(p.status ?? ""), syncingTo: null };
  }
  const desired = p.desired_state ?? "";
  const syncing = CONVERGEABLE.includes(desired) && CONVERGEABLE.includes(live) && desired !== live;
  return { unverified: false, syncingTo: syncing ? desired : null };
}

// ── Create wizard ─────────────────────────────────────────────────────────────
/** One transformation in the wizard: whether it is ticked, and the text typed for each of its params. */

function PipelineForm({ onClose }: { onClose: () => void }) {
  const { ok, toast, err, discardAndClose } = useApp();
  const { data: tpls, isLoading: tplsLoading, error: tplsError } = useListTemplatesQuery();
  const { data: conns, isLoading: connsLoading, error: connsError } = useGetConnectionsQuery();
  const { data: quals } = useGetQualityPoliciesQuery();
  const { data: smts } = useGetTransformsQuery();
  const { data: existingPipelines } = useGetPipelinesQuery();
  const [createTransform] = useCreateTransformMutation();
  const [createPipeline] = useCreatePipelineMutation();
  const [triggerTopicPreview] = useLazyGetTopicNamePreviewQuery();
  // FE-BUG-08: the real topic name, from the backend's own naming formula
  // (app/services/topic_naming.py) — never reconstructed client-side, so
  // this can't drift from what deploy actually names the topic (BUG-01).
  // `message` is the backend's own explanation when it rejected the identity (e.g. a "-" in a field, which is the separator) — shown as-is.
  const [topicPreview, setTopicPreview] = useState<{ text: string; error: boolean; message: string }>({ text: "", error: false, message: "" });

  // FE-BUG-13: this form spans source/objects/topic/mode/SMTs/schema/quality/
  // runtime-policy fields (everything below) — losing any of it to an
  // accidental close would mean redoing real work. `dirty` is set ONLY by
  // the user's own edits (see markDirty and every onChange below), never by
  // the reference-data auto-defaulting effects further down, so opening the
  // drawer never itself counts as "dirty".
  const [dirty, setDirty] = useState(false);
  const markDirty = () => setDirty(true);
  useUnsavedChangesGuard(dirty, "You have unsaved pipeline changes.");

  const [tplId, setTplId] = useState("");
  const [connId, setConnId] = useState("");
  const [qId, setQId] = useState("");
  const [qMode, setQMode] = useState<QualityMode>("OBSERVE");
  const [tf, setTf] = useState<Record<string, TransformSelection>>({});
  // A Custom SMT (a policy made with "+ New SMT" on the Transforms page) used as this pipeline's transformations — instead of the ticked boxes above it.
  const [customId, setCustomId] = useState("");
  const [initload, setInitload] = useState("");
  const [tables, setTables] = useState("");
  const [sinkTopic, setSinkTopic] = useState("");
  const [f, setF] = useState({ name: "ingest-accounts", zone: "raw", mode: "api", ver: "v1", dom: "crm", src: "salesforce", ent: "accounts" });
  // FE-BUG-21: the backend's own 422 field_errors (ErrorEnvelope.field_errors
  // — app/schemas/errors.py) already say which field was invalid; the only
  // thing missing was a place on this side to keep that and an input to
  // point aria-invalid/aria-describedby at, instead of the generic err()
  // toast being the only signal.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);
  const on = (k: keyof typeof f) => (e: { target: { value: string } }) => {
    markDirty(); const v = e.target.value; setF((prev) => ({ ...prev, [k]: v }));   // from the latest state: a template switch may have just corrected mode in the same instant
    setFieldError((fe) => (fe?.key === k ? null : fe));
  };

  // FE-BUG-08: debounced so every keystroke in domain/source/entity doesn't
  // fire its own request — settles 300ms after the identity fields stop
  // changing, same as any other type-then-fetch preview.
  useEffect(() => {
    if (!f.dom.trim() || !f.src.trim() || !f.ent.trim()) { setTopicPreview({ text: "", error: false, message: "" }); return; }
    const identity = { zone: f.zone, domain: f.dom, source_system: f.src, entity: f.ent, mode: f.mode, version: f.ver };
    const timer = setTimeout(() => {
      triggerTopicPreview(identity).unwrap()
        .then((r) => setTopicPreview({ text: r.topic_name, error: false, message: "" }))
        .catch((e: unknown) => {
          // A 4xx means the backend understood the request and refused these field values, and says why ("'-' is the separator
          // between the parts of a topic name… use '_' instead") — that is what the user needs. Anything else (network, 5xx) stays generic.
          const failure = apiErrorShape(e);
          const message = failure && failure.status >= 400 && failure.status < 500 ? failure.message : "";
          setTopicPreview({ text: "", error: true, message });
        });
    }, 300);
    return () => clearTimeout(timer);
  }, [f.zone, f.dom, f.src, f.ent, f.mode, f.ver, triggerTopicPreview]);

  // Error handling policy (GAP-04) — how a bad record is handled once
  // deployed: FAIL_FAST fails the connector immediately, DLQ_AND_CONTINUE
  // (the previous, only behavior) logs it and keeps going, THRESHOLD does
  // the same but auto-pauses the pipeline if too many pile up in a window.
  const [errPolicy, setErrPolicy] = useState<ErrPolicy>("DLQ_AND_CONTINUE");
  const policyChosenRef = useRef(false); // true once the user picks a policy themselves
  const [errMaxErrors, setErrMaxErrors] = useState("100");
  const [errWindowSeconds, setErrWindowSeconds] = useState("300");

  // Default the pickers once reference data lands.
  useEffect(() => {
    // Start on JDBC Source, the most general-purpose connector. If a deployment does not have that template, start on a connector the backend
    // says can run, when there is one. Every connector stays selectable: another environment may have plugins this one does not, and deploy itself checks.
    if (tpls && tpls.length && !tplId) setTplId(tpls.find((t) => t.id === DEFAULT_TEMPLATE_ID)?.id ?? defaultChoice(tpls, (t) => availabilityOf(t.availability)) ?? tpls[0]!.id);
  }, [tpls, tplId]);
  useEffect(() => {
    if (conns && conns[0] && !connId) setConnId(conns[0].id);
  }, [conns, connId]);

  const tpl = tpls ? tpls.find((t) => t.id === tplId) : undefined;

  // Reset transform picker + initial-load default whenever the template changes.
  useEffect(() => {
    if (!tpl) return;
    setTf(emptySelection(tpl));          // supported_transforms is validated into typed specs by templateAdapters.ts (the dynamic-JSON boundary)
    // A Custom SMT cannot follow the user to a connector that takes no transformations at all.
    if (!templateCapabilities(tpl).supportsSMT) setCustomId("");
    setInitload(snapDefault(tpl));
    // FE-BUG-23: f.mode/errPolicy can hold a value the newly-selected
    // template doesn't support (e.g. switching from a CDC source to a file
    // source while "cdc" is still selected) — the mode/policy <Select>s
    // below only ever render options this template allows, so a stale,
    // now-hidden value would otherwise still be silently submitted.
    const allowedModes = modesForTemplate(tpl);
    setF((prev) => (allowedModes.includes(prev.mode) ? prev : { ...prev, mode: allowedModes[0] || prev.mode }));
    const allowedPolicies = errPolicyOptionsForTemplate(tpl).map(([v]) => v);
    // A choice the user made is kept while the new connector still allows it; one they never touched follows the
    // connector's own default (a sink -> the dead-letter queue, a source -> skip and log).
    setErrPolicy((prev) => (policyChosenRef.current && allowedPolicies.includes(prev) ? prev : (allowedPolicies[0] || "FAIL_FAST")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tplId]);

  const loading = tplsLoading || connsLoading;
  const loadErr = tplsError || connsError;

  if (loadErr) return <Drawer title="New pipeline" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><div className="ab">{errorMessage(loadErr)}</div></Drawer>;
  if (loading) return <Drawer title="New pipeline" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><div className="empty"><span className="spin" /> loading templates &amp; connections…</div></Drawer>;

  // Alphabetical, not the API's own (roughly by-category) order — a long
  // flat list of source/sink templates is much faster to scan by name.
  // NEW-09: what can run first, what cannot after it (each alphabetical), so a long list of unavailable connectors never buries the usable ones.
  const tplList = [...(tpls || [])].sort((a, b) => a.name.localeCompare(b.name));          // every connector, alphabetically — none is disabled or reordered
  const tplAvailability = availabilityOf(tpl?.availability);
  const connList = conns || [];
  if (!tplList.length || !connList.length) {
    return (
      <Drawer title="New pipeline" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
        <div className="alert alert-w"><div className="at">Prerequisites missing</div><div className="ab">You need at least one connector template and one connection first.</div></div>
      </Drawer>
    );
  }

  const klass = (tpl && tpl.connector_class) || "";
  const isDbz = /debezium/i.test(klass);
  const isJdbcSrc = klass === "io.confluent.connect.jdbc.JdbcSourceConnector";
  const isMongoSrc = klass === "com.mongodb.kafka.connect.MongoSourceConnector";
  const isDbSource = isDbz || isJdbcSrc || isMongoSrc;
  const isSource = tpl && tpl.connector_type === "source";
  const isSink = tpl && tpl.connector_type === "sink";
  const snap = snapConfig(tpl);

  const setChecked = (type: string, checked: boolean) => { markDirty(); setTf((s) => ({ ...s, [type]: { params: s[type]?.params ?? {}, checked } })); };
  const setParam = (type: string, key: string, val: string) => {
    markDirty();
    setTf((s) => ({ ...s, [type]: { checked: s[type]?.checked ?? false, params: { ...s[type]?.params, [key]: val } } }));
    setFieldError((fe) => (fe?.key.startsWith(`tf:${type}:`) ? null : fe));
  };

  const { chosenSmt, chosenUnsupported } = chainChoice(tpl, smts, customId);

  const submit = async () => {
    if (chosenUnsupported.length) {                       // the connector was changed after picking: the pick no longer fits it
      toast(`${chosenSmt?.name}: ${tpl?.name ?? "this connector"} doesn't support ${chosenUnsupported.join(", ")}.`, "err");
      document.getElementById("pipe-custom-smt")?.focus();
      return;
    }
    // A ticked transform with a required setting left blank would deploy "successfully" and then fail when its task starts.
    for (const spec of transformsForTemplate(tpl)) {
      const sel = tf[spec.type];
      const missing = sel?.checked ? missingTransformParams(spec, sel.params) : [];
      const first = missing[0];
      if (first) {
        const message = `${spec.type}: ${missing.map((p) => p.label).join(", ")} ${missing.length > 1 ? "are" : "is"} required.`;
        setFieldError({ key: `tf:${spec.type}:${first.key}`, message });
        toast(message, "err");
        document.getElementById(`pipe-tf-${spec.type}-${first.key}`)?.focus();
        return;
      }
    }
    try {
      const cc: Record<string, unknown> = { "tasks.max": "1" };
      if (isSink && sinkTopic.trim()) cc.topics = sinkTopic.trim();
      if (isDbSource && tables.trim()) {
        const v = tables.trim();
        cc.tables = v === "*" ? ["*"] : v.split(",").map((s) => s.trim()).filter(Boolean);
      }
      if (snap && initload) cc[snap.cckey] = initload;

      const body: PipelineCreate = {
        name: f.name, template_id: tplId, connection_profile_id: connId,
        zone: f.zone, domain: f.dom, source_system: f.src, entity: f.ent,
        mode: f.mode, version: f.ver, connector_config: cc, status: "draft",
        error_handling_policy: errPolicy,
      };
      if (qId) {
        body.quality_policy_id = qId;
        body.quality_mode = qMode;
      }
      if (errPolicy === "THRESHOLD") {
        body.error_threshold_max_errors = Number(errMaxErrors) || 100;
        body.error_threshold_window_seconds = Number(errWindowSeconds) || 300;
      }

      const transforms = stepsFromSelection(tpl, tf);
      if (customId) {
        body.transform_policy_id = customId;               // an existing Custom SMT chain: shared as it is, no per-pipeline copy
      } else if (transforms.length) {
        const pol = await createTransform({ name: `${body.name}-transforms`, description: AUTO_SMT_DESCRIPTION, origin: "auto", transforms }).unwrap();
        body.transform_policy_id = pol.id;
      }

      const p = await createPipeline(body).unwrap();
      // A successful, intentional save — close unconditionally rather than
      // through the guarded `onClose`, which would otherwise immediately
      // re-prompt "Unsaved changes" for the very save that just happened.
      discardAndClose();
      ok(`Pipeline created → ${topicSummary(p) || p.name}${p.schema_version_id ? " · schema inferred (see Schemas)" : ""}`);
      // BUG-10: the backend now records exactly why auto-introspection didn't
      // link a schema (skipped — source unreachable/empty — or an outright
      // failure like a connection timeout) instead of leaving the caller to
      // guess via a second, separate connection probe.
      // A connector that can't discover schemas is an expected skip, already explained on the form
      // above — not an error worth a red toast.
      const expectedSkip = p.schema_introspection_status === "skipped" && !caps.schemaDiscovery;
      if (!expectedSkip && (p.schema_introspection_status === "failed" || p.schema_introspection_status === "skipped")) {
        const label = p.schema_introspection_status === "failed" ? "FAILED" : "skipped";
        toast(`Schema introspection ${label}${p.schema_introspection_message ? ` — ${p.schema_introspection_message}` : ""}`, "err");
      }
    } catch (e: unknown) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ key: fe.field, message: fe.message });
      err(e);
    }
  };

  const caps = templateCapabilities(tpl);


  return (
    <Drawer title="New pipeline" onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" onClick={submit}>Create</button></>}>
      <div className="fsec">References</div>
      <div className="fg">
        <div className="fld"><label htmlFor="pipe-template">Connector template (source)</label><Select id="pipe-template" value={tplId} aria-describedby={tplAvailability && tplAvailability.state !== "supported" ? "pipe-template-availability" : undefined}
          onChange={(e) => { markDirty(); setTplId(e.target.value); }}>
          {tplList.map((t) => {
            const availability = availabilityOf(t.availability);
            return <option key={t.id} value={t.id} data-availability={availability?.state}>{t.name}</option>;
          })}
        </Select></div>
        <div className="fld"><label htmlFor="pipe-connection">Connection profile</label><Select id="pipe-connection" value={connId} onChange={(e) => { markDirty(); setConnId(e.target.value); }}>{connList.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.source_type})</option>)}</Select></div>
      </div>

      <AvailabilityNote id="pipe-template-availability" availability={tplAvailability} advisory />

      <TransformationsSection tpl={tpl} tf={tf} customChosen={!!customId} setChecked={setChecked} setParam={setParam} fieldError={fieldError} />

      <CustomChainSection tpl={tpl} smts={smts} pipelines={existingPipelines} tf={tf} customId={customId} setCustomId={(id) => { markDirty(); setCustomId(id); }} />

      {!caps.schemaDiscovery && (
        <div className="ab" style={{ marginBottom: 12 }}>
          This connector doesn't support schema discovery, so no schema is detected from the source when the pipeline is
          created. Register one under Schemas if you need schema checks.
        </div>
      )}

      <div className="fsec">Data quality</div>
      <div className="fld" style={{ marginBottom: 6 }}><label htmlFor="pipe-quality">Quality Check (optional)</label>
        <Select id="pipe-quality" value={qId} onChange={(e) => { markDirty(); setQId(e.target.value); }}><option value="">— none —</option>{(quals || []).map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}</Select>
      </div>
      <QualityModeField id="pipe-quality-mode" hasPolicy={!!qId} mode={qMode} isSink={!!isSink} onChange={(m) => { markDirty(); setQMode(m); }} />
      <div className="ab" style={{ marginBottom: 12 }}>If set, the quality check runs automatically on a live sample of the source when the pipeline is created — results show in the Quality tab.</div>

      <div className="fsec">Error handling</div>
      <div className="fld" style={{ marginBottom: 6 }}><label htmlFor="pipe-err-policy">On a bad record</label>
        <Select id="pipe-err-policy" value={errPolicy} onChange={(e) => { markDirty(); policyChosenRef.current = true; setErrPolicy(errPolicyOf(e.target.value) ?? "FAIL_FAST"); }}>
          {errPolicyOptionsForTemplate(tpl).map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </Select>
      </div>
      {tpl && !templateCapabilities(tpl).supportsDLQ && (
        <div className="ab" style={{ marginBottom: 6 }}>
          {tpl.connector_type === "sink"
            ? "This connector template doesn't support dead-letter-queue routing — choose \"Fail immediately\" or \"Skip bad records & log them\"."
            : "Kafka Connect only provides a dead-letter queue for sink connectors, so a source can't keep a bad record anywhere — it either stops on it (\"Fail immediately\") or skips and logs it."}
        </div>
      )}
      {errPolicy === "THRESHOLD" && (
        <div className="fg fg3" style={{ marginBottom: 6 }}>
          <div className="fld"><label htmlFor="pipe-err-max">Max errors</label><input id="pipe-err-max" type="number" min={1} value={errMaxErrors} onChange={(e) => { markDirty(); setErrMaxErrors(e.target.value); }} /></div>
          <div className="fld"><label htmlFor="pipe-err-window">Window (seconds)</label><input id="pipe-err-window" type="number" min={1} value={errWindowSeconds} onChange={(e) => { markDirty(); setErrWindowSeconds(e.target.value); }} /></div>
        </div>
      )}
      <div className="ab" style={{ marginBottom: 12 }}>
        {errPolicy === "FAIL_FAST"
          ? "The connector stops on the very first bad record — right for an integration where a bad record means something is fundamentally wrong."
          : errPolicy === "SKIP_AND_LOG"
          ? "A bad record is skipped and written to the connector's log; it is not kept anywhere else, so it can't be replayed. The connector keeps running."
          : errPolicy === "THRESHOLD"
          ? "Individual bad records don't stop the connector, but the pipeline pauses itself if more than the configured count land on its dead-letter queue within the window."
          : "Individual bad records are logged and routed to this pipeline's own dead-letter queue; the connector keeps running."}
      </div>

      {snap && (
        <div style={{ marginBottom: 12 }}>
          <div className="fsec">Initial load / snapshot</div>
          <div className="fld"><label htmlFor="pipe-initload">{snap.label}</label>
            <Select id="pipe-initload" value={initload} onChange={(e) => { markDirty(); setInitload(e.target.value); }}>{snap.opts.map((o) => <option key={o[0]} value={o[0]}>{o[1]}</option>)}</Select>
          </div>
          <div className="ab" style={{ marginTop: 4 }}>{snap.note}</div>
        </div>
      )}

      {isDbSource ? (
        <div style={{ marginBottom: 12 }}>
          <div className="fsec">{isMongoSrc ? "Collections" : "Tables"} to ingest</div>
          <div className="fld"><label htmlFor="pipe-tables">{isMongoSrc ? "Collections" : "Tables"} (comma-separated, or * for all)</label>
            <input id="pipe-tables" value={tables} placeholder="e.g. employees, orders_info   —   or   *" onChange={(e) => { markDirty(); setTables(e.target.value); }} /></div>
          <div className="ab" style={{ marginTop: 4 }}>Leave blank to ingest just the <b>Entity</b> below. Enter several {isMongoSrc ? "collection" : "table"} names, or <b>*</b> to ingest every {isMongoSrc ? "collection" : "table"} in the database.</div>
        </div>
      ) : isSource ? (
        <div className="ab" style={{ marginTop: 4, marginBottom: 12 }}>{sourceNote(klass)}</div>
      ) : null}

      {isSink && (
        <div style={{ marginBottom: 12 }}>
          <div className="fsec">Sink input</div>
          <div className="fld"><label htmlFor="pipe-sink-topic">Source topic to read *</label><input id="pipe-sink-topic" value={sinkTopic} placeholder="e.g. raw-sales-postgres-employees-batch-v1" onChange={(e) => { markDirty(); setSinkTopic(e.target.value); }} /></div>
          <div className="ab" style={{ marginTop: 4 }}>Set <b>Entity</b> below to the destination table to write into (e.g. employees_copy).</div>
        </div>
      )}

      <div className="fsec">Topic identity (lowercase a-z 0-9 _ — no &quot;-&quot;, it joins the parts of the topic name)</div>
      <div className="fld" style={{ marginBottom: 12 }}>
        <label htmlFor="pipe-name">Name</label>
        <input
          id="pipe-name" value={f.name} onChange={on("name")}
          aria-invalid={fieldError?.key === "name"} aria-describedby={fieldError?.key === "name" ? "pipe-name-error" : undefined}
        />
        {fieldError?.key === "name" && <FieldError id="pipe-name-error" message={fieldError.message} />}
      </div>
      <div className="fg fg3">
        <div className="fld"><label htmlFor="pipe-zone">Zone</label><Select id="pipe-zone" value={f.zone} onChange={on("zone")}>{ZONES.map((z) => <option key={z}>{z}</option>)}</Select></div>
        <div className="fld"><label htmlFor="pipe-mode">Mode</label><Select id="pipe-mode" value={f.mode} onChange={on("mode")}>{modesForTemplate(tpl).map((z) => <option key={z}>{z}</option>)}</Select></div>
        <div className="fld"><label htmlFor="pipe-ver">Version</label><input id="pipe-ver" value={f.ver} onChange={on("ver")} /></div>
      </div>
      <div className="fg fg3">
        <div className="fld"><label htmlFor="pipe-domain">Domain</label><input id="pipe-domain" value={f.dom} onChange={on("dom")} /></div>
        <div className="fld"><label htmlFor="pipe-source">Source system</label><input id="pipe-source" value={f.src} onChange={on("src")} /></div>
        <div className="fld"><label htmlFor="pipe-entity">Entity</label><input id="pipe-entity" value={f.ent} onChange={on("ent")} /></div>
      </div>
      {isSink && (
        // A sink generates no topic: say so, rather than previewing a name nothing will ever be written to.
        <div className="ab" style={{ fontSize: 11 }}>This sink reads the topic you enter above and creates no topic of its own.</div>
      )}
      {topicPreview.text && !isSink && (
        <div className="ab" style={{ fontFamily: "var(--font-mono)", fontSize: 11, wordBreak: "break-all" }}>
          Topic: {topicPreview.text}
        </div>
      )}
      {topicPreview.error && <div className="ab" role="status" data-testid="topic-preview-error">{topicPreview.message || "Couldn't preview the topic name — check the fields above."}</div>}
    </Drawer>
  );
}

// ── Edit + FE-BUG-14: optimistic-concurrency conflict handling ─────────────────
// Editable spec fields kept intentionally smaller than the create wizard's:
// identity (zone/domain/.../entity/mode/version) is immutable once created
// (see PipelineUpdate's own docstring), and connector_config/transforms have
// create-specific plumbing (auto-creating a fresh TransformPolicy on every
// submit) that isn't a correct "edit in place" operation — retargeting those
// on an existing pipeline is future scope, not something this ticket needs.
type ErrPolicy = "FAIL_FAST" | "SKIP_AND_LOG" | "DLQ_AND_CONTINUE" | "THRESHOLD";
const ERR_POLICIES: readonly ErrPolicy[] = ["FAIL_FAST", "SKIP_AND_LOG", "DLQ_AND_CONTINUE", "THRESHOLD"];
/** `value` as a policy if it is one of the four the backend accepts (a stored value from an older build, or a <Select>'s text, may not be). */
function errPolicyOf(value: unknown): ErrPolicy | undefined {
  return ERR_POLICIES.find((policy) => policy === value);
}
const ERR_POLICY_OPTIONS: Array<[ErrPolicy, string]> = [
  ["DLQ_AND_CONTINUE", "Log & continue (route to dead-letter queue)"],
  ["FAIL_FAST", "Fail immediately"],
  ["THRESHOLD", "Log & continue, but auto-pause if too many pile up"],
  ["SKIP_AND_LOG", "Skip bad records & log them (no dead-letter queue)"],
];
// Kafka Connect gives a dead-letter queue only to SINK connectors, so the two policies built on one are
// sink-only (the backend enforces the same rule — app/core/error_policies.py).
const DLQ_ONLY_POLICIES: ErrPolicy[] = ["DLQ_AND_CONTINUE", "THRESHOLD"];

function errPolicyLabel(v: string | null | undefined): string {
  return ERR_POLICY_OPTIONS.find((o) => o[0] === v)?.[1] || String(v ?? "");
}

/** Observe vs. Enforce for a pipeline's quality policy (NEW-01). Enforce needs a policy, so with none selected the choice is disabled and reads
 * Observe. For a source, shows the prepared topic — what downstream sinks must read — once the pipeline already enforces. A SINK has no
 * prepared topic to read: enforcing, it writes only valid records to its target and sends the failing ones to quarantine (the hand-off
 * between its check and its connector is internal and never shown). */
function QualityModeField({ id, hasPolicy, mode, preparedTopic, isSink = false, onChange }: {
  id: string; hasPolicy: boolean; mode: QualityMode; preparedTopic?: string | null; isSink?: boolean; onChange: (mode: QualityMode) => void;
}) {
  const effective: QualityMode = hasPolicy ? mode : "OBSERVE";
  return (
    <>
      <div className="fld" style={{ marginBottom: 6 }}><label htmlFor={id}>How the check is applied</label>
        <Select id={id} value={effective} disabled={!hasPolicy} onChange={(e) => onChange(qualityModeOf(e.target.value))}>
          <option value="OBSERVE">{isSink ? "Observe — write every record, count failures and copy them to quarantine" : "Observe — count failures and copy them to quarantine"}</option>
          <option value="ENFORCE">{isSink ? "Enforce — write only valid records; failing ones go to quarantine" : "Enforce — only valid records go to the prepared topic"}</option>
        </Select>
      </div>
      <div className="ab" id={`${id}-help`} style={{ marginBottom: 12 }}>
        {!hasPolicy
          ? "Choose a quality check to enforce it."
          : isSink
            ? effective === "ENFORCE"
              ? "Records that pass the check are written to the target; failing records go to quarantine only and never reach it."
              : "Every record is written to the target, including the ones that fail. Choose Enforce to write only valid records."
            : effective === "ENFORCE"
              ? <>Failing records go to quarantine only. Downstream sinks must read this pipeline&apos;s prepared topic{preparedTopic ? <> (<code>{preparedTopic}</code>)</> : null}, not its data topic.</>
              : "The data topic still carries every record, including the ones that fail. Choose Enforce when consumers must only ever see valid data."}
      </div>
    </>
  );
}

// The fields this form can diff between what user A opened and what's
// live now — everything PipelineEditForm actually lets someone change.
function conflictDiffRows(existing: Pipeline, latest: PipelineConflictView): Array<[string, string, string]> {
  const rows: Array<[string, string, string]> = [];
  const add = (label: string, a: string, b: string) => { if (a !== b) rows.push([label, a, b]); };
  add("Name", existing.name, latest.name);
  add("Quality Check", existing.quality_policy_id || "(none)", latest.quality_policy_id || "(none)");
  add("Quality mode", qualityModeOf(existing.quality_mode), qualityModeOf(latest.quality_mode));
  add("Error handling", errPolicyLabel(existing.error_handling_policy), errPolicyLabel(latest.error_handling_policy));
  if (existing.error_handling_policy === "THRESHOLD" || latest.error_handling_policy === "THRESHOLD") {
    add("Max errors", String(existing.error_threshold_max_errors ?? "—"), String(latest.error_threshold_max_errors ?? "—"));
    add("Window (s)", String(existing.error_threshold_window_seconds ?? "—"), String(latest.error_threshold_window_seconds ?? "—"));
  }
  return rows;
}

function PipelineEditForm({ existing, onClose }: { existing: Pipeline; onClose: () => void }) {
  const { ok, toast, err, discardAndClose } = useApp();
  const { data: quals } = useGetQualityPoliciesQuery();
  // FE-BUG-23: mode/zone/etc. are immutable once created (see this
  // component's own docstring below), but error_handling_policy isn't — a
  // user could still switch an existing pipeline INTO a DLQ policy its
  // template doesn't support, so the same template lookup PipelineForm uses
  // to gate its own DLQ options is needed here too.
  const { data: tpls } = useListTemplatesQuery();
  const tpl = tpls ? tpls.find((t) => t.id === existing.template_id) : undefined;
  const [updatePipeline] = useUpdatePipelineMutation();

  const [dirty, setDirty] = useState(false);
  const markDirty = () => setDirty(true);
  useUnsavedChangesGuard(dirty, "You have unsaved pipeline changes.");

  const [name, setName] = useState(existing.name);
  const [qId, setQId] = useState(existing.quality_policy_id || "");
  const [qMode, setQMode] = useState<QualityMode>(qualityModeOf(existing.quality_mode));
  const [errPolicy, setErrPolicy] = useState<ErrPolicy>(
    errPolicyOf(existing.error_handling_policy) ?? "SKIP_AND_LOG",
  );
  const [errMaxErrors, setErrMaxErrors] = useState(String(existing.error_threshold_max_errors ?? "100"));
  const [errWindowSeconds, setErrWindowSeconds] = useState(String(existing.error_threshold_window_seconds ?? "300"));

  // The pipeline's transformations and initial load, as the New pipeline form shows them. What the pipeline has now is loaded into the boxes /
  // the chain choice / the load mode once it is known; nothing is sent for them unless the user changes one of them.
  const { data: smts } = useGetTransformsQuery();
  const { data: allPipelines } = useGetPipelinesQuery();
  const [createTransform] = useCreateTransformMutation();
  const [deleteTransform] = useDeleteTransformMutation();
  const snap = snapConfig(tpl);
  const [tf, setTf] = useState<Record<string, TransformSelection>>({});
  const [customId, setCustomId] = useState("");
  const [smtTouched, setSmtTouched] = useState(false);
  const [initload, setInitload] = useState("");
  const [initTouched, setInitTouched] = useState(false);
  const loadedRef = useRef(false);
  const currentChain = existing.transform_policy_id ? (smts ?? []).find((c) => c.id === existing.transform_policy_id) : undefined;
  useEffect(() => {
    if (loadedRef.current || !tpl) return;
    if (existing.transform_policy_id && !smts) return;                        // wait for the chains: the pipeline's own is one of them
    loadedRef.current = true;
    if (currentChain && smtOriginOf(currentChain) === "custom") { setCustomId(currentChain.id); setTf(emptySelection(tpl)); }
    else setTf(currentChain ? selectionFromSteps(tpl, currentChain.transforms ?? []) : emptySelection(tpl));
    const stored = (existing.connector_config ?? {}) as Record<string, unknown>;
    const raw = snap ? stored[snap.cckey] : undefined;
    const cfg = typeof raw === "string" && snap && "aliases" in snap ? (snap.aliases?.[raw] ?? raw) : raw;   // a saved "no_data" shows as what it deploys as
    setInitload(typeof cfg === "string" && snap?.opts.some((o) => o[0] === cfg) ? cfg : snapDefault(tpl));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tpl, smts]);
  const orphanedSteps = currentChain && smtOriginOf(currentChain) === "auto" ? stepsWithoutBox(tpl, currentChain.transforms ?? []) : [];
  const { chosenSmt, chosenUnsupported } = chainChoice(tpl, smts, customId);
  const touchSmt = () => { markDirty(); setSmtTouched(true); };
  const setChecked = (type: string, checked: boolean) => { touchSmt(); setTf((st) => ({ ...st, [type]: { params: st[type]?.params ?? {}, checked } })); };
  const setParam = (type: string, key: string, val: string) => {
    touchSmt();
    setTf((st) => ({ ...st, [type]: { checked: st[type]?.checked ?? false, params: { ...st[type]?.params, [key]: val } } }));
    setFieldError((fe) => (fe?.key.startsWith(`tf:${type}:`) ? null : fe));
  };

  // FE-BUG-23: the persisted policy could predate the template being
  // curated with supportsDLQ: false — correct it once the template loads,
  // the same way PipelineForm corrects a stale mode/policy on template
  // switch, rather than leaving the <Select> showing a hidden option.
  useEffect(() => {
    if (!tpl) return;
    const allowed = errPolicyOptionsForTemplate(tpl).map(([v]) => v);
    setErrPolicy((prev) => (allowed.includes(prev) ? prev : (allowed[0] || "FAIL_FAST")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tpl?.id]);

  // FE-BUG-14: set only on a 409 PIPELINE_VERSION_CONFLICT — the backend's
  // own latest row state, handed back inside the error itself (see
  // PipelineVersionConflictError's details.current) so showing "what
  // changed" never needs a second round trip.
  const [conflict, setConflict] = useState<PipelineConflictView | null>(null);
  const [comparing, setComparing] = useState(false);
  // FE-BUG-21: see PipelineForm's own copy of this pattern above.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);

  const submit = async () => {
    if (smtTouched && !customId) {                           // a ticked transformation with a required setting left blank would deploy and then fail
      for (const spec of transformsForTemplate(tpl)) {
        const sel = tf[spec.type];
        const missing = sel?.checked ? missingTransformParams(spec, sel.params) : [];
        const first = missing[0];
        if (first) {
          const message = `${spec.type}: ${missing.map((p) => p.label).join(", ")} ${missing.length > 1 ? "are" : "is"} required.`;
          setFieldError({ key: `tf:${spec.type}:${first.key}`, message });
          toast(message, "err");
          document.getElementById(`pipe-tf-${spec.type}-${first.key}`)?.focus();
          return;
        }
      }
    }
    if (smtTouched && chosenUnsupported.length) {
      toast(`${chosenSmt?.name}: ${tpl?.name ?? "this connector"} doesn't support ${chosenUnsupported.join(", ")}.`, "err");
      document.getElementById("pipe-custom-smt")?.focus();
      return;
    }
    let madeChain: string | undefined;
    try {
      const body: PipelineUpdate = {
        name, quality_policy_id: qId || null, quality_mode: qId ? qMode : "OBSERVE",   // ENFORCE needs a policy; none selected means OBSERVE
        error_handling_policy: errPolicy, expected_revision: existing.revision,
      };
      if (errPolicy === "THRESHOLD") {
        body.error_threshold_max_errors = Number(errMaxErrors) || 100;
        body.error_threshold_window_seconds = Number(errWindowSeconds) || 300;
      }
      if (smtTouched) {
        // Another Custom chain is just linked. Ticked boxes become a fresh Standard chain of this pipeline's own; nothing ticked removes the link.
        const steps = stepsFromSelection(tpl, tf);
        if (customId) body.transform_policy_id = customId;
        else if (steps.length) {
          const made = await createTransform({ name: `${name}-transforms`, description: AUTO_SMT_DESCRIPTION, origin: "auto", transforms: steps }).unwrap();
          madeChain = made.id;
          body.transform_policy_id = made.id;
        } else body.transform_policy_id = null;
      }
      if (initTouched && snap) body.connector_config = { ...(existing.connector_config ?? {}), [snap.cckey]: initload };
      await updatePipeline({ id: existing.id, body }).unwrap();
      // The Standard chain this pipeline had is its own copy: once it is no longer linked and nothing else uses it, it goes.
      if (smtTouched && currentChain && smtOriginOf(currentChain) === "auto" && body.transform_policy_id !== currentChain.id
          && !(allPipelines ?? []).some((p) => p.id !== existing.id && p.transform_policy_id === currentChain.id)) {
        await deleteTransform(currentChain.id).unwrap().catch(() => undefined);
      }
      discardAndClose();
      ok(`Pipeline "${name}" updated`);
    } catch (e: unknown) {
      if (madeChain) await deleteTransform(madeChain).unwrap().catch(() => undefined);   // the pipeline was not saved: don't leave its new chain behind
      const failure = apiErrorShape(e);
      const current = failure ? conflictFromDetails(failure.details?.current) : null;
      if (failure?.status === 409 && failure.code === "PIPELINE_VERSION_CONFLICT" && current) {
        // The draft can no longer be saved as-is — nothing left in it is
        // "unsaved and at risk" in the FE-BUG-13 sense, so stop arming that
        // guard here rather than layering its generic prompt on top of this
        // conflict-specific one.
        setDirty(false);
        setConflict(current);
      } else {
        const fe = firstFieldError(e);
        if (fe) setFieldError({ key: fe.field, message: fe.message });
        err(e);
      }
    }
  };

  // Someone else's save landed first — never silently overwritten (see
  // update_pipeline's expected_revision check). "Review latest" and
  // "Compare changes" both end the same way (discard this draft, close, and
  // require the user to reopen Edit and reapply anything they still want —
  // the ticket's own stated minimum bar); Compare just shows what changed
  // first.
  if (conflict) {
    return (
      <Drawer title="Unsaved changes" onClose={discardAndClose}
        footer={<><button className="btn-s" onClick={discardAndClose}>Review latest</button>{!comparing && <button className="btn-p" onClick={() => setComparing(true)}>Compare changes</button>}</>}>
        <div className="alert alert-w">
          <div className="at">This pipeline changed since you opened it.</div>
          <div className="ab">Someone else saved changes to &quot;{existing.name}&quot; while you were editing. Your changes were not applied.</div>
        </div>
        {comparing && (
          <div style={{ marginTop: 12 }}>
            <div className="fsec">What changed</div>
            {(() => {
              const rows = conflictDiffRows(existing, conflict);
              return rows.length ? (
                <div role="table" style={{ "--cols": "0.8fr 1.1fr 1.1fr" } as React.CSSProperties}>
                  <div className="thead" role="row"><div className="th" role="columnheader">Field</div><div className="th" role="columnheader">You opened</div><div className="th" role="columnheader">Latest</div></div>
                  <div className="tbody" role="rowgroup">
                    {rows.map(([label, a, b]) => (
                      <div className="tr" role="row" key={label}>
                        <div className="td" role="cell">{label}</div>
                        <div className="td" role="cell">{a}</div>
                        <div className="td" role="cell">{b}</div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : <div className="ab">No visible field differs — the conflicting edit may have touched something this form doesn&apos;t show.</div>;
            })()}
          </div>
        )}
      </Drawer>
    );
  }

  return (
    <Drawer title={`Edit pipeline · ${existing.name}`} onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" onClick={submit}>Save</button></>}>
      <div className="fld" style={{ marginBottom: 12 }}>
        <label htmlFor="pipe-edit-name">Name</label>
        <input
          id="pipe-edit-name" value={name}
          onChange={(e) => { markDirty(); setName(e.target.value); setFieldError((fe) => (fe?.key === "name" ? null : fe)); }}
          aria-invalid={fieldError?.key === "name"} aria-describedby={fieldError?.key === "name" ? "pipe-edit-name-error" : undefined}
        />
        {fieldError?.key === "name" && <FieldError id="pipe-edit-name-error" message={fieldError.message} />}
      </div>
      <TransformationsSection tpl={tpl} tf={tf} customChosen={!!customId} setChecked={setChecked} setParam={setParam} fieldError={fieldError} />
      {orphanedSteps.length > 0 && (
        <div className="ab" role="status" style={{ marginBottom: 8 }}>
          This pipeline's chain also has {orphanedSteps.join(", ")}, which this connector has no box for here. Changing the transformations replaces the chain with the ticked ones.
        </div>
      )}

      <CustomChainSection
        tpl={tpl} smts={smts} pipelines={allPipelines} tf={tf} customId={customId} selfId={existing.id}
        setCustomId={(id) => { touchSmt(); setCustomId(id); }}
      />
      {existing.status !== "draft" && (
        <div className="ab" style={{ marginBottom: 12 }}>
          This pipeline is already deployed: a change to its transformations or initial load is applied to its connector, which may restart it.
        </div>
      )}

      <div className="fsec">Data quality</div>
      <div className="fld" style={{ marginBottom: 12 }}><label htmlFor="pipe-edit-quality">Quality Check (optional)</label>
        <Select id="pipe-edit-quality" value={qId} onChange={(e) => { markDirty(); setQId(e.target.value); }}>
          <option value="">— none —</option>{(quals || []).map((q) => <option key={q.id} value={q.id}>{q.name}</option>)}
        </Select>
      </div>
      <QualityModeField id="pipe-edit-quality-mode" hasPolicy={!!qId} mode={qMode} preparedTopic={existing.prepared_topic ?? null}
        isSink={existing.direction === "SINK"}
        onChange={(m) => { markDirty(); setQMode(m); }} />

      <div className="fsec">Error handling</div>
      <div className="fld" style={{ marginBottom: 6 }}><label htmlFor="pipe-edit-err-policy">On a bad record</label>
        <Select id="pipe-edit-err-policy" value={errPolicy} onChange={(e) => { markDirty(); setErrPolicy(errPolicyOf(e.target.value) ?? "FAIL_FAST"); }}>
          {errPolicyOptionsForTemplate(tpl).map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </Select>
      </div>
      {tpl && !templateCapabilities(tpl).supportsDLQ && (
        <div className="ab" style={{ marginBottom: 6 }}>
          {tpl.connector_type === "sink"
            ? "This connector template doesn't support dead-letter-queue routing — choose \"Fail immediately\" or \"Skip bad records & log them\"."
            : "Kafka Connect only provides a dead-letter queue for sink connectors, so a source can't keep a bad record anywhere — it either stops on it (\"Fail immediately\") or skips and logs it."}
        </div>
      )}
      {errPolicy === "THRESHOLD" && (
        <div className="fg fg3" style={{ marginBottom: 6 }}>
          <div className="fld"><label htmlFor="pipe-edit-err-max">Max errors</label><input id="pipe-edit-err-max" type="number" min={1} value={errMaxErrors} onChange={(e) => { markDirty(); setErrMaxErrors(e.target.value); }} /></div>
          <div className="fld"><label htmlFor="pipe-edit-err-window">Window (seconds)</label><input id="pipe-edit-err-window" type="number" min={1} value={errWindowSeconds} onChange={(e) => { markDirty(); setErrWindowSeconds(e.target.value); }} /></div>
        </div>
      )}
      {snap && (
        <div style={{ marginBottom: 12 }}>
          <div className="fsec">Initial load / snapshot</div>
          <div className="fld"><label htmlFor="pipe-initload">{snap.label}</label>
            <Select id="pipe-initload" value={initload} onChange={(e) => { markDirty(); setInitTouched(true); setInitload(e.target.value); }}>{snap.opts.map((o) => <option key={o[0]} value={o[0]}>{o[1]}</option>)}</Select>
          </div>
          <div className="ab" style={{ marginTop: 4 }}>{snap.note}</div>
        </div>
      )}
    </Drawer>
  );
}

// FE-BUG-15: deleting a never-deployed draft and deleting a pipeline with a
// live (or once-live) Kafka Connect connector are different operations with
// different consequences — a generic "Are you sure?" is honest about
// neither. `status !== "draft"` is the same test delete_pipeline itself
// uses (app/services/pipeline_service.py) to decide whether there's a real
// connector to reconcile away: any status past "draft" (deployed, running,
// paused, restarting, even failed) means a connector was created and still
// needs Kafka Connect cleanup, not just a row delete.
function DeletePipelineDialog({ pipeline, onConfirm, onClose }: { pipeline: Pipeline; onConfirm: () => void; onClose: () => void }) {
  const deployed = pipeline.status !== "draft";
  return (
    <Drawer title={`Delete "${pipeline.name}"?`} onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" style={{ background: "var(--color-error-text)" }} onClick={onConfirm}>Delete pipeline</button></>}>
      {deployed ? (
        <>
          <div style={{ fontSize: 13, marginBottom: 6 }}>This will:</div>
          <ul style={{ margin: "0 0 14px 20px", padding: 0, fontSize: 13, lineHeight: 1.8, color: "var(--color-text-secondary)" }}>
            <li>stop the Kafka Connect connector</li>
            <li>remove its DataConnect pipeline definition</li>
            <li>preserve existing Kafka topic data</li>
          </ul>
          {pipeline.topic_name && (
            <div className="fld"><label>Topic</label>
              <div style={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>{pipeline.topic_name}</div>
            </div>
          )}
        </>
      ) : (
        <div className="ab">
          This draft has never been deployed — there is no Kafka Connect connector or topic to affect. Deleting it removes only its DataConnect pipeline definition.
        </div>
      )}
    </Drawer>
  );
}

// FE-BUG-24: "Ready to deploy" review screen — calls the backend's own
// GET .../deploy-readiness (PipelineService.check_deploy_readiness) rather
// than re-deriving any of these rules in React, per the ticket's explicit
// instruction. Every check the backend reports is rendered exactly as it
// comes back — this component has no rule of its own about what "ready"
// means beyond `result.ready`, which is itself computed backend-side.
function DeployReadinessDialog({ pipeline, onConfirm, onClose }: { pipeline: Pipeline; onConfirm: () => void; onClose: () => void }) {
  const [trigger, { data, isFetching, error }] = useLazyGetDeployReadinessQuery();

  useEffect(() => {
    trigger(pipeline.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipeline.id]);

  return (
    <Drawer title="Ready to deploy" onClose={onClose}
      footer={<>
        <button className="btn-s" onClick={onClose}>Back</button>
        {/* After fixing whatever failed (a connection, a policy, a name clash), re-run the same
            backend checks right here instead of closing and reopening this screen. preferCacheValue
            is false so it always asks the backend again. */}
        <button className="btn-s" onClick={() => trigger(pipeline.id, false)} disabled={isFetching}>
          {isFetching ? "Checking…" : "Re-check"}
        </button>
        <button
          className="btn-p" onClick={onConfirm} disabled={isFetching || !data || data.ready === false}
          title={data && data.ready === false ? "Resolve the failed checks above before deploying" : undefined}
        >
          Deploy
        </button>
      </>}>
      {isFetching && <Loading />}
      {error ? <ErrBox error={error} /> : data && (
        <>
          {data.ready === false && (
            <div className="alert alert-w">
              <div className="at">Not ready to deploy</div>
              <div className="ab">One or more checks below failed — fix them, then choose Re-check.</div>
            </div>
          )}
          <div className="fsec">Checks</div>
          <ul className="rdy-list">
            {data.checks.map((c) => (
              <li className="rdy-row" key={c.key}>
                <span className={`rdy-icon ${c.ok ? "ok" : "fail"}`} aria-hidden="true">{c.ok ? "✓" : "✗"}</span>
                <span className="rdy-label">
                  <span>{c.ok ? "" : "Not ready: "}{c.label}</span>
                  {!c.ok && c.reason && <div className="rdy-reason">{c.reason}</div>}
                </span>
              </li>
            ))}
          </ul>
          {data.topic_name && (
            <div className="fld" style={{ marginTop: 4 }}>
              <label>Topic</label>
              <div style={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>{data.topic_name}</div>
            </div>
          )}
        </>
      )}
    </Drawer>
  );
}

// Snapshot / initial-load control per source connector class.
// `note` is authored as real JSX (structured React content), not an HTML
// string — there is nothing here for dangerouslySetInnerHTML to parse, so a
// future switch to template/backend-controlled copy can't turn this into an
// XSS vector the way an HTML-string field could.
// FE-BUG-23: the Designer must not assume every connector supports every
// pipeline mode — MODES (constants.ts) is the full, template-agnostic set;
// tpl.supported_modes (populated per-template server-side, see
// app/db/seed_templates.py) is the one this template actually supports.
// Empty means unrestricted/undeclared (same convention the backend's own
// create-time and deploy-time checks use — see
// app/services/pipeline_spec_validator.py), not "supports nothing".

// FE-BUG-23: DLQ_AND_CONTINUE/THRESHOLD both route bad records to a
// connector-level dead-letter queue — offering them for a template whose
// capabilities.supportsDLQ is false would let a user pick an option the
// backend now rejects at creation time (pipeline_spec_validator.py) with no
// warning until they submit. Fail immediately never depends on DLQ support.
function errPolicyOptionsForTemplate(tpl: ConnectorTemplate | undefined): typeof ERR_POLICY_OPTIONS {
  // First entry = the default a fresh form selects (and what a stale choice is corrected to), so a source —
  // which has no dead-letter queue — defaults to "Skip & log", a sink to the dead-letter queue.
  if (!templateCapabilities(tpl).supportsDLQ) {
    return [...ERR_POLICY_OPTIONS.filter(([v]) => !DLQ_ONLY_POLICIES.includes(v))].sort(
      (a, b) => Number(b[0] === "SKIP_AND_LOG") - Number(a[0] === "SKIP_AND_LOG"),
    );
  }
  return ERR_POLICY_OPTIONS;
}

// What each installed Debezium connector (2.5.4) accepts as snapshot.mode — checked against Kafka Connect's own config validator on
// 2026-10-28 (MySQL from the Debezium 2.5 docs: its plugin is installed on demand). The form used to offer one list for all of them, and
// e.g. PostgreSQL + "no_data" deployed only to be rejected by Connect. Same lists as the backend's SNAPSHOT_MODES_BY_CLASS (connect_config.py).
export const DEBEZIUM_SNAPSHOT_MODES: Record<string, string[]> = {
  "io.debezium.connector.postgresql.PostgresConnector": ["initial", "initial_only", "always", "never"],
  "io.debezium.connector.mysql.MySqlConnector": ["initial", "initial_only", "when_needed", "never", "schema_only", "schema_only_recovery"],
  "io.debezium.connector.sqlserver.SqlServerConnector": ["initial", "initial_only", "schema_only"],
  "io.debezium.connector.oracle.OracleConnector": ["initial", "initial_only", "always", "schema_only", "schema_only_recovery"],
  "io.debezium.connector.mongodb.MongoDbConnector": ["initial", "never"],
};
const SNAPSHOT_MODE_LABEL: Record<string, string> = {
  initial: "initial — load existing rows, then stream changes (default)",
  initial_only: "initial_only — load existing rows, then stop",
  always: "always — reload all rows on every start, then stream changes",
  never: "never — stream new changes only",
  schema_only: "schema_only — stream new changes only",
  schema_only_recovery: "schema_only_recovery — rebuild lost schema history, then stream changes",
  when_needed: "when_needed — load rows only if the stored position is missing",
};

function snapConfig(tpl: ConnectorTemplate | undefined) {
  const klass = tpl?.connector_class ?? "";
  // FE-BUG-23: belt-and-suspenders — the regex matches below already only
  // fire for known CDC/JDBC/Mongo source classes, but a template explicitly
  // curated with capabilities.snapshot: false (e.g. a future CDC-shaped
  // connector that's log-only, no initial snapshot) must still suppress
  // this section rather than let the connector_class match alone decide.
  if (!templateCapabilities(tpl).snapshot) return null;
  const modes = DEBEZIUM_SNAPSHOT_MODES[klass];
  if (modes) {
    return {
      cckey: "snapshot_mode", label: "Snapshot mode",
      opts: modes.map((x) => [x, SNAPSHOT_MODE_LABEL[x] ?? x]),
      // "no_data" (Debezium 2.6's name) was offered before and may be saved on a pipeline: it deploys as this connector's stream-only mode.
      aliases: { no_data: modes.includes("never") ? "never" : "schema_only" } as Record<string, string>,
      note: <>Debezium (CDC): <b>initial</b> loads the existing rows, then streams changes; <b>{modes.includes("never") ? "never" : "schema_only"}</b> streams new changes only.</>,
    };
  }
  if (/debezium/i.test(klass)) {
    // A Debezium-based connector whose accepted modes were not checked against the installed plugin (e.g. YugabyteDB): left as it was.
    const m = ["initial", "always", "initial_only", "no_data", "when_needed", "configuration_based", "custom"];
    return {
      cckey: "snapshot_mode", label: "Snapshot mode",
      opts: m.map((x) => [x, x + (x === "initial" ? " (default)" : "")]),
      note: <>Debezium (CDC): <b>initial</b> snapshots then streams changes; <b>no_data</b> streams changes only.</>,
    };
  }
  if (klass === "io.confluent.connect.jdbc.JdbcSourceConnector") {
    return {
      cckey: "load_mode", label: "Load mode",
      opts: [["auto", "auto (default)"], ["bulk", "bulk — full snapshot every poll"], ["incrementing", "incrementing — only new rows"]],
      note: <>JDBC: <b>bulk</b> re-reads the whole table each poll; <b>incrementing</b> reads only rows with a new primary key.</>,
    };
  }
  if (klass === "com.mongodb.kafka.connect.MongoSourceConnector") {
    return {
      cckey: "startup_mode", label: "Startup mode",
      opts: [["copy_existing", "copy_existing — snapshot then stream (default)"], ["latest", "latest — stream new changes only"]],
      note: <>MongoDB: <b>copy_existing</b> snapshots the collection then streams; <b>latest</b> streams new changes only.</>,
    };
  }
  return null;
}
function snapDefault(tpl: ConnectorTemplate | undefined) {
  const s = snapConfig(tpl);
  return s ? (s.opts[0]?.[0] ?? "") : "";
}
function sourceNote(klass: string): React.ReactNode {
  if (/s3|gcs|azure|blob/i.test(klass)) return <>This source reads objects from the <b>bucket</b> configured on the connection profile — no tables to select.</>;
  if (/sftp|ftp|file/i.test(klass)) return <>This source reads files from the <b>path</b> configured on the connection profile — no tables to select.</>;
  if (/http|webhook|rest/i.test(klass)) return <>This source reads from the <b>endpoint / URL</b> configured on the connection profile — no tables to select.</>;
  return "Ingest for this source is driven by its connection profile — no tables to select.";
}

// ── Pipelines page ────────────────────────────────────────────────────────────
export default function Pipelines({ go, focus, clearFocus }: { go?: (id: "mon") => void } & PageFocusProps) {
  const { openModal, closeModal, modal, ok, toast, err, can } = useApp();
  const { selectConnector } = useUiActions();
  const canManage = can("dataconnect:pipelines:edit");
  const canDelete = can("dataconnect:pipelines:edit");
  const canDeploy = can("dataconnect:pipelines:deploy");
  const canControl = can("dataconnect:pipelines:operate");
  const [q, setQ] = useState("");
  const [statusTab, setStatusTab] = usePersistedChoice<StatusTab>("dc_pipelines_tab", ["all", "running", "deployed", "paused", "failed", "draft"], "all");   // survives a refresh
  // A different tab is a different list: show it from its first row.
  useEffect(() => { document.querySelector("#pipelines-table .tbody")?.scrollTo({ top: 0 }); }, [statusTab]);
  // A pipeline picked in the top bar's search: narrow the list to it.
  useEffect(() => {
    if (focus?.kind !== "pipeline") return;
    setQ(focus.name);
    clearFocus?.();
  }, [focus, clearFocus]);

  // The list refreshes itself every PIPELINE_LIST_POLL_MS — always, there is no switch for it; the refresh icon beside it reloads the page at once
  // (and the 20 seconds start again from that load). Poll only while no drawer is open (mirrors the old
  // setInterval that skipped ticks while a modal was up) — every consumer
  // of these two queries across the app shares this one live subscription.
  // usePolling() centralizes the rest: paused while the tab is hidden,
  // backed off (with jitter) while the backend is failing, reset the moment
  // it recovers — see src/hooks/usePolling.ts. PIPELINE_LIST_POLL_MS (see
  // ../pollingPolicy) is this page's own tier of the cadence matrix — a
  // calmer overview than Monitoring's, which polls faster on purpose.
  const [pollingInterval, reportPollResult] = usePolling({ enabled: !modal, baseMs: PIPELINE_LIST_POLL_MS });
  const { data: pipes, isLoading: pipesLoading, error, refetch: refetchPipelines } = useGetPipelinesQuery(undefined, { pollingInterval });
  const { data: flowData, error: flowError, refetch: refetchFlow, fulfilledTimeStamp: flowObservedAt } = useGetRuntimeFlowQuery(undefined, { pollingInterval });
  // A browser reload wipes the RTK cache, and /runtime/flow takes a few seconds to answer — without a fallback the summary cards and per-row
  // counts drop to 0/— until it does. The last good snapshot is shown meanwhile, but only as a clearly labelled stand-in (FE-02): it carries
  // the time it was observed, belongs to one backend + organisation, expires, and the banner below says it is not live.
  const { cfg } = useApp();
  const flowScope = useMemo(() => ({ baseUrl: cfg.baseUrl, orgId: cfg.orgId }), [cfg.baseUrl, cfg.orgId]);
  useEffect(() => {
    // Only a real, reachable answer is a "good" snapshot; an unreachable-runtime reply or a failed refresh must not overwrite it.
    if (!flowData || flowError || flowObservedAt === undefined || flowData.reachable === false) return;
    try { writeFlowCache(localStorage, flowScope, flowData, flowObservedAt); } catch { /* storage unavailable */ }
  }, [flowData, flowError, flowObservedAt, flowScope]);
  const cachedFlow = useMemo<CachedFlow | undefined>(() => {
    try { return readFlowCache(localStorage, flowScope); } catch { return undefined; }
  }, [flowScope]);
  const flow = flowData || cachedFlow?.flow || { connectors: [], topics: [], sinks: [] };
  const freshness = flowFreshness({ live: flowData, liveObservedAt: flowObservedAt, failed: Boolean(flowError), cached: cachedFlow });

  // Only one of the two queries' outcomes needs to drive backoff — they poll
  // in lockstep (same interval, same enabled/visible gate), so tracking both
  // separately would just double-count the same underlying network health.
  useEffect(() => { reportPollResult(error ?? flowError); }, [error, flowError, reportPollResult]);
  useOnVisible(() => { refetchPipelines(); refetchFlow(); });

  // FE-BUG-25: keyed by pipeline id -> the action currently in flight for
  // it — disables that row's Deploy/Pause/Resume/Restart buttons and swaps
  // the busy one's label to "<Action>ing…" the instant a click fires (see
  // `act` below) or this page discovers an already-in-flight operation on
  // load/refresh (the effect below), never only once the network response
  // for that click comes back — a double-click before the first response
  // lands must never submit a second request.
  const [busy, setBusy] = useState<Record<string, PipelineActionName | "delete">>({});
  // Operation ids this tab is already watching — a ref, not state, since it
  // only ever guards against starting a duplicate watch loop for the same
  // operation and never itself needs to trigger a render.
  const watchingRef = useRef<Set<string>>(new Set());

  const dispatch = useAppDispatch();
  const [deletePipeline] = useDeletePipelineMutation();
  const [pipelineAction] = usePipelineActionMutation();
  const [fetchPipeline] = useLazyGetPipelineQuery();

  // FE-BUG-14: re-fetches rather than reusing the (possibly several-seconds-
  // stale, polled) list-cache row, so the revision the edit form opens with
  // is as fresh as possible — minimizing false-positive conflicts against a
  // change that already happened before this click.
  const onEditPipeline = async (id: string) => {
    try {
      const fresh = await fetchPipeline(id).unwrap();
      openModal(<PipelineEditForm existing={fresh} onClose={closeModal} />);
    } catch (e) { err(e); }
  };

  // FE-BUG-25: "refreshing the page should rediscover the active
  // operation" — a pipeline whose status is already transitional carries
  // its own active_operation_id (set atomically at submission time, see
  // the backend's claim_lifecycle_transition), so resume watching it
  // exactly the way a fresh click would, instead of leaving its buttons
  // clickable while something is already running. Runs on every pipes
  // refetch (not just mount), so it also picks up an operation another tab
  // — or another user — just started against the same pipeline.
  useEffect(() => {
    if (!pipes) return;
    for (const p of pipes) {
      const action = ACTION_FOR_TRANSITIONAL_STATUS[p.status as string];
      if (action && p.active_operation_id && !watchingRef.current.has(p.active_operation_id)) {
        watchOperation(action, p.id, p.active_operation_id);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipes]);

  if (error) return <ErrBox error={error} />;
  if (pipesLoading || !pipes) return <Loading />;

  const connByName: Record<string, RuntimeFlowConnector> = {}; (flow.connectors || []).forEach((c) => { connByName[c.name] = c; });
  const topicMsgs: Record<string, number> = {}; (flow.topics || []).forEach((t) => { topicMsgs[t.topic] = t.messages; });
  const sinkByConn: Record<string, RuntimeFlowSink> = {}; (flow.sinks || []).forEach((sk) => { sinkByConn[sk.connector] = sk; });

  const count = (st: string) => pipes.filter((p) => p.status === st).length;

  // The status tabs under the card's title: All, or only the pipelines in one status (the same status the counts are of).
  let list = statusTab === "all" ? pipes.slice() : pipes.filter((p) => p.status === statusTab);
  if (q) { const s = q.toLowerCase(); list = list.filter((p) => (p.name || "").toLowerCase().includes(s) || searchableTopics(p).includes(s) || (p.mode || "").toLowerCase().includes(s)); }
  // No paging: every pipeline renders in one list and only the rows scroll
  // (see .card-fill / .tbl-fill in styles.css), so the header stays put.
  const rows = list;

  // FE-BUG-04: a deploy/pause/resume/restart failure is exactly the
  // scenario DataConnect's own infrastructure-specific error codes exist
  // for (Kafka Connect rejected the config, the worker was unreachable, a
  // topic policy couldn't be applied, ...) — worth a structured dialog
  // naming the pipeline and the actual backend reason, not a bare toast.
  const showActionFailure = (action: PipelineActionName, pipelineId: string, info: ReturnType<typeof toFailureInfo>) => {
    const pipe = pipes.find((p) => p.id === pipelineId);
    openModal(
      <FailureDialog
        title={`${ACTION_LABEL[action]} failed`}
        {...info}
        context={pipe ? { Pipeline: pipe.name } : undefined}
        onRetry={info.retryable ? () => { closeModal(); act(pipelineId, action); } : undefined}
        onClose={closeModal}
      />,
    );
  };

  // FE-BUG-05: "the browser gave up watching" is not "the operation
  // failed" — pollOperation reports the former as a PollOutcome (never a
  // thrown timeout) precisely so this can render each one honestly: a real
  // terminal failed/stale status gets FailureDialog, giving up watching
  // gets StillProcessingDialog with a way to keep watching the SAME
  // operation, never a resubmission of the action itself.
  // FE-BUG-25: idempotent against being asked to watch the same operation
  // twice (the resume-on-refresh effect above and a fresh click can both
  // reach here for the same operationId) — the second call is a no-op, not
  // a second poll loop. busy[pipelineId] is set for as long as this
  // function is actively watching, cleared in `finally` however it ends
  // (terminal success/failure, or opening StillProcessingDialog) — a stale
  // "busy" row is never stuck forever, and the backend's own claim remains
  // the real guard regardless of whatever this tab's local state drifts to.
  const watchOperation = async (action: PipelineActionName, pipelineId: string, operationId: string) => {
    if (watchingRef.current.has(operationId)) return;
    watchingRef.current.add(operationId);
    setBusy((b) => ({ ...b, [pipelineId]: action }));
    try {
      const outcome = await pollOperation(dispatch, operationId);
      if (outcome.kind === "polling_timeout" || outcome.kind === "network_disconnected") {
        const pipe = pipes.find((p) => p.id === pipelineId);
        openModal(
          <StillProcessingDialog
            action={ACTION_LABEL[action]}
            reason={outcome.kind}
            context={pipe ? { Pipeline: pipe.name } : undefined}
            onContinueMonitoring={() => { closeModal(); watchOperation(action, pipelineId, operationId); }}
            onClose={closeModal}
          />,
        );
        return;
      }
      const operation = outcome.value;
      if (operation.status === "cancelled") {
        toast(`${action} was cancelled`, "err");
        return;
      }
      if (operation.status === "failed" || operation.status === "stale") {
        showActionFailure(action, pipelineId, toFailureInfoFromOperation(operation, `${action} failed`));
        return;
      }
      const result = operationResultSummary(operation.result);
      const topic = result.topic ? ` · topic ${result.topic}` : "";
      ok(`${action} → ${result.status ?? "done"}${topic}`);
      refetchPipelines(); refetchFlow(); // immediate feedback instead of waiting for the next poll tick
    } finally {
      watchingRef.current.delete(operationId);
      setBusy((b) => { const next = { ...b }; delete next[pipelineId]; return next; });
    }
  };

  // deploy/pause/resume/restart wait on a live Kafka Connect round-trip, so
  // the backend returns 202 + a queued Operation instead of the final
  // PipelineStatus (see api.ts's own header comment) — submit, then watch
  // it to the real outcome rather than reporting "queued" as if it were done.
  //
  // FE-BUG-25: busy[id] is set here FIRST, before the submission request
  // even lands — a double-click fires this twice, but the second call sets
  // the same (already-set) busy entry and then hits the backend's own
  // atomic claim (app/services/pipeline_service.py's
  // claim_lifecycle_transition), which is what actually rejects the
  // duplicate; disabling the button below is the fast, no-network-round-
  // trip layer on top of that real guard, not a replacement for it.
  const act = async (id: string, action: PipelineActionName) => {
    setBusy((b) => ({ ...b, [id]: action }));
    try {
      const submitted = await pipelineAction({ id, action }).unwrap();
      await watchOperation(action, id, submitted.id);
    } catch (e) {
      setBusy((b) => { const next = { ...b }; delete next[id]; return next; });
      // The submission call itself failed (e.g. a 409 conflict) — rich
      // dialog only when there's an actual DataConnect-specific explanation
      // to show; a generic/unrecognized failure stays a plain toast rather
      // than a drawer with nothing to add.
      const info = toFailureInfo(e);
      if (explainCode(info.code)) showActionFailure(action, id, info);
      else err(e);
    }
  };
  // Same busy/spinner/disable treatment as deploy/pause/resume/restart —
  // see the comment above those buttons. A deployed pipeline's row stays
  // visible with status "deleting" (set synchronously by the DELETE call
  // itself) until the runtime reconciler confirms the connector is gone and
  // the row is actually removed (see pipeline_service.delete_pipeline's own
  // docstring); this loop is what notices that removal (a 404 on the
  // per-pipeline GET) instead of leaving the row showing "deleting" forever.
  // The list's own polling (useGetPipelinesQuery above) already re-renders
  // the row with its live status on every tick in the meantime, same as any
  // other transitional status.
  //
  // One toast at the end, not one at submission and a second at completion
  // — same "<action> → <final state>" shape watchOperation's toast uses for
  // pause/deploy/resume/restart (e.g. "pause → paused"), not a separate
  // "Deleting..." toast immediately followed by its own "Deleted" toast.
  const del = async (id: string) => {
    setBusy((b) => ({ ...b, [id]: "delete" }));
    try {
      await deletePipeline(id).unwrap();
      refetchPipelines();

      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        const query = dispatch(dataConnectApi.endpoints.getPipeline.initiate(id, { forceRefetch: true }));
        try {
          await query.unwrap();
          await new Promise(r => setTimeout(r, 1000));
        } catch (e: unknown) {
          if (apiErrorShape(e)?.status === 404) {
            ok("delete → deleted");
            refetchPipelines();
            return;
          }
          throw e;
        } finally {
          query.unsubscribe();
        }
      }
      ok("delete → deleted (timeout — still finishing in the background)");
      refetchPipelines();
    } catch (e) {
      err(e);
    } finally {
      setBusy((b) => { const next = { ...b }; delete next[id]; return next; });
    }
  };

  // FE-BUG-15: runtime-aware confirmation — see DeletePipelineDialog for why
  // a draft and a deployed pipeline get different copy.
  const confirmDelete = (p: Pipeline) => {
    openModal(<DeletePipelineDialog pipeline={p} onClose={closeModal} onConfirm={() => { closeModal(); del(p.id); }} />);
  };

  // FE-BUG-24: Deploy no longer fires immediately — it opens the "Ready to
  // deploy" review screen first, same shape as confirmDelete above.
  const confirmDeploy = (p: Pipeline) => {
    openModal(<DeployReadinessDialog pipeline={p} onClose={closeModal} onConfirm={() => { closeModal(); act(p.id, "deploy"); }} />);
  };

  const tabs: { id: StatusTab; label: string; n: number; cls: string }[] = [
    { id: "all", label: "All", n: pipes.length, cls: "bb" },
    { id: "running", label: "Running", n: count("running"), cls: "bg" },
    { id: "deployed", label: "Deployed", n: count("deployed"), cls: "bb" },
    { id: "paused", label: "Paused", n: count("paused"), cls: "ba" },
    { id: "failed", label: "Failed", n: count("failed"), cls: "br" },
    { id: "draft", label: "Draft", n: count("draft"), cls: "bgr" },
  ];
  const tabKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const at = tabs.findIndex((t) => t.id === statusTab);
    const next = tabs[e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (at + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length]!;
    setStatusTab(next.id);
    document.getElementById(`pipelines-tab-${next.id}`)?.focus();
  };

  return (
    <div className="page-fill">
      <PH title="Pipelines" sub="Manage data ingestion pipelines"
        right={canManage && <button className="btn-p" onClick={() => openModal(<PipelineForm onClose={closeModal} />)}>+ New pipeline</button>} />

      <FlowFreshnessNote freshness={freshness} />

      <StatCards cards={[
        { k: "bl", l: "Live connectors", v: (flow.connectors || []).length, s: "Kafka Connect" },
        { k: "gn", l: "Records in topics", v: (flow.topics || []).reduce((a, t) => a + (t.messages || 0), 0), s: "source → Kafka" },
        // What sinks have READ (their committed offsets), not what they wrote: a sink that skips records reads them all and writes none
        // (2026-10-28: 101,500 read, 0 written), so calling this "written" overstated it. Real writes per second are on Dashboard / Monitoring.
        { k: "gn", l: "Records read by sinks", v: (flow.sinks || []).reduce((a, sk) => a + (sk.consumed || 0), 0), s: "Kafka → sink" },
        { k: (flow.sinks || []).some((sk) => sk.lag) ? "am" : "bl", l: "Sink lag", v: (flow.sinks || []).reduce((a, sk) => a + (sk.lag || 0), 0) },
      ]} />

      <div className="card card-fill">
        <div className="ch">
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div className="dc-tabs ptabs" role="tablist" aria-label="Show pipelines by status" onKeyDown={tabKey}>
              {tabs.map((t) => (
                <button
                  key={t.id} id={`pipelines-tab-${t.id}`} type="button" role="tab" aria-selected={statusTab === t.id} aria-controls="pipelines-table"
                  tabIndex={statusTab === t.id ? 0 : -1} onClick={() => setStatusTab(t.id)}
                >{t.label} <span className={`badge ${t.cls}`}>{t.n}</span></button>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <RefreshButton label="Refresh" />
            {q && (
              <button type="button" className="chip-x" aria-label={`Clear filter ${q}`} title="Show all pipelines" onClick={() => setQ("")}>
                <span>{q}</span><span aria-hidden="true">×</span>
              </button>
            )}
          </div>
        </div>
        <div role="table" id="pipelines-table" className="tbl-fill" style={{ "--cols": "1.5fr 1fr 0.9fr 1.2fr 0.55fr 0.9fr minmax(80px, 0.5fr)" }}>
          <div className="thead" role="row"><div className="th" role="columnheader">Name</div><div className="th" role="columnheader">Created at</div><div className="th" role="columnheader">Status</div><div className="th" role="columnheader">Topic</div><div className="th" role="columnheader">Mode</div><div className="th" role="columnheader">Records / progress</div><div className="th th-actions" role="columnheader">Actions</div></div>
          <div className="tbody" role="rowgroup">
            {rows.length ? rows.map((p) => {
              const { conn, live, records, pct } = rowInfo(p, connByName, topicMsgs, sinkByConn);
              const P = Math.max(0, Math.min(100, Math.round(pct)));
              const { unverified, syncingTo } = rowRuntimeHonesty(p, !!conn, live ?? "");
              return (
                <div className="tr" role="row" key={p.id}>
                  <div className="td" role="cell" style={{ gap: 8 }}><Ring state={live} pct={P} /><CutText text={p.name} style={{ fontWeight: 500 }} /></div>
                  <div className="tdm" role="cell"><CreatedAt value={p.created_at} /></div>
                  <div className="td" role="cell" style={{ flexWrap: "wrap", rowGap: 2 }}>
                    {conn
                      ? <>
                          <Pill cls={PIPE_BADGE[live as keyof typeof PIPE_BADGE] || "bb"}>{live}</Pill>
                          {syncingTo && <span title={`Desired ${syncingTo}, Kafka Connect currently reports ${live}`} style={{ color: "var(--color-text-secondary)", fontSize: 10, marginLeft: 4 }}>→ {syncingTo}</span>}
                        </>
                      : unverified
                        ? <><Pill cls="bgr">unknown</Pill> <span title={`Kafka Connect has no live data for this connector. Last recorded status: ${p.status}`} style={{ color: "var(--color-text-secondary)", fontSize: 9 }}>not live</span></>
                        : <Pill cls={PIPE_BADGE[(p.status ?? "draft") as keyof typeof PIPE_BADGE]}>{p.status}</Pill>}
                  </div>
                  <div className="tdmn" role="cell"><CutText text={topicSummary(p)} /></div>
                  <div className="tdm" role="cell">{p.mode}</div>
                  <div className="td" role="cell" style={{ flexDirection: "column", alignItems: "center", gap: 0 }}>
                    <span style={{ fontWeight: 500 }}>{records} <span className="tdm" style={{ padding: 0 }}>({P}%)</span></span>
                    <ProgressBar pct={P} color={live === "failed" ? "var(--dc-bad, #ef4444)" : "var(--dc-ok, #22c55e)"} />
                  </div>
                  <div className="acts" role="cell" style={{ justifyContent: "center" }}>
                    {/* All of a row's actions live behind the three dots, one line each. FE-BUG-19: viewing the detail is no longer gated on
                        desired_state (a draft that was never deployed has configuration worth reading). FE-BUG-25: while ANY action is busy for
                        this row the whole menu is busy (the backend would reject a different action too — claim_lifecycle_transition);
                        otherwise an action the row's status can't start (actionBlockedReason) is listed greyed, with the reason on hover. */}
                    <RowMenu
                      label={`Actions for ${p.name}`} busy={!!busy[p.id]}
                      items={[
                        { key: "view", label: "View details", ariaLabel: `View ${p.name} detail`, icon: <IconView />,
                          onSelect: () => openModal(<PipelineRuntimeDrawer pipelineId={p.id} onClose={closeModal} />) },
                        { key: "metrics", label: "View metrics", ariaLabel: `View metrics for ${p.name}`, icon: <IconMetrics />, disabled: !p.connector_name,
                          reason: "Not deployed yet — no connector to show metrics for",
                          onSelect: () => openModal(
                            <ConnectorMetricsDrawer pipeline={p} onClose={closeModal}
                              onOpenMonitoring={() => { closeModal(); selectConnector(p.connector_name); go?.("mon"); }} />) },
                        ...(canManage ? [{ key: "edit", label: "Edit", ariaLabel: `Edit ${p.name}`, icon: <IconEdit />, onSelect: () => onEditPipeline(p.id) }] : []),
                        ...(([
                          ["deploy", canDeploy, <IconDeploy key="i" />, () => confirmDeploy(p)],
                          ["pause", canControl, <IconPause key="i" />, () => act(p.id, "pause")],
                          ["resume", canControl, <IconResume key="i" />, () => act(p.id, "resume")],
                          ["restart", canControl, <IconRestart key="i" />, () => act(p.id, "restart")],
                        ] as const).filter(([, allowed]) => allowed).map(([a, , icon, onClick]) => {
                          const blocked = actionBlockedReason(p.status, a);
                          return { key: a, label: ACTION_LABEL[a], ariaLabel: `${ACTION_LABEL[a]} ${p.name}`, icon, onSelect: onClick, disabled: !!blocked, reason: blocked };
                        })),
                        ...(canDelete ? [{ key: "delete", label: "Delete", ariaLabel: `Delete ${p.name}`, icon: <IconDel />, danger: true, onSelect: () => confirmDelete(p) }] : []),
                      ]}
                    />
                  </div>
                </div>
              );
            }) : <div className="empty">{pipes.length === 0
              ? (canManage ? 'No pipelines yet — click "+ New pipeline".' : "No pipelines yet.")
              : statusTab !== "all" && list.length === 0 && !q ? `No ${statusTab} pipelines` : "No pipelines match"}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
