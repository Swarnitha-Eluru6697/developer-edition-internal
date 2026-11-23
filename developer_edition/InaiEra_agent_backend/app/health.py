"""
Real liveness/readiness checks (§0.81, GAP_REPORT) - the actual fix
for the reported gap: the old /api/health unconditionally returned
{"status": "ok"}, checking nothing real at all. Two real, separate
endpoints now, matching the standard real Kubernetes convention this
report asks for: /livez answers "is this process alive enough to not
be killed and restarted" (no dependency checks - a process that can
respond to this AT ALL is, by definition, alive); /readyz answers
"should traffic be routed here right now," and genuinely checks every
real dependency the report named.

Real, per-dependency checks, each independently timed out so one slow
dependency can't hang the whole probe:

- Postgres - a real SELECT 1 against this app's own real engine.
- Temporal - a real, short-timeout Client.connect against
  TEMPORAL_ADDRESS (the same real env var/default execution.py's own
  durable-path code already uses).
- Worker availability - a real DescribeTaskQueue RPC call (with
  report_pollers=True) against the real task queue worker.py's own
  TASK_QUEUE constant names, checking whether any real poller is
  currently registered - genuinely answers "is a worker actually
  polling," not just "is the Temporal server itself reachable."
- UCS - a real, short-timeout HTTP request to UCS_BASE_URL (the same
  real env var ucs_client.py already uses); any real HTTP response
  (even a 404) counts as "reachable," since the goal here is process
  reachability, not a specific real endpoint's own contract.
- Configured model provider - whether ANY provider with a runtime
  adapter has a key, from its own env var or the stored provider config
  (model_adapter's _stored_provider_config). A fast presence check, not
  a billed API call on every probe. MODEL_PROVIDER_REQUIRED narrows it
  to the one provider production policy has chosen.
- Malware scanner - which engine, if any, is actually scanning uploads.

What GATES readiness, and what is merely reported:

READYZ_REQUIRE (comma-separated) names the gating dependencies and wins
outright when set, including narrowing production below its default. An
unknown name in it is refused rather than ignored.

Unset, the default now depends on the environment: production requires
postgres, temporal, worker and model_provider; everywhere else requires
postgres alone. Postgres-alone-everywhere was the previous behaviour,
and it meant a production pod reported itself ready, and took traffic,
with Temporal down, no worker polling and no provider configured. UCS
stays out of the production set on purpose — an install with no
catalogue wired up is a supported configuration.

Anything not gating is still reported with its real status, and a
component that is degraded or unavailable without being required is
named under "degraded" alongside a ready verdict.

Two surfaces, different audiences:

/readyz is public and says only status per component. /internal/
diagnostics/readyz, behind require_operator, adds Temporal's address,
the task queue and poller count, the UCS URL, and the exception text
behind a failing component — detail the public probe used to hand to
any unauthenticated caller.
"""
import asyncio
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import Session, text

from .db import engine

router = APIRouter(tags=["health"])

DEFAULT_PROBE_TIMEOUT = float(os.getenv("READYZ_PROBE_TIMEOUT_SECONDS", "3"))


# What a deployment cannot serve traffic without, by environment.
#
# Postgres alone was the default everywhere. In production that meant a
# pod reported itself ready — and took traffic — with Temporal down, no
# worker polling and no model provider configured: every durable run
# would queue forever and every LLM node would fail, while the load
# balancer saw a healthy backend. Verified against the live install,
# which answered `"required": ["postgres"], "status": "ready"`.
#
# UCS is deliberately NOT in the production set. Governance is a
# per-deployment choice and an install with no catalogue wired up is a
# supported configuration; a deployment that gates on it says so.
_PRODUCTION_REQUIRED = ("postgres", "temporal", "worker", "model_provider")
_DEFAULT_REQUIRED = ("postgres",)

KNOWN_DEPENDENCIES = ("postgres", "temporal", "worker", "ucs",
                      "model_provider", "malware_scanner")


def _required_dependencies() -> set:
    """Which checks gate overall readiness.

    READYZ_REQUIRE still wins outright, including narrowing production
    below the default — an operator who has read the list and chosen
    something else has made a real decision. What changed is only what
    happens when nobody chose: production now inherits the full set
    instead of the reference environment's own minimum.

    An unknown name is refused rather than ignored. `READYZ_REQUIRE=
    postgres,temportal` would otherwise silently require Postgres only,
    which is the exact fault this function exists to close.
    """
    raw = os.getenv("READYZ_REQUIRE", "").strip()
    if not raw:
        from .environment import is_production
        return set(_PRODUCTION_REQUIRED if is_production() else _DEFAULT_REQUIRED)

    names = {d.strip() for d in raw.split(",") if d.strip()}
    unknown = sorted(names - set(KNOWN_DEPENDENCIES))
    if unknown:
        raise RuntimeError(
            f"READYZ_REQUIRE names unknown dependencies {unknown}. "
            f"Valid names: {', '.join(KNOWN_DEPENDENCIES)}. Refusing to "
            f"ignore them, because an ignored name is a dependency this "
            f"deployment believes it is gating on and is not."
        )
    return names


async def _check_postgres() -> Dict[str, Any]:
    try:
        def _query():
            with Session(engine) as s:
                s.exec(text("SELECT 1"))
        await asyncio.wait_for(asyncio.to_thread(_query), timeout=DEFAULT_PROBE_TIMEOUT)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "unreachable", "detail": str(e)}


async def _check_temporal() -> Dict[str, Any]:
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    try:
        from temporalio.client import Client
        await asyncio.wait_for(Client.connect(temporal_address, namespace="default"), timeout=DEFAULT_PROBE_TIMEOUT)
        return {"status": "ok", "address": temporal_address}
    except asyncio.TimeoutError:
        return {"status": "unreachable", "address": temporal_address, "detail": "connect timed out"}
    except Exception as e:
        return {"status": "unreachable", "address": temporal_address, "detail": str(e)}


async def _check_worker() -> Dict[str, Any]:
    """Real worker-availability check - genuinely different from the
    Temporal-server check above: a real Temporal server can be up with
    zero real workers actually polling the task queue, in which case
    every real durable-path run would hang forever waiting for one. A
    real DescribeTaskQueue(report_pollers=True) call answers this
    directly."""
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    try:
        from temporalio.client import Client
        import temporalio.api.workflowservice.v1 as wf_v1
        import temporalio.api.taskqueue.v1 as tq_v1
        import temporalio.api.enums.v1 as enums_v1
        from .worker import TASK_QUEUE

        client = await asyncio.wait_for(Client.connect(temporal_address, namespace="default"), timeout=DEFAULT_PROBE_TIMEOUT)
        req = wf_v1.DescribeTaskQueueRequest(
            namespace="default",
            task_queue=tq_v1.TaskQueue(name=TASK_QUEUE, kind=enums_v1.TaskQueueKind.TASK_QUEUE_KIND_NORMAL),
            task_queue_type=enums_v1.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            report_pollers=True,
        )
        resp = await asyncio.wait_for(client.workflow_service.describe_task_queue(req), timeout=DEFAULT_PROBE_TIMEOUT)
        poller_count = len(resp.pollers)
        return {
            "status": "ok" if poller_count > 0 else "no_pollers",
            "task_queue": TASK_QUEUE, "poller_count": poller_count,
        }
    except asyncio.TimeoutError:
        return {"status": "unreachable", "detail": "describe_task_queue timed out"}
    except Exception as e:
        return {"status": "unreachable", "detail": str(e)}


async def _check_ucs() -> Dict[str, Any]:
    ucs_base_url = os.getenv("UCS_BASE_URL", "http://127.0.0.1:5174")
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_PROBE_TIMEOUT) as client:
            resp = await client.get(ucs_base_url)
        return {"status": "ok", "url": ucs_base_url, "http_status": resp.status_code}
    except Exception as e:
        return {"status": "unreachable", "url": ucs_base_url, "detail": str(e)}


# Providers with an env-var convention of their own. Every other
# runtime-supported provider is configured through the Integrations page
# and is found by the stored-config lookup below instead.
_PROVIDER_ENV_VARS = {"Anthropic": "ANTHROPIC_API_KEY", "OpenAI": "OPENAI_API_KEY"}


def _check_model_provider() -> Dict[str, Any]:
    """Is ANY runtime-supported provider configured?

    A fast presence check, not a billed API call on every probe.

    This asked about Anthropic and nothing else. `call_llm` can execute
    against roughly a dozen providers — RUNTIME_SUPPORTED_PROVIDERS is
    `{Anthropic, Replicate}` plus every OpenAI-compatible base — so a
    perfectly good OpenAI-only deployment was reported `not_configured`
    and, once model_provider is a required dependency (see
    _required_dependencies), would have been held out of its load
    balancer forever while being entirely able to serve traffic.

    MODEL_PROVIDER_REQUIRED names the provider production policy has
    actually chosen, when there is one. Without it, any configured
    provider counts: the probe answers "can this process call a model",
    which is the question readiness is asking.
    """
    try:
        from .model_adapter import RUNTIME_SUPPORTED_PROVIDERS, _stored_provider_config
    except Exception as e:
        return {"status": "unreachable", "detail": f"{type(e).__name__}: {e}"}

    wanted = os.getenv("MODEL_PROVIDER_REQUIRED", "").strip()
    if wanted:
        if wanted not in RUNTIME_SUPPORTED_PROVIDERS:
            # Naming a provider with no adapter behind it is a
            # configuration error, not a dependency outage. Reported as
            # unreachable so it fails readiness rather than being
            # rounded off to "nothing configured".
            return {"status": "unreachable", "required": wanted,
                    "detail": f"MODEL_PROVIDER_REQUIRED={wanted!r} has no runtime adapter"}
        candidates = [wanted]
    else:
        candidates = sorted(RUNTIME_SUPPORTED_PROVIDERS)

    configured, errors = [], []
    for name in candidates:
        env_var = _PROVIDER_ENV_VARS.get(name)
        if env_var and os.getenv(env_var):
            configured.append({"provider": name, "source": "environment", "env_var": env_var})
            continue
        try:
            api_key, _org, _base_url = _stored_provider_config(name)
        except Exception as e:
            # One provider's lookup failing is not an answer about the
            # rest, so it is collected and the loop continues. It only
            # becomes the verdict if nothing at all was configured.
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if api_key:
            configured.append({"provider": name, "source": "stored_provider_config"})

    if configured:
        return {"status": "ok", "providers": [c["provider"] for c in configured],
                "detail": configured[0], **({"required": wanted} if wanted else {})}
    if errors:
        return {"status": "unreachable", "detail": "; ".join(errors[:3])}

    checked = wanted or f"{len(candidates)} runtime-supported providers"
    if os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true":
        return {"status": "not_configured",
                "detail": f"no API key configured for {checked}; "
                          "ALLOW_MOCK_MODELS=true permits mock responses"}
    return {"status": "not_configured",
            "detail": f"no API key configured for {checked}, and ALLOW_MOCK_MODELS is not set"}


@router.get("/livez")
def livez() -> dict:
    """Real liveness - no dependency checks, deliberately. A process
    that can respond to this at all is, by the real, standard
    Kubernetes definition, alive; liveness answers "should this
    container be restarted," not "should traffic be routed here" (see
    /readyz for that real, separate question)."""
    return {"status": "alive"}


def _check_malware_scanner() -> Dict[str, Any]:
    """Whether uploads are actually scanned, and by what.

    The reported gap: when no full scanner was available, upload
    validation fell back to a lightweight heuristic and nothing said so.
    An operator could reasonably believe documents were being scanned
    while they were getting an EICAR check and three magic numbers.

    Three outcomes, deliberately distinct:
      ok        a real engine answered a ping
      degraded  the built-in heuristic, by choice — ready, but stated
      unavailable  AV is required by policy and absent, or a configured
                   daemon is down; fails readiness when required
    """
    try:
        from .upload_security import malware_scanner_status

        status = malware_scanner_status()
    except Exception as e:  # never let a probe take readiness down
        return {"status": "unknown", "detail": f"{type(e).__name__}: {e}"}

    mode = status.get("mode")
    if mode == "clamav":
        state = "ok"
    elif mode == "heuristic":
        state = "degraded"
    else:
        state = "unavailable"
    return {"status": state, "mode": mode, "engine": status.get("engine"),
            "required": status.get("required"), "detail": status.get("detail")}


# Keys that describe infrastructure rather than health. Stripped from
# the public probe: an unauthenticated caller was being handed
# `temporal:7233`, `inaiera-agents-task-queue`, the UCS URL and raw
# exception text. A load balancer needs none of it to route a request,
# and the exception strings in particular carry driver and hostname
# detail straight out of a stack trace.
_INTERNAL_DETAIL_KEYS = ("address", "task_queue", "url", "http_status",
                         "detail", "poller_count", "engine", "providers")


def _public_check(check: Dict[str, Any]) -> Dict[str, Any]:
    """One component's status, with the topology taken out.

    `mode` and `required` survive deliberately. They are policy, not
    topology — "uploads are scanned by the built-in heuristic" is the
    thing the malware_scanner work made visible on purpose, and hiding
    it again here would undo that fix in the name of this one.
    """
    keep = {k: v for k, v in check.items() if k not in _INTERNAL_DETAIL_KEYS}
    # The provider NAMES go with the topology: which seven vendors this
    # deployment holds keys for is a map of its supply chain, and an
    # unauthenticated caller has no use for it. The count is what
    # readiness is actually asserting — that something can be called.
    if isinstance(check.get("providers"), list):
        keep["providers_configured"] = len(check["providers"])
    return keep or {"status": check.get("status", "unknown")}


async def _readiness_report() -> Dict[str, Any]:
    """The full picture, detail included. Shared by both surfaces.

    One computation, so the internal and external views cannot disagree
    about whether this process is ready — a second probe path is a
    second answer waiting to happen.
    """
    postgres, temporal, worker, ucs = await asyncio.gather(
        _check_postgres(), _check_temporal(), _check_worker(), _check_ucs(),
    )
    checks = {"postgres": postgres, "temporal": temporal, "worker": worker, "ucs": ucs,
              "model_provider": _check_model_provider(),
              "malware_scanner": _check_malware_scanner()}

    required = _required_dependencies()
    # malware_scanner is required whenever the deployment says uploads
    # need AV, without having to be listed in READYZ_REQUIRE as well:
    # declaring the policy in one place and the probe in another is how
    # they drift apart, and a deployment that set the policy and forgot
    # the probe would report itself ready while refusing every upload.
    if checks["malware_scanner"].get("required"):
        required = set(required) | {"malware_scanner"}

    failed_required = [name for name in required
                       if checks.get(name, {}).get("status") not in ("ok",)]
    # "degraded" is reported ALONGSIDE ready, not instead of it: a
    # heuristic-only scanner is a deliberate, supported configuration, so
    # it must not fail a readiness probe and take the service out of a
    # load balancer — but it must also not be invisible, which is what it
    # was. An operator polling /readyz can now see which of the two is
    # running without reading the source.
    # "unavailable" counts as degraded too when it is not required:
    # a CLAMD_HOST pointing at a daemon that is down would otherwise be
    # reported as a plain "ready", which is the exact invisibility being
    # fixed — someone configured a scanner and is not getting one.
    degraded = sorted(name for name, check in checks.items()
                      if check.get("status") in ("degraded", "unavailable")
                      and name not in required)

    return {"status": "ready" if not failed_required else "not_ready",
            "required": sorted(required), "checks": checks,
            "failed_required": sorted(failed_required),
            "degraded": degraded}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Whether to route traffic here. Deliberately says little else.

    Status per component, and which of them are gating — enough for an
    operator to see WHY a pod is out of rotation, with no hostnames,
    queue names, ports or exception text. The full diagnostic view is
    /internal/readyz, behind require_operator.
    """
    report = await _readiness_report()
    body: Dict[str, Any] = {
        "status": report["status"],
        "required": report["required"],
        "checks": {name: _public_check(check)
                   for name, check in report["checks"].items()},
    }
    if report["degraded"]:
        body["degraded"] = report["degraded"]
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503,
                        content=body)


async def readyz_detail() -> dict:
    """The diagnostic readiness view, for /internal.

    Registered by routers/internal.py rather than exposed here, so it
    inherits the same three layers every other operator route has: the
    /internal prefix an ingress refuses, require_operator failing
    closed, and exclusion from the published OpenAPI schema.

    Always 200 — this is a diagnostic read, and an operator asking what
    is wrong should not have the answer delivered as an error status.
    """
    return await _readiness_report()
