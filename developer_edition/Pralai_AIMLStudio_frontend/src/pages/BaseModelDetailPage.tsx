import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import { openModelOrTrainingRun } from "../components/modelNavigation";
import { DEFAULT_MAX_TOKENS, defaultInputFor, MaxTokensField, SpeechTester, TruncatedNote, withMaxTokens } from "../components/TestDrawer";
import { domainBadge } from "../components/modelBadges";
import type { BaseModel, CatalogModel, TestPrediction } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

type Tab = "overview" | "hyperparams" | "test" | "api";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "hyperparams", label: "Hyperparameters" },
  { key: "test", label: "Test" },
  { key: "api", label: "API" },
];

export default function BaseModelDetailPage() {
  const { can } = usePermissions();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const model = useApi(() => api.getBaseModel(id), [id]);
  const library = useApi(api.getBaseModels, []);
  const models = useApi(api.getModels, []);
  // ?tab=test (e.g. from the Train wizard) opens straight on that tab.
  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab");
  const [tab, setTab] = useState<Tab>(initialTab === "test" || initialTab === "hyperparams" || initialTab === "api" ? initialTab : "overview");

  if (model.loading) return <div className="empty-hint">Loading…</div>;
  if (model.error || !model.data) {
    return (
      <div>
        <BackButton />
        <div className="callout callout-amber">Couldn't load '{id}': {model.error ?? "not found"}</div>
      </div>
    );
  }

  const b = model.data;
  const alternatives = (b.alternatives ?? [])
    .map((altId) => (library.data ?? []).find((x) => x.id === altId))
    .filter((x): x is BaseModel => !!x);
  const derived = (models.data ?? []).filter((m) => m.base_ref === b.id);

  return (
    <div>
      <div className="page-fixed">
        <BackButton />

        <div className="page-head">
          <div>
            <div className="badge-row" style={{ marginBottom: 10 }}>
              {domainBadge(b.domain, b.framework)}
              <span className="badge badge-outline">Base library</span>
            </div>
            <h1 style={{ fontFamily: "var(--font-mono)" }}>{b.title}</h1>
            <p>{b.description}</p>
          </div>
          <div style={{ display: "flex", gap: 10, flex: "none" }}>
            <button hidden={!can(PERM.train)} className="btn btn-primary" onClick={() => navigate(`/train?base=${encodeURIComponent(b.id)}`)}>
              <Icon name="plus" />
              Train with this base model
            </button>
          </div>
        </div>

        <div className="tabs">
          {TABS.map((t) => (
            <button key={t.key} className={"tab" + (tab === t.key ? " active" : "")} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "overview" && <OverviewTab model={b} alternatives={alternatives} derived={derived} />}
      {tab === "hyperparams" && <HyperparamsTab model={b} />}
      {tab === "test" && <TestTab model={b} />}
      {tab === "api" && <ApiTab model={b} />}
    </div>
  );
}

function BackButton() {
  const navigate = useNavigate();
  return (
    <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={() => navigate("/catalog")}>
      <Icon name="arrow-left" />
      Back to catalog
    </button>
  );
}

/* ---------------- Overview ---------------- */

function OverviewTab({
  model: b, alternatives, derived,
}: { model: BaseModel; alternatives: BaseModel[]; derived: CatalogModel[] }) {
  const navigate = useNavigate();
  return (
    <>
      <div className="field-row" style={{ alignItems: "stretch", marginBottom: 16 }}>
        <div className="card card-pad">
          <div className="drawer-section-title">Overview</div>
          <p style={{ fontSize: 12.5, color: "var(--text-dark)", lineHeight: 1.6, marginBottom: 14 }}>
            {b.long_description}
          </p>
          <div className="kv-row"><span className="k">Task</span><span className="v">{b.task}</span></div>
          <div className="kv-row"><span className="k">Framework</span><span className="v">{b.framework || "—"}</span></div>
          <div className="kv-row"><span className="k">License</span><span className="v">{b.license || "—"}</span></div>
          {/* Only rendered when the model card actually publishes a figure —
              an absent row means "not published", not "zero". */}
          {b.params && <div className="kv-row"><span className="k">Parameters</span><span className="v">{b.params}</span></div>}
          {b.context_window && <div className="kv-row"><span className="k">Context window</span><span className="v">{b.context_window}</span></div>}
        </div>
        <div className="card card-pad">
          <div className="drawer-section-title">Deployment profile</div>
          <div className="kv-row">
            <span className="k">Registry URI</span>
            <span className="v" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{b.uri || "—"}</span>
          </div>
          <div className="kv-row"><span className="k">GPU required</span><span className="v">{b.gpu ? "Yes" : "No"}</span></div>
          <div className="kv-row"><span className="k">Recommended compute profile</span><span className="v">{b.compute_profile || "—"}</span></div>
          {/* The honest state of this entry. Registered in the catalog is not the
              same as callable: only entries whose weights have actually been
              fetched into object storage can serve a prediction, and for the rest
              the registry URI names a trainer image or estimator class with no
              pretrained artifact behind it. */}
          <div className="kv-row">
            <span className="k">Status</span>
            {b.stored_weights_uri ? (
              <span className="v" style={{ color: "var(--teal-700)" }}>
                Registered in Catalog · weights downloaded — callable for inference
              </span>
            ) : (
              <span className="v">Registered in Catalog · no weights fetched — train from it to get a servable model</span>
            )}
          </div>
          {b.weights_downloaded_at && (
            <div className="kv-row">
              <span className="k">Weights fetched</span>
              <span className="v">{new Date(b.weights_downloaded_at).toLocaleString()}</span>
            </div>
          )}
          {derived.length > 0 && (
            <div className="kv-row">
              <span className="k">Derived models</span>
              <span className="v" style={{ color: "var(--teal-700)" }}>
                {derived.length} derived model{derived.length === 1 ? "" : "s"} registered against it
              </span>
            </div>
          )}
          {derived.some((m) => m.kind === "adapter") && (
            <div className="callout callout-navy" style={{ marginTop: 12 }}>
              Serves as a shared base for multi-LoRA deployments — fine-tunes registered against this
              base run as adapters on one deployment, not one GPU each.
            </div>
          )}
        </div>
      </div>

      <div className="field-row" style={{ alignItems: "flex-start" }}>
        <div className="card card-pad">
          <div className="drawer-section-title">Alternatives in the base library</div>
          <div className="entity-list">
            {alternatives.map((a) => (
              <div
                className="entity-row"
                style={{ cursor: "pointer" }}
                key={a.id}
                onClick={() => navigate(`/base-models/${encodeURIComponent(a.id)}`)}
              >
                <div className={"entity-icon " + (a.domain === "llm" ? "icon-bg-amber" : "icon-bg-teal")}>
                  <Icon name="grid" />
                </div>
                <div className="entity-main">
                  <div className="entity-title">{a.title}</div>
                  <div className="entity-sub">{a.description}</div>
                </div>
              </div>
            ))}
            {alternatives.length === 0 && (
              <div className="empty-hint">No listed alternatives for this base model.</div>
            )}
          </div>
        </div>
        <div className="card card-pad">
          <div className="drawer-section-title">Derived models in this catalog</div>
          {derived.length ? (
            <div className="entity-list">
              {derived.map((m) => (
                <div
                  className="entity-row"
                  style={{ cursor: "pointer" }}
                  key={m.id}
                  onClick={() => openModelOrTrainingRun(navigate, m)}
                >
                  <div className="entity-icon icon-bg-amber"><Icon name="wand" /></div>
                  <div className="entity-main">
                    <div className="entity-title">{m.id}</div>
                    <div className="entity-sub">
                      {m.versions.length} version{m.versions.length === 1 ? "" : "s"} · {m.kind}
                      {m.pending ? " · first run in progress" : ""}
                    </div>
                  </div>
                  <div className="entity-meta">
                    <div className="alias-row">
                      {m.versions.filter((v) => v.alias !== "archived").map((v) => (
                        <span className={`alias alias-${v.alias}`} key={v.v}>{v.alias}</span>
                      ))}
                      {m.training && (
                        <span className="status-pill status-running"><span className="status-dot" /> training</span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-hint">Nothing trained from this base model yet.</div>
          )}
        </div>
      </div>
    </>
  );
}

/* ---------------- Hyperparameters ---------------- */

function HyperparamsTab({ model: b }: { model: BaseModel }) {
  return (
    <>
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Default hyperparameters</div>
        <div className="hint" style={{ marginBottom: 10 }}>
          Applied automatically in the Train wizard's fixed-hyperparameters mode unless overridden.
        </div>
        {b.default_hp ? (
          <pre className="step-log">{JSON.stringify(b.default_hp, null, 2)}</pre>
        ) : (
          // No invented block here: a null means this entry's trainer never
          // published a default set, and showing a plausible-looking one would
          // be presenting a guess as configuration the platform will apply.
          <div className="empty-hint">
            No default hyperparameters are recorded for this entry — its trainer takes the library
            defaults of the underlying framework, which this catalog does not restate.
          </div>
        )}
      </div>
      <div className="card card-pad">
        <div className="drawer-section-title">Supported hyperparameters</div>
        <div className="hint" style={{ marginBottom: 10 }}>
          The full schema — types, defaults, and allowed values. This is also what
          hyperparameter-search mode (Ray Tune + Optuna) samples from when a range or choice list is
          given instead of a fixed value.
        </div>
        {b.supported_hp ? (
          <pre className="step-log">{JSON.stringify(b.supported_hp, null, 2)}</pre>
        ) : (
          <div className="empty-hint">
            No tunable-parameter schema is recorded for this entry, so hyperparameter search has
            nothing declared to sample from here.
          </div>
        )}
      </div>
    </>
  );
}

/* ---------------- Test ---------------- */

function TestTab({ model: b }: { model: BaseModel }) {
  // Speech models take an audio file or text + voice, not a JSON body.
  if (/speech/i.test(b.task)) {
    return (
      <div className="card card-pad">
        <div className="drawer-section-title">Test this model</div>
        <p className="hint" style={{ marginBottom: 14 }}>
          Runs {b.title} for real from its published Hugging Face weights (downloaded and cached on first use).
        </p>
        <SpeechTester task={b.task} onRun={(body) => api.testPredictBaseModel(b.id, body)} />
      </div>
    );
  }
  return <JsonTestTab model={b} />;
}

function JsonTestTab({ model: b }: { model: BaseModel }) {
  const [input, setInput] = useState(() => defaultInputFor(b.task, b.domain));
  const [result, setResult] = useState<TestPrediction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const isLlm = b.domain === "llm";
  const [maxTokens, setMaxTokens] = useState(DEFAULT_MAX_TOKENS);

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(input);
    } catch {
      setError("That isn't valid JSON — fix the request body and try again.");
      setRunning(false);
      return;
    }
    try {
      setResult(await api.testPredictBaseModel(b.id, isLlm ? withMaxTokens(parsed, maxTokens) : parsed));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="card card-pad">
      <div className="drawer-section-title">Test this model</div>
      <p className="hint" style={{ marginBottom: 14 }}>
        {b.stored_weights_uri
          ? "This entry's weights have been downloaded into object storage, so Run performs a real inference call against them."
          : "No weights have been fetched for this entry — its registry URI names a trainer image or estimator class, not a pretrained artifact. Run still exercises the real serving route, which will say exactly that instead of returning a fabricated prediction."}
      </p>

      <div className="field-row" style={{ alignItems: "flex-start" }}>
        <div>
          <div className="field">
            <label>Input (JSON)</label>
            <textarea
              className="textarea"
              style={{ minHeight: 150 }}
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
          </div>
          {isLlm && <MaxTokensField value={maxTokens} onChange={setMaxTokens} />}
          <button className="btn btn-primary" onClick={run} disabled={running}>
            <Icon name="play" />
            {running ? (isLlm ? "Generating…" : "Running…") : "Run"}
          </button>
        </div>
        <div className="card card-pad" style={{ minHeight: 220, background: "var(--surface-sunken)" }}>
          <div className="drawer-section-title">Result</div>
          {!result && !error && <div className="empty-hint">Run the model to see a response here.</div>}
          {error && <div className="callout callout-amber">{error}</div>}
          {result && (
            <>
              {typeof (result.prediction as { prediction?: unknown } | null)?.prediction === "string" ? (
                <div className="step-log" style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", fontSize: 13.5, lineHeight: 1.6 }}>
                  {(result.prediction as { prediction: string }).prediction}
                </div>
              ) : (
                <div className="step-log" style={{ whiteSpace: "pre-wrap" }}>
                  {JSON.stringify(result.prediction, null, 2)}
                </div>
              )}
              <TruncatedNote prediction={result.prediction} />
              <div className="kv-row" style={{ marginTop: 10 }}>
                <span className="k">Latency</span><span className="v">{result.latencyMs} ms</span>
              </div>
              <div className="kv-row">
                <span className="k">Source</span>
                <span className="v" style={{ color: result.dataSource === "measured" ? "var(--teal-700)" : "var(--amber-700)" }}>
                  {result.dataSource === "measured" ? "Measured — real weights" : "Development stand-in"}
                </span>
              </div>
              <div
                className={result.dataSource === "measured" ? "callout callout-navy" : "callout callout-amber"}
                style={{ marginTop: 10 }}
              >
                {result.servedBy}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/* ---------------- API ---------------- */

function ApiTab({ model: b }: { model: BaseModel }) {
  const navigate = useNavigate();
  // The real route this deployment actually exposes — same path the Test tab
  // above calls. Deliberately not a dressed-up gateway URL the platform does
  // not serve.
  const endpoint = `POST /api/base-models/${b.id}/test-predict`;
  const body = defaultInputFor(b.task, b.domain).replace(/\n\s*/g, " ");
  const curl =
    `# Operator-authenticated: goes through the platform gateway (Envoy), which\n` +
    `# verifies your sign-in; needs the mlstudio:train:run permission.\n` +
    `curl -X POST <gateway>/api/base-models/${b.id}/test-predict \\\n` +
    `  -H 'Content-Type: application/json' \\\n` +
    `  -d '${body}'`;

  return (
    <div className="card card-pad">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <div className="drawer-section-title" style={{ marginBottom: 0 }}>Call this model</div>
        <button className="btn btn-secondary btn-sm" onClick={() => navigate("/endpoints")}>All endpoints</button>
      </div>
      <p className="hint" style={{ marginBottom: 14 }}>
        Base library entries are called through the Studio's own serving route. Entries whose weights
        have been downloaded return a real prediction; the rest return an explicit
        MODEL_INFERENCE_UNAVAILABLE with the reason. A model you train from this base gets its own
        promoted endpoint with OAuth client credentials — see Endpoints.
      </p>

      <div className="field" style={{ marginBottom: 14 }}>
        <label>API endpoint</label>
        <div className="input" style={{ fontFamily: "var(--font-mono)", fontSize: 12, background: "var(--surface-sunken)" }}>
          {endpoint}
        </div>
      </div>

      <div className="field" style={{ marginBottom: 0 }}>
        <label>curl example</label>
        <div className="step-log" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{curl}</div>
      </div>
    </div>
  );
}
