import os

from fastapi import Depends, FastAPI, Request
from gateway_auth import Principal, protect_app, require_permission
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import init_db
from .permissions import ACCESS
from .seed import seed
from .harness import PublishedVersionIntegrityError
from .middleware import MaxRequestBodySizeMiddleware
from .limits import MAX_JSON_BODY_BYTES
from .routers.uploads import MAX_UPLOAD_BYTES, MULTIPART_HEADROOM_BYTES
from .routers import agents, templates, runs, traces, alerts, monitoring, integrations, execution, webhooks, env_vars, knowledge_bases, evals, mcp, uploads, settings, guardrails, internal, embed, catalog, git_connections, agent_assets as agent_assets_router
from . import metrics as metrics_module
from . import health as health_module

app = FastAPI(
    title="InAiEra Agent Platform API",
    description=(
        "Real CRUD/API backend matching the approved UI mockup. "
        "NOT a production runtime — see docstrings on STUBbed endpoints "
        "(publish, MCP add, model-provider key config) for what's simulated "
        "vs. what would need real infra (Temporal, UCS, LLM providers, "
        "Firecracker) to be a true production system."
    ),
    version="0.1.0",
)

# Deny by default (contract docs/auth/01-CONTRACT.md section 5): every route —
# /api, /mcp, /internal, /metrics — needs a gateway-verified principal (401
# otherwise); each route then declares the permission it needs (403 without
# it, app/permissions.py). The public list is deliberately short: liveness /
# readiness probes and the static chat-bubble script. No inbound webhook here
# verifies its own signature (POST /api/webhooks/{token} relies on an
# unguessable token only), so none is public.
#
# Registered before CORS so CORS stays the outer layer and a browser preflight
# is answered without credentials.
PUBLIC_PATHS = ["/livez", "/readyz", "/embed.js"]


def _install_gateway_auth(application: FastAPI) -> None:
    from .gateway_identity import DEFAULT_TENANT_ID
    protect_app(application, public_paths=PUBLIC_PATHS, default_tenant_id=DEFAULT_TENANT_ID)


_install_gateway_auth(app)


def _cors_allowed_origins() -> list:
    """The browser origins allowed to call this API.

    `*` remains the default outside production, where this runs as a
    local dev backend with no auth layer and a wildcard is the honest
    setting. Under ENVIRONMENT=production it is refused: an explicit
    allowlist is required, and a wildcard is rejected even when set
    deliberately.

    The reason it is a hard refusal rather than a warning is that the
    failure is invisible. A production deployment that never set
    CORS_ALLOWED_ORIGINS inherited `*` and worked perfectly — nothing
    breaks, no log line fires, and the exposure only matters later, when
    a browser-facing auth or management surface is added in front of an
    API that already accepts cross-origin requests from anywhere.

    Resolved at import, before the middleware stack is built, so a
    misconfigured production deployment cannot come up and serve a
    single request. That is the same fail-closed shape as the
    ALLOW_MOCK_MODELS, SECRETS_ENCRYPTION_KEY and STORAGE_BACKEND
    startup refusals below — see their own guards.
    """
    from .environment import is_production

    raw = os.getenv("CORS_ALLOWED_ORIGINS")
    production = is_production()

    if raw is None or not raw.strip():
        if production:
            raise RuntimeError(
                "Refusing to start: ENVIRONMENT=production but CORS_ALLOWED_ORIGINS "
                "is not set. Unset means `*` — every origin — which is the right "
                "default for a local dev backend and wrong for a deployment. Set it "
                "to a comma-separated list of exact origins "
                "(e.g. https://app.example.com,https://admin.example.com)."
            )
        return ["*"]

    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if production and "*" in origins:
        raise RuntimeError(
            "Refusing to start: CORS_ALLOWED_ORIGINS contains '*' under "
            "ENVIRONMENT=production. A wildcard allowlist is not an allowlist. "
            "Name the exact origins this API serves."
        )
    if not origins:
        # e.g. CORS_ALLOWED_ORIGINS=",," — set, and yet empty.
        if production:
            raise RuntimeError(
                "Refusing to start: CORS_ALLOWED_ORIGINS is set but names no origins "
                f"({raw!r}). An empty allowlist blocks every browser client; if that "
                "is intended, say so with a single origin nothing matches."
            )
        return ["*"]
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    MaxRequestBodySizeMiddleware,
    # §0.111, GAP_REPORT — the actual, effective enforcement point for
    # the real, reported DoS gap: see middleware.py's own module
    # docstring for the full real reasoning on why this genuinely has
    # to live here, at the raw ASGI layer, rather than inside the
    # upload route handler itself. Scoped to the real upload endpoint
    # specifically — see MaxRequestBodySizeMiddleware's own docstring
    # for why a single, global cap isn't the right real choice here.
    # A DEFAULT for every API path, not just uploads. This was scoped to
    # /api/uploads alone, so flows, prompts, structured inputs, batches
    # and configuration could all receive arbitrarily large JSON bodies
    # — consumed by the ASGI layer and Pydantic before any handler could
    # object. 1MB is far more than any JSON request this platform makes:
    # the largest flow in its own seed data is a few kilobytes.
    max_bytes=MAX_JSON_BODY_BYTES,
    path_prefixes=("/api", "/internal", "/mcp"),
    # The paths that legitimately carry a document. The most specific
    # prefix wins, so these keep the upload ceiling while everything
    # else gets the JSON default above.
    overrides={
        "/api/uploads": MAX_UPLOAD_BYTES + MULTIPART_HEADROOM_BYTES,
        "/api/integrations/knowledge-bases": MAX_UPLOAD_BYTES + MULTIPART_HEADROOM_BYTES,
        # Agent node skills (<= 50 MB) and reference files (video <= 100 MB).
        # The route handlers enforce the exact per-type limits.
        r"re:/api/agents/[^/]+/skills": 50 * 1024 * 1024 + MULTIPART_HEADROOM_BYTES,
        r"re:/api/agents/[^/]+/files": 100 * 1024 * 1024 + MULTIPART_HEADROOM_BYTES,
    },
)


from .secrets_crypto import SecretDecryptionError  # noqa: E402


@app.exception_handler(PublishedVersionIntegrityError)
async def _published_version_integrity_handler(request: Request, exc: PublishedVersionIntegrityError):
    """Real, specific HTTP surface (§0.90, GAP_REPORT) for a real,
    server-side data-integrity failure — without this, an unhandled
    PublishedVersionIntegrityError would still genuinely stop the run
    (Python's own default exception propagation), but would reach the
    caller as a generic, unhelpful 500 with no real explanation. A
    real 500 is still the honest status (this is a real, server-side
    data problem the caller can't fix by changing their own request),
    but the real, specific body below lets an operator immediately
    understand exactly what's wrong and how to fix it, rather than
    needing to dig through server logs first.

    §0.117, GAP_REPORT — in a real production deployment, an EXTERNAL
    caller (not necessarily a trusted operator) could reach this same
    real 500 — real remediation guidance ("re-publish the agent
    through...") is genuinely useful to an operator with real access
    to fix it, but is real, internal operational detail an untrusted
    external caller has no real use for and shouldn't be shown. Uses
    exc.agent_id itself as the real correlation_id (already a real,
    stable identifier naming exactly which agent hit this) — the
    full, real detail is still logged, unsanitized, server-side."""
    from .error_responses import is_production, sanitized_error_value, log_full_detail
    log_full_detail(exc.agent_id, f"PublishedVersionIntegrityError: {exc}", exc=exc)
    if is_production():
        return JSONResponse(status_code=500, content=sanitized_error_value(exc.agent_id))
    return JSONResponse(status_code=500, content={"detail": str(exc), "error": "published_version_integrity_error", "agent_id": exc.agent_id})


@app.exception_handler(SecretDecryptionError)
async def _secret_decryption_handler(request: Request, exc: SecretDecryptionError):
    """A stored credential encrypted under a different SECRETS_ENCRYPTION_KEY.

    Fifteen-odd call sites decrypt stored secrets, and only a couple caught
    this — everywhere else it fell through to the generic backstop below
    and reached the caller as an anonymous 500 ("AGENT_EXECUTION_FAILED"),
    which reads as a server bug. It is not: the request is fine, a stored
    value needs re-entering. 409 says the resource is in a state the caller
    can fix, and the body says how. Nothing secret is in the message."""
    from .error_responses import log_full_detail
    log_full_detail(f"secret-{request.url.path}", f"SecretDecryptionError: {exc}", exc=exc)
    return JSONResponse(status_code=409, content={
        "error": "stored_secret_unreadable",
        "detail": (
            "A stored credential for this resource cannot be decrypted with this server's current "
            "SECRETS_ENCRYPTION_KEY — it was saved under a different key. Re-enter it: a knowledge "
            "base's connection password or API key under Integrations > KB Connections > Edit "
            "connection; a pipeline's Retrieval or Upload API key by regenerating it on that "
            "pipeline's API tab."),
    })


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """§0.117, GAP_REPORT — the actual, final backstop for the
    reported gap: every other real fix this section adds sanitizes a
    SPECIFIC, already-anticipated failure path (a node handler's own
    exception, a UCS soft-denial, a batch row's own failure) — but a
    genuinely UNANTICIPATED exception (a real database outage, a bug
    in code that runs before any per-endpoint try/except gets a
    chance to run, a real dependency-injection failure) would still
    reach an external caller as FastAPI/Starlette's own real, default
    500 response. Confirmed live, directly: without this handler,
    Starlette's own real default (this app has no real debug=True set
    anywhere) returns a completely EMPTY 500 body — no information at
    all, real or otherwise. This is the real, final, global backstop:
    ANY exception that reaches this far (nothing more specific already
    handled it — Starlette's own real handler-resolution order tries
    the most specific real exception type first, confirmed live, so
    this never shadows HTTPException's own normal 4xx handling or the
    more specific PublishedVersionIntegrityError handler just above)
    gets the report's own real, structured shape — in EVERY real
    environment, not just production: the pre-existing, real
    alternative (an empty body) was strictly worse for a real
    developer too, and the full, real detail (exc_info and all) is
    always logged server-side below regardless of environment, the
    same real place a local developer already looks first. A fresh,
    real correlation_id is generated here specifically BECAUSE this is
    the one real case with no existing run_id/agent_id to reuse — this
    exception, by definition, happened somewhere this platform's own
    normal Run/Trace-creating code paths never even reached."""
    import uuid
    from .error_responses import sanitized_error_value, log_full_detail
    correlation_id = f"unhandled-{uuid.uuid4().hex[:12]}"
    log_full_detail(correlation_id, f"Unhandled exception on {request.method} {request.url.path}", exc=exc)
    return JSONResponse(status_code=500, content=sanitized_error_value(correlation_id))


@app.on_event("startup")
def on_startup():
    # Real, honest startup refusal (§0.58, GAP_REPORT) — the second
    # real half of the reported fix. ALLOW_MOCK_MODELS=true is a real,
    # legitimate opt-in for local development (see model_adapter.py's
    # own real, explicit-consent requirement for mock LLM responses),
    # but genuinely dangerous in a real production deployment: a
    # misconfigured pod with no real ANTHROPIC_API_KEY could otherwise
    # serve synthetic mock answers while still reporting every run as a
    # success. Rather than silently allow that combination, this
    # platform refuses to even start.
    from .environment import is_production as _is_production

    allow_mocks = os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true"
    if allow_mocks and _is_production():
        raise RuntimeError(
            "Refusing to start: ALLOW_MOCK_MODELS=true is set alongside ENVIRONMENT=production. "
            "Mock LLM responses are a real, deliberate development-only opt-in — never enable "
            "ALLOW_MOCK_MODELS in a real production deployment. Unset one of these two "
            "environment variables to start this server."
        )
    # Real, fail-CLOSED secrets-encryption-key check (§0.89, GAP_REPORT)
    # — the same real "refuse to start rather than silently misbehave
    # later" philosophy as the ALLOW_MOCK_MODELS check just above.
    # Without a real, shared SECRETS_ENCRYPTION_KEY, this API process
    # and the Temporal worker process (a genuinely separate process —
    # see worker.py's own identical real check) would each generate
    # their OWN, independent, temporary key — a real secret encrypted
    # by one is genuinely undecryptable by the other. See
    # secrets_crypto.py's own check_production_key_requirement for the
    # full real reasoning.
    from .secrets_crypto import check_production_key_requirement
    check_production_key_requirement()
    # Same reasoning, applied to where uploads land: STORAGE_BACKEND
    # defaults to "local", and in a container that is the writable layer
    # — destroyed on replacement and invisible to other replicas. Refused
    # at boot under ENVIRONMENT=production unless the deployment declares
    # the path a durable shared volume. See storage.py's own guard.
    from .storage import check_production_storage_requirement
    check_production_storage_requirement()
    init_db()
    # Reference data (provider/model/tool/template catalog) always;
    # sample agents and their synthetic runs, traces, alerts, roles and
    # logs ONLY when SEED_DEMO_DATA is explicitly set, and never under
    # ENVIRONMENT=production. See seed.py's own module docstring.
    seed()
    # Still run separately: sync_model_catalog does more than insert
    # missing rows — it also widens an existing provider's `types` to
    # include "Text Embedding" and reconciles (provider, api_model_id)
    # pairs registered under a different name, neither of which
    # seed_reference_data's plain insert-if-absent does.
    from .seed import sync_model_catalog
    sync_model_catalog()
    # Hourly cleanup of orphaned agent assets, retired assets nothing pins any
    # more, and unreferenced node secrets (ASSET_RECONCILE_INTERVAL_SECONDS=0
    # turns the in-process job off for deployments that run it as a CronJob).
    from .agent_assets import start_asset_reconciler
    start_asset_reconciler()


def _principal(request: Request) -> Principal:
    from .gateway_identity import get_principal
    return get_principal(request)


@app.get("/api/me")
def me(principal: Principal = Depends(_principal)) -> dict:
    """The contract's GET /me body: `{ user, userId, tenant, permissions }`.

    Needs a gateway principal and nothing more, so the UI can tell "no
    permissions here" apart from "not signed in". The UI shows pages from
    `permissions`; the real control is each route's own permission check."""
    return principal.me()


@app.get("/api/auth/whoami", dependencies=[Depends(require_permission(ACCESS))])
def whoami(request: Request) -> dict:
    """Whether THIS request arrived with a gateway-verified identity.

    Exists so the "Require authenticated user" switch can tell you what
    turning it on will actually do from where you are sitting. Deliberately
    reports only whether identity was established and why not, never the
    secret or any value the caller did not already send.
    """
    from .auth import get_verified_identity, _identity_refusal_detail
    from .gateway_identity import gateway_trust_enabled

    identity = get_verified_identity(request)
    return {
        "verified_identity": identity,
        "gateway_trust_enabled": gateway_trust_enabled(),
        # Only when there is nothing to report as an identity.
        "reason": None if identity else _identity_refusal_detail(request),
    }


@app.get("/api/health", dependencies=[Depends(require_permission(ACCESS))])
def health():
    """Real, backward-compatible alias (§0.81, GAP_REPORT) — kept for
    any real, existing external monitoring/uptime check already
    polling this exact path, but now delegates to the real /livez
    liveness definition rather than the old, always-`{"status":
    "ok"}` stub. See health.py's own module docstring for the real,
    separate /readyz — the actual fix for the reported gap — which
    genuinely checks Postgres, Temporal, worker availability, UCS, and
    the configured model provider."""
    return health_module.livez()


app.include_router(health_module.router)
app.include_router(agents.router)
app.include_router(execution.router)
app.include_router(templates.router)
app.include_router(runs.router)
app.include_router(traces.router)
app.include_router(alerts.router)
app.include_router(monitoring.router)
app.include_router(integrations.router)
app.include_router(webhooks.router)
app.include_router(env_vars.router)
app.include_router(knowledge_bases.router)
app.include_router(evals.router)
app.include_router(mcp.router)
app.include_router(uploads.router)
app.include_router(agent_assets_router.router)
app.include_router(settings.router)
app.include_router(git_connections.router)
app.include_router(guardrails.router)
app.include_router(metrics_module.router)
app.include_router(embed.router)
# UCS's object page defers Api Access / Logs / Monitoring / Traces to this
# service; these are the FQN-keyed endpoints it points at.
app.include_router(catalog.router)
# Operator maintenance, deliberately last and deliberately separate: it
# is not part of the product API surface. Its own router carries the
# require_operator guard and is excluded from the OpenAPI schema, and
# production ingress should refuse the /internal prefix outright — see
# routers/internal.py and InaiEra_Agent_frontend/nginx.conf.
app.include_router(internal.router)
# Diagnostic readiness, deliberately NOT on the public /readyz — see
# health.py's own readyz docstring for what was being disclosed there.
app.include_router(internal.diagnostics_router)
