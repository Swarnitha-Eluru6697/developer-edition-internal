import { useEffect, useState, type ReactNode } from "react";
import { api, ApiRequestError } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import { StatusPill } from "../components/StatusPill";
import { TestDrawer } from "../components/TestDrawer";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";
import type { BaseModel, Endpoint } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

type Tab = "deployed" | "base";

export default function EndpointsPage() {
  const [tab, setTab] = useState<Tab>("deployed");
  const endpoints = useApi(api.getEndpoints, []);
  const models = useApi(api.getModels, []);
  const [openId, setOpenId] = useState<string | null>(null);
  // The top-bar search filters whichever tab is open.
  const [query] = usePageSearch(tab === "deployed" ? "Search deployed endpoints…" : "Search base models…");

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Endpoints</h1>
            <p>
              KServe (RawDeployment) and vLLM, both scaled by KEDA on live traffic — the same
              autoscaler for both domains.
            </p>
          </div>
        </div>

        <div className="tabs">
          <button className={"tab" + (tab === "deployed" ? " active" : "")} onClick={() => setTab("deployed")}>Deployed models</button>
          <button className={"tab" + (tab === "base" ? " active" : "")} onClick={() => setTab("base")}>Base models</button>
        </div>

      </div>

      {tab === "deployed" && (
        <div style={{ marginTop: 16 }}>
          {endpoints.loading && <div className="empty-hint">Loading…</div>}
          {endpoints.error && <div className="callout callout-amber">Couldn't load endpoints: {endpoints.error}</div>}
          <div className="entity-list">
            {endpoints.data?.filter((ep) => {
              const m = models.data?.find((x) => x.id === ep.modelId);
              return matchesQuery(query, ep.modelId, ep.title, ep.url, ep.status, m?.task, m?.domain, m?.framework);
            }).map((ep) => {
              const model = models.data?.find((m) => m.id === ep.modelId);
              const isLlm = model?.domain === "llm";
              const isAdapter = model?.kind === "adapter";
              return (
                <div className="entity-row" style={{ cursor: "pointer" }} key={ep.modelId} onClick={() => setOpenId(ep.modelId)}>
                  <div className={"entity-icon " + (isLlm ? "icon-bg-amber" : "icon-bg-teal")}>
                    <Icon name={isLlm ? "wand" : "server"} />
                  </div>
                  <div className="entity-main">
                    <div className="entity-title">
                      {ep.title}
                      <span className="tag" style={{ marginLeft: 6 }}>
                        {isAdapter
                          ? `shared · ${model?.base_ref ?? "base model"}`
                          : isLlm
                          ? "vLLM"
                          : "KServe · RawDeployment"}
                      </span>
                    </div>
                    <div className="entity-sub">
                      {isAdapter
                        ? "model_kind = adapter — served on its base model's deployment, no dedicated one"
                        : ep.rateLimits
                        ? `${ep.rateLimits.rpm} rpm · burst ${ep.rateLimits.burst} · ${ep.rateLimits.concurrent} concurrent`
                        : "Shares its base model's rate-limit pool"}
                    </div>
                  </div>
                  <div className="entity-meta">
                    {/* Measured, or explicitly absent. An endpoint nobody has
                        called shows a dash rather than a plausible req/min. */}
                    <div className="m-item">
                      <div className="m-val">{ep.traffic.requests > 0 ? ep.traffic.requestsPerMinute : "—"}</div>
                      <div className="m-lbl">req/min</div>
                    </div>
                    <div className="m-item">
                      <div className="m-val">{ep.traffic.p95LatencyMs !== null ? `${ep.traffic.p95LatencyMs}ms` : "—"}</div>
                      <div className="m-lbl">p95</div>
                    </div>
                    <div className="m-item"><StatusPill status={ep.status} /></div>
                  </div>
                </div>
              );
            })}
            {!endpoints.loading && !endpoints.data?.length && (
              <div className="empty-hint">
                No endpoints yet — promoting a model on Deploy &amp; promote provisions one.
              </div>
            )}
            {!!endpoints.data?.length && query.trim() && !endpoints.data.some((ep) => matchesQuery(query, ep.modelId, ep.title, ep.url, ep.status,
              models.data?.find((x) => x.id === ep.modelId)?.task)) && (
              <div className="empty-hint">No endpoints match “{query.trim()}”.</div>
            )}
          </div>
          <p className="hint" style={{ marginTop: 10 }}>
            req/min and p95 are measured from this endpoint's real prediction traces over the last
            hour. A dash means it has served no requests in that window — not that the figure is
            unknown.
          </p>
        </div>
      )}

      {tab === "base" && <BaseModelEndpoints query={query} />}

      {openId && (
        <EndpointDetail
          modelId={openId}
          onClose={() => setOpenId(null)}
          onChanged={() => endpoints.reload()}
        />
      )}
    </div>
  );
}

/**
 * The base library as a serving surface. The design presents every base model
 * as callable from registration; in reality only entries whose weights have
 * been fetched into storage can serve, so each row says which it is instead of
 * marking all 85 "deployed".
 */
const PAGE_SIZES = [10, 25, 50];

function BaseModelEndpoints({ query }: { query: string }) {
  const baseModels = useApi(api.getBaseModels, []);
  const [testing, setTesting] = useState<BaseModel | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const q = query.trim().toLowerCase();
  const filtered = (baseModels.data ?? []).filter((b) => matchesQuery(q, b.id, b.title, b.task, b.framework, b.domain));
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, pageCount);
  const first = (current - 1) * pageSize;
  const shown = filtered.slice(first, first + pageSize);
  // A new search starts from page 1.
  useEffect(() => setPage(1), [q, pageSize]);
  const goTo = (n: number) => {
    setPage(n);
    document.querySelector(".view")?.scrollTo({ top: 0 });
  };

  return (
    <div style={{ marginTop: 16 }}>
      <div className="callout callout-navy" style={{ marginBottom: 14 }}>
        Base library entries are separate from the models above, which are trained or fine-tuned
        instances that went through Deploy &amp; promote. An entry here is callable once its weights
        have been fetched into object storage; until then its registry URI names a trainer image or
        estimator class with no artifact to serve.
      </div>
      {baseModels.loading && <div className="empty-hint">Loading base models…</div>}
      {baseModels.error && <div className="callout callout-amber">Couldn't load base models: {baseModels.error}</div>}
      <div className="entity-list">
        {shown.map((b) => (
          <div className="entity-row" key={b.id}>
            <div className={"entity-icon " + (b.domain === "llm" ? "icon-bg-amber" : "icon-bg-teal")}>
              <Icon name="grid" />
            </div>
            <div className="entity-main">
              <div className="entity-title">
                {b.title}
                <span className="tag" style={{ marginLeft: 6 }}>{b.task}</span>
              </div>
              <div className="entity-sub" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                POST /api/base-models/{b.id}/test-predict
              </div>
            </div>
            <div className="entity-meta">
              <div className="m-item">
                <StatusPill
                  status={b.stored_weights_uri ? "healthy" : "queued"}
                  label={b.stored_weights_uri ? "weights ready" : "no weights"}
                />
              </div>
              <div className="m-item">
                <IconAction icon="test" label={`Test base model ${b.title}`} align="end" onClick={() => setTesting(b)} />
              </div>
            </div>
          </div>
        ))}
        {baseModels.data && filtered.length === 0 && <div className="empty-hint">No base models match that search.</div>}
      </div>
      {filtered.length > 0 && (
        <div className="pager">
          <span className="pager-summary">Showing {first + 1}–{first + shown.length} of {filtered.length}</span>
          <div className="pager-controls">
            <label className="pager-rows">
              Rows
              <select className="sm" value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
                {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <button className="btn btn-secondary btn-sm" disabled={current <= 1} onClick={() => goTo(current - 1)}>‹ Previous</button>
            <span className="pager-page">Page {current} of {pageCount}</span>
            <button className="btn btn-secondary btn-sm" disabled={current >= pageCount} onClick={() => goTo(current + 1)}>Next ›</button>
          </div>
        </div>
      )}

      {testing && (
        <TestDrawer
          label={testing.id}
          task={testing.task}
          domain={testing.domain}
          onRun={(input) => api.testPredictBaseModel(testing.id, input)}
          onClose={() => setTesting(null)}
        />
      )}
    </div>
  );
}

/**
 * A read-only value with a Copy button beside it, as in the mock — the URL,
 * token endpoint, client ID and a freshly issued secret are all things you
 * paste into client code, so selecting them by hand shouldn't be the only way.
 */
function CopyField({ value, what, copyValue, disabledReason, children }: {
  value: string;
  /** Names the value in the Copy button's tooltip ("Copy client ID"). */
  what: string;
  /** What actually goes on the clipboard, when it differs from what's shown. */
  copyValue?: string;
  /** Set when there's nothing real to copy (e.g. a masked secret). */
  disabledReason?: string;
  children?: ReactNode;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  async function copy() {
    const text = copyValue ?? value;
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      // Clipboard API unavailable (non-secure origin) — fall back to a hidden
      // textarea so the button still works over plain http.
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      setState(ok ? "copied" : "failed");
    }
    setTimeout(() => setState("idle"), 1800);
  }
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <input className="input" readOnly value={value} onFocus={(e) => e.currentTarget.select()}
        style={{ fontFamily: "var(--font-mono)", fontSize: 12.5, flex: 1, minWidth: 0 }} />
      <IconAction
        icon={state === "copied" ? "check" : "copy"} align="end" onClick={copy} disabled={!!disabledReason}
        label={disabledReason ? `Copy ${what} · ${disabledReason}` : state === "copied" ? `Copied ${what}` : state === "failed" ? `Copy failed — select the ${what} and copy it by hand` : `Copy ${what}`}
      />
      {children}
    </div>
  );
}

function EndpointDetail({
  modelId, onClose, onChanged,
}: { modelId: string; onClose: () => void; onChanged: () => void }) {
  const { can } = usePermissions();
  const endpoint = useApi(() => api.getEndpoint(modelId), [modelId]);
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ rpm: string; burst: string; concurrent: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  const ep: Endpoint | null = endpoint.data ?? null;
  const limits = draft ?? {
    rpm: String(ep?.rateLimits?.rpm ?? ""),
    burst: String(ep?.rateLimits?.burst ?? ""),
    concurrent: String(ep?.rateLimits?.concurrent ?? ""),
  };

  async function save() {
    if (!ep) return;
    setSaving(true);
    setError(null);
    try {
      await api.updateRateLimits(ep.modelId, {
        rpm: Number(limits.rpm), burst: Number(limits.burst), concurrent: Number(limits.concurrent),
      });
      endpoint.reload();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function regenerate() {
    if (!ep) return;
    setError(null);
    try {
      const result = await api.regenerateSecret(ep.modelId);
      setRevealedSecret(result.clientSecret);
      endpoint.reload();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-md">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)" }}>{modelId}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>URL, authentication, and rate limiting</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {endpoint.loading && <div className="empty-hint">Loading…</div>}
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          {ep && (
            <>
              <div className="drawer-section">
                <div className="metric-grid" style={{ gridTemplateColumns: "repeat(2,1fr)", marginBottom: 0 }}>
                  <div className="metric-mini">
                    <div className="m-label">Requests (last {ep.traffic.windowMinutes}m)</div>
                    <div className="m-val">{ep.traffic.requests}</div>
                    <div className="m-sub">{ep.traffic.traceCapped ? "at least — trace buffer full" : "measured"}</div>
                  </div>
                  <div className="metric-mini">
                    <div className="m-label">p95 latency</div>
                    <div className="m-val">{ep.traffic.p95LatencyMs !== null ? `${ep.traffic.p95LatencyMs}ms` : "—"}</div>
                    <div className="m-sub">{ep.traffic.lastRequestAt ? `last call ${new Date(ep.traffic.lastRequestAt).toLocaleString()}` : "no traffic yet"}</div>
                  </div>
                </div>
              </div>

              <div className="drawer-section">
                <div className="drawer-section-title">Endpoint URL</div>
                <CopyField value={ep.url} what="endpoint URL" />
                <p className="hint" style={{ marginTop: 8 }}>
                  This is the gateway address the deployment advertises. In this environment the same
                  model is reachable at <code>POST /api/serve/{ep.modelId}/predict</code>.
                </p>
              </div>

              <div className="drawer-section">
                <div className="drawer-section-title">Authentication</div>
                <div className="kv-row"><span className="k">Method</span><span className="v">{ep.authMethod}</span></div>
                <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                  <label>Token endpoint</label>
                  <CopyField value={ep.tokenEndpoint} what="token endpoint" />
                </div>
                <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                  <label>Client ID</label>
                  <CopyField value={ep.clientId} what="client ID" />
                </div>
                <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                  <label>Client secret</label>
                  {/* Only the masked form is stored, so there's nothing real to
                      copy until a secret is regenerated — copying the dots
                      would hand you a value that can't authenticate. */}
                  <CopyField
                    value={revealedSecret ?? ep.clientSecretMasked} what="client secret"
                    disabledReason={revealedSecret ? undefined : "Only the masked secret is stored — Regenerate to get a full secret you can copy."}
                  >
                    <IconAction icon="refresh" label="Regenerate client secret" danger gated={PERM.deploy} align="end" onClick={regenerate} />
                  </CopyField>
                </div>
                {revealedSecret && (
                  <div className="callout callout-amber" style={{ marginTop: 10 }}>
                    This is the only time the full secret is shown — copy it now. The previous secret
                    stopped working the moment this one was issued.
                  </div>
                )}
                <p className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
                  Exchange the client ID and secret for an access token at the token endpoint
                  (<code>grant_type=client_credentials</code>), then send it as a <code>Bearer</code>
                  {" "}header on requests to the model.
                </p>
              </div>

              <div className="drawer-section">
                <div className="drawer-section-title">Rate limiting</div>
                {ep.rateLimits ? (
                  <>
                    <p className="hint" style={{ marginBottom: 14 }}>
                      Requests over these limits get a 429 — tune per endpoint based on real traffic.
                    </p>
                    <div className="field-row">
                      <div className="field">
                        <label>Requests / minute</label>
                        <input className="input" value={limits.rpm} onChange={(e) => setDraft({ ...limits, rpm: e.target.value })} />
                      </div>
                      <div className="field">
                        <label>Burst limit</label>
                        <input className="input" value={limits.burst} onChange={(e) => setDraft({ ...limits, burst: e.target.value })} />
                      </div>
                    </div>
                    <div className="field">
                      <label>Max concurrent</label>
                      <input className="input" value={limits.concurrent} onChange={(e) => setDraft({ ...limits, concurrent: e.target.value })} />
                    </div>
                    <button hidden={!can(PERM.deploy)} className="btn btn-primary btn-sm" disabled={saving} onClick={save}>
                      <Icon name="check" />
                      {saving ? "Saving…" : "Save rate limits"}
                    </button>
                  </>
                ) : (
                  <div className="empty-hint">
                    This endpoint shares its base model's rate-limit pool — there's nothing separate
                    to configure here.
                  </div>
                )}
              </div>

              <div className="drawer-section">
                <button className="btn btn-primary btn-block" onClick={() => setTesting(true)}><Icon name="test" />Test this endpoint</button>
              </div>
            </>
          )}
        </div>
      </div>

      {testing && ep && (
        <TestDrawer
          label={ep.modelId}
          task="classification"
          domain="ml"
          modelId={ep.modelId}
          onRun={(input) => api.testPredict(ep.modelId, input)}
          onClose={() => setTesting(false)}
        />
      )}
    </div>
  );
}

