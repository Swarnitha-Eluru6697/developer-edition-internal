import os
import re
from datetime import datetime
from typing import List, Optional, Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from pydantic import BaseModel

from ..db import get_session
from ..models import ModelProvider, Tool, ToolAction, MCPServer, DataSource, ProviderModel, StorageDestination, MlStudioCredential
from ..secrets_crypto import encrypt_secret, require_durable_key, using_ephemeral_key


def _require_durable_secret_key(what: str) -> None:
    """require_durable_key, surfaced as a 503 rather than a 500.

    The condition is a server misconfiguration the caller cannot fix
    from the request, and the message names exactly what to do, so it
    belongs in the response instead of only in a stack trace."""
    try:
        require_durable_key(what)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
from .. import data_sources

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


# ---------------- model providers ----------------

class ApiKeyConfigIn(BaseModel):
    authorization_name: Optional[str] = None
    api_key: str
    organization: Optional[str] = None
    api_base_url: Optional[str] = None
    validate_model: Optional[str] = None
    api_protocol: Optional[str] = "Responses API"


@router.get("/model-providers", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_model_providers(session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    """§0.114, GAP_REPORT — the actual fix for the report's own core,
    reported concern: this used to return every real, seeded
    ModelProvider row with no way for a caller to tell "Anthropic" (a
    genuinely real, wired execution adapter behind it) apart from
    "OpenAI, Gemini, Amazon Bedrock, ..." (real, honest catalog entries
    with no actual runtime behind most of them) — a real
    product/runtime mismatch if the UI ever implied any of the latter
    were already usable for real Agent execution. `runtime_supported`
    is now a real, computed field (from model_adapter.py's own
    RUNTIME_SUPPORTED_PROVIDERS — the same one, real source of truth
    _call_anthropic/_call_openai are actually implemented against, not
    a second, separately-maintained list that could silently drift
    from what's genuinely wired) added to each real row's own
    response, so a real frontend can honestly distinguish the two
    rather than presenting every real, cataloged provider as equally
    usable."""
    from ..model_adapter import (RUNTIME_SUPPORTED_PROVIDERS, _NOT_OPENAI_COMPATIBLE,
                                 _OPENAI_COMPATIBLE_BASES)
    from ..secrets_crypto import decrypt_secret, SecretDecryptionError

    def key_usable(row: ModelProvider) -> Optional[bool]:
        """Whether the stored key can actually be READ, not merely
        whether one exists.

        api_key_configured only records that a key was once saved. A key
        encrypted under a different SECRETS_ENCRYPTION_KEY than the one
        now configured still sets that flag and still shows as a masked
        value, so the provider reads as ready while every call using it
        reports "no key configured" — the failure appears nowhere near
        its cause. This is what actually happens when a dev server runs
        without the key exported: secrets_crypto falls back to a
        throwaway per-process key, and everything saved through it
        becomes unreadable on the next reload.

        None when there is nothing stored to test."""
        if not row.api_key:
            return None
        try:
            decrypt_secret(row.api_key)
            return True
        except SecretDecryptionError:
            return False
        except Exception:
            return False

    rows = session.exec(select(ModelProvider)).all()
    return [{
        **r.model_dump(),
        "runtime_supported": r.name in RUNTIME_SUPPORTED_PROVIDERS,
        "key_usable": key_usable(r),
        # Why a provider is not wired, so the UI can be specific rather
        # than saying a bare "not supported".
        "unsupported_reason": _NOT_OPENAI_COMPATIBLE.get(r.name),
        # Azure is provisioned per resource, so it cannot run until the
        # deployment supplies its own endpoint.
        "requires_base_url": _OPENAI_COMPATIBLE_BASES.get(r.name) == "",
    } for r in rows]


# Each SDK already knows its own endpoint, so a blank api_base_url is
# the correct value for a normal setup, not a missing one. These two
# helpers exist because storing this field verbatim silently broke both
# real providers at once: an Anthropic row saved with
# https://api.openai.com pointed the Anthropic client at OpenAI (404),
# and the same value on the OpenAI row dropped the /v1 the OpenAI SDK
# appends by default (also 404). Neither surfaced until a model was
# actually called.
_PROVIDER_HOSTS = {
    "OpenAI": "api.openai.com",
    "Anthropic": "api.anthropic.com",
    "DeepSeek": "api.deepseek.com",
    "Cohere": "api.cohere.com",
}


def _validated_base_url(provider: str, raw: Optional[str]) -> Optional[str]:
    """Empty means "use the SDK default", which is what almost every
    deployment wants. A real custom endpoint (Azure, a proxy, a
    self-hosted vLLM) is still allowed — only a host that unambiguously
    belongs to a DIFFERENT known vendor is refused, because that is a
    misconfiguration rather than a deliberate override."""
    from urllib.parse import urlparse
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, f"api_base_url must be an absolute http(s) URL, got {raw!r}.")
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    own = _PROVIDER_HOSTS.get(provider)
    for other, other_host in _PROVIDER_HOSTS.items():
        if other != provider and host == other_host:
            raise HTTPException(
                400,
                f"api_base_url '{url}' is {other}'s endpoint, but you are configuring {provider}. "
                f"Leave the field blank to use {provider}'s own endpoint"
                + (f" ({own})" if own else "") + ".",
            )
    # The OpenAI SDK appends paths to base_url, so the bare host loses
    # the /v1 its default carries and every call 404s.
    if provider == "OpenAI" and host == "api.openai.com" and not parsed.path.strip("/"):
        return None
    return url


def _validated_organization(provider: str, raw: Optional[str]) -> Optional[str]:
    """OpenAI sends this as the OpenAI-Organization header and rejects
    the request outright if it is not the key's real organization ID —
    "OpenAI-Organization header should match organization for API key".
    A company name typed here is the easy mistake, so catch it before it
    becomes a 401 at call time. Blank is correct and lets the key's own
    default organization apply."""
    org = (raw or "").strip()
    if not org:
        return None
    if provider == "OpenAI" and not org.startswith("org-"):
        raise HTTPException(
            400,
            f"OpenAI organization must be the organization ID from your OpenAI account "
            f"(it begins with 'org-'), not a name like {org!r}. Leave it blank to use the "
            f"default organization for this API key.",
        )
    return org


@router.post("/model-providers/{name}/api-key", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def configure_model_provider(name: str, body: ApiKeyConfigIn, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Real storage (§0.63, GAP_REPORT) — a real, reported gap: this
    endpoint used to accept api_key/organization/api_base_url and
    store none of them, only ever flipping api_key_configured to True.
    "Provider configured" in the UI was genuinely disconnected from
    what the runtime actually executed against — model_adapter.py's
    call_llm only ever read os.getenv("ANTHROPIC_API_KEY"), never this
    row. api_key is now real Fernet ciphertext at rest
    (secrets_crypto.py, the same mechanism already used for EnvVar/
    KnowledgeBase/MCP secrets); call_llm now genuinely falls back to
    this stored, decrypted value for the "Anthropic" provider
    specifically when the env var isn't set — see model_adapter.py's
    own docstring for the real, honest scope: the other listed
    providers (OpenAI, Gemini, Bedrock, etc.) remain real catalog
    entries with no actual execution path behind them yet, matching
    this platform's existing single-real-provider boundary; storing
    their credentials here is honest bookkeeping for when that changes,
    not a claim that they're already wired to anything.

    Still does not validate the key against the provider's real API
    (a live network call this environment has no real credentials to
    make on the caller's behalf) — status flips to "credits" the same
    way it always has, reflecting that a key was genuinely provided,
    not that it was verified to work."""
    mp = session.get(ModelProvider, name)
    if not mp:
        raise HTTPException(404, "Provider not found")
    if not body.api_key:
        raise HTTPException(400, "API key is required")
    api_base_url = _validated_base_url(name, body.api_base_url)
    organization = _validated_organization(name, body.organization)
    mp.api_key_configured = True
    mp.status = "credits"
    # A secret encrypted with a throwaway key is worse than no
    # secret: it reads as configured and fails everywhere.
    _require_durable_secret_key(f"the API key for {name}")
    mp.api_key = encrypt_secret(body.api_key)
    mp.organization = organization
    mp.api_base_url = api_base_url
    session.add(mp)
    session.commit()
    session.refresh(mp)
    # Same real, one-time-echo convention as EnvVar/KnowledgeBase/MCP
    # secrets — the real plaintext key the caller just sent, only on
    # this response; every later GET goes through ModelProvider's own
    # field_serializer and never shows it again.
    result = mp.model_dump()
    result["api_key"] = body.api_key
    return result


VALID_MODEL_TYPES = {"LLM", "Text Embedding", "Rerank"}


class ProviderModelIn(BaseModel):
    name: str
    api_model_id: str
    types: Optional[List[str]] = None
    input_price_per_mtok: Optional[float] = None
    output_price_per_mtok: Optional[float] = None
    # Only meaningful for a Text Embedding model. Left unset, the real
    # width is discovered from the provider's first response, which is
    # preferable to trusting a typed-in number.
    embedding_dimension: Optional[int] = None


@router.get("/model-providers/{name}/models", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_provider_models(name: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Both halves of the catalog for one provider: the models compiled
    into model_adapter.py and the ones registered here. `builtin` is
    read from that module directly rather than duplicated, so the two
    cannot drift."""
    mp = session.get(ModelProvider, name)
    if not mp:
        raise HTTPException(404, "Provider not found")
    from ..model_adapter import _MODEL_PROVIDERS, _MODEL_MAP, RUNTIME_SUPPORTED_PROVIDERS
    kind = name.lower()
    builtin = [
        {"name": m, "api_model_id": _MODEL_MAP.get(m), "builtin": True}
        for m, prov in _MODEL_PROVIDERS.items() if prov == kind
    ]
    custom = [
        {"name": r.name, "api_model_id": r.api_model_id, "types": r.types,
         "input_price_per_mtok": r.input_price_per_mtok,
         "output_price_per_mtok": r.output_price_per_mtok,
         "embedding_dimension": r.embedding_dimension, "builtin": False}
        for r in session.exec(select(ProviderModel).where(ProviderModel.provider_name == name)).all()
    ]
    return {"provider": name, "runtime_supported": name in RUNTIME_SUPPORTED_PROVIDERS,
            "builtin": sorted(builtin, key=lambda x: x["name"]),
            "custom": sorted(custom, key=lambda x: x["name"])}


@router.post("/model-providers/{name}/models", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def add_provider_model(name: str, body: ProviderModelIn, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Register a model against a provider so the builder can select it
    and call_llm can route to it.

    Refused for a provider with no execution adapter: model_adapter's
    RUNTIME_SUPPORTED_PROVIDERS is the only honest gate, and letting a
    model be added under a catalog-only provider would put an
    unrunnable entry in the builder's model picker. Also refused
    without a configured API key, since the call could not authenticate
    anyway."""
    mp = session.get(ModelProvider, name)
    if not mp:
        raise HTTPException(404, "Provider not found")
    from ..model_adapter import RUNTIME_SUPPORTED_PROVIDERS, _MODEL_PROVIDERS
    if name not in RUNTIME_SUPPORTED_PROVIDERS:
        raise HTTPException(
            400,
            f"{name} has no execution adapter in this platform yet, so a model added under it "
            f"could never actually run. Runtime-wired providers: {sorted(RUNTIME_SUPPORTED_PROVIDERS)}.",
        )
    if not mp.api_key_configured:
        raise HTTPException(400, f"Add an API key for {name} before registering models against it.")
    model_name = (body.name or "").strip()
    api_model_id = (body.api_model_id or "").strip()
    if not model_name or not api_model_id:
        raise HTTPException(400, "Both name and api_model_id are required.")
    if model_name in _MODEL_PROVIDERS:
        raise HTTPException(400, f"'{model_name}' is already a built-in model for this platform.")
    if session.get(ProviderModel, model_name):
        raise HTTPException(400, f"A model named '{model_name}' is already registered.")
    types = body.types or ["LLM"]
    bad = [t for t in types if t not in VALID_MODEL_TYPES]
    if bad:
        raise HTTPException(400, f"Unknown model type(s) {bad}; valid types are {sorted(VALID_MODEL_TYPES)}.")
    row = ProviderModel(
        name=model_name, provider_name=name, api_model_id=api_model_id,
        types=types,
        input_price_per_mtok=body.input_price_per_mtok,
        output_price_per_mtok=body.output_price_per_mtok,
        embedding_dimension=body.embedding_dimension,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"name": row.name, "provider_name": row.provider_name,
            "api_model_id": row.api_model_id, "types": row.types,
            "input_price_per_mtok": row.input_price_per_mtok,
            "output_price_per_mtok": row.output_price_per_mtok,
            "embedding_dimension": row.embedding_dimension, "builtin": False}


@router.delete("/model-providers/{name}/models/{model_name}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def delete_provider_model(name: str, model_name: str, session: Session = Depends(get_session)) -> Dict[str, str]:
    row = session.get(ProviderModel, model_name)
    if not row or row.provider_name != name:
        raise HTTPException(404, "Model not registered for this provider")
    session.delete(row)
    session.commit()
    return {"deleted": model_name}


@router.get("/embedding-models", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_embedding_models(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Every Text Embedding model selectable for a RAG pipeline, across
    all providers — what the RAG Settings tab's embedding dropdown reads.

    The built-in local hashing option is listed first and always
    available: it needs no key and no network. Everything else comes
    from models registered on this page, so the list grows as providers
    are configured rather than being hardcoded here.

    A model qualifies either by declaring the "Text Embedding" type or
    by having an id from a known embedding family — see
    embeddings.is_embedding_model, which is the SAME predicate the
    runtime uses to resolve a model. This second route exists because
    the type is a checkbox: a genuine embedding model saved as
    types=["LLM"] (`intfloat/e5-base-v2`, say) used to be invisible
    here, with nothing to explain why. Such a model is now listed and
    marked `inferred` so the reason is visible, rather than either
    hidden or passed off as declared.
    """
    from ..model_adapter import RUNTIME_SUPPORTED_PROVIDERS
    from ..embeddings import EMBED_MODEL_NAME, EMBED_DIM, is_embedding_model
    providers = {p.name: p for p in session.exec(select(ModelProvider)).all()}
    options = [{
        "name": EMBED_MODEL_NAME, "provider": None, "api_model_id": None,
        "embedding_dimension": EMBED_DIM, "builtin": True, "available": True,
        "note": "Local feature hashing — no key needed, but lexical only: it scores a "
                "paraphrase at zero similarity.",
    }]
    for r in session.exec(select(ProviderModel)).all():
        usable, inferred = is_embedding_model(r.name, r.api_model_id, r.types)
        if not usable:
            continue
        mp = providers.get(r.provider_name)
        # A model whose provider has no key cannot embed anything, so it
        # is listed but flagged rather than silently offered.
        has_key = bool(mp and mp.api_key_configured)
        available = has_key and r.provider_name in RUNTIME_SUPPORTED_PROVIDERS
        if not has_key:
            note = f"Add an API key for {r.provider_name} to use this model."
        elif not available:
            note = f"{r.provider_name} has no embedding runtime wired up yet."
        elif inferred:
            # Honest about WHY it is here, and about the one thing this
            # cannot check: that the provider it was filed under really
            # serves this model id.
            note = (f"Listed from its model id — it is registered under types "
                    f"{r.types or []}, not \"Text Embedding\". Confirm {r.provider_name} "
                    f"actually serves \"{r.api_model_id}\"; re-add it under the right provider "
                    f"if not.")
        else:
            note = None
        options.append({
            "name": r.name, "provider": r.provider_name, "api_model_id": r.api_model_id,
            "embedding_dimension": r.embedding_dimension, "builtin": False,
            "available": available, "inferred": inferred, "types": r.types or [],
            "note": note,
        })
    # Declared models first, then inferred ones, so the list reads
    # most-trustworthy-first rather than in insertion order.
    options[1:] = sorted(options[1:], key=lambda o: (o["inferred"], not o["available"], o["name"].lower()))
    return {"models": options}


@router.get("/llm-models", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_llm_models(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Every chat/completion model selectable for an LLM, Agent or
    parameter-extraction node, across all providers — the same shape and
    the same rules as list_embedding_models above, because these are the
    same question asked about a different model type.

    The gap this closes: the Builder's model dropdown was a hardcoded
    list of five names (three Anthropic, two OpenAI) with a single
    "(default — not yet wired to a real distinct API call)" placeholder
    standing in for every other provider. Three things were wrong with
    that at once.

    First, models registered from the Integrations page were invisible
    to it — the field is labelled "from Model catalog" and did not read
    the catalog, so a workspace with gemini-2.5-pro, deepseek-chat and
    command-r configured could select none of them.

    Second, the placeholder was no longer true. DeepSeek, Cohere, Gemini
    and Hugging Face Hub all reach a real adapter through
    _OPENAI_COMPATIBLE_BASES, and Replicate has _call_replicate; only
    Amazon Bedrock is genuinely unwired. RUNTIME_SUPPORTED_PROVIDERS is
    read here rather than restated, so this list cannot drift from what
    call_llm can actually execute — which is exactly how the hardcoded
    version came to be wrong.

    Third, and worse than cosmetic: the placeholder's own value
    ("DeepSeek (default)") is not a built-in name and not a registered
    one, so selecting it made call_llm raise "Unrecognized model" at run
    time. The single option those providers offered was guaranteed to
    fail.

    Availability is reported, never enforced by omission: a model whose
    provider has no key is still listed, flagged, with a note saying
    what to do. Hiding it would reproduce the original complaint — a
    configured model that does not appear, and nothing explaining why.
    """
    from ..model_adapter import (_MODEL_MAP, _MODEL_PROVIDERS, RUNTIME_SUPPORTED_PROVIDERS,
                                 provider_display_name)
    from ..embeddings import is_embedding_model
    from ..model_capabilities import provider_capabilities

    providers = {p.name: p for p in session.exec(select(ModelProvider)).all()}

    def _availability(provider_name: str) -> tuple:
        mp = providers.get(provider_name)
        has_key = bool(mp and mp.api_key_configured)
        wired = provider_name in RUNTIME_SUPPORTED_PROVIDERS
        if not wired:
            return False, f"{provider_name} has no execution adapter wired up yet."
        if not has_key:
            return False, f"Add an API key for {provider_name} to use this model."
        return True, None

    options = []
    for name, kind in _MODEL_PROVIDERS.items():
        display = provider_display_name(kind)
        available, note = _availability(display)
        options.append({
            "name": name, "provider": display, "api_model_id": _MODEL_MAP.get(name),
            "builtin": True, "available": available, "inferred": False,
            "types": ["LLM"], "note": note, **provider_capabilities(display),
        })

    for r in session.exec(select(ProviderModel)).all():
        types = r.types or []
        if "LLM" not in types:
            continue
        # A registered model can carry the wrong type — the Add-model
        # form's types are checkboxes, and "LLM" is the default. An
        # embedding model left at that default (intfloat/e5-base-v2) must
        # not be offered as a chat model: it would be selected, called,
        # and fail in a way that looks like a provider outage. The same
        # predicate the embedding list uses, so the two cannot disagree
        # about which side a model belongs on.
        looks_like_embedding, _inferred = is_embedding_model(r.name, r.api_model_id, types)
        if looks_like_embedding:
            continue
        available, note = _availability(r.provider_name)
        options.append({
            "name": r.name, "provider": r.provider_name, "api_model_id": r.api_model_id,
            "builtin": False, "available": available, "inferred": False,
            "types": types, "note": note, **provider_capabilities(r.provider_name),
        })

    # Every LLM provider from Integrations shows up as a group, even one
    # with no models registered yet (Amazon Bedrock, Azure, ...): a
    # disabled placeholder says what to do instead of the provider being
    # silently absent from the picker.
    listed = {o["provider"] for o in options}
    for mp in providers.values():
        if "LLM" in (mp.types or []) and mp.name not in listed:
            # An unwired provider (Amazon Bedrock) says so: registering a
            # model there would not make it runnable.
            wired = mp.name in RUNTIME_SUPPORTED_PROVIDERS
            options.append({
                "name": f"{mp.name} — no models yet", "provider": mp.name, "api_model_id": None,
                "builtin": False, "available": False, "inferred": False, "types": ["LLM"],
                "placeholder": True,
                "note": (f"Register a model for {mp.name} in Integrations > Model Providers to use it here."
                         if wired else f"{mp.name} has no execution adapter wired up yet."),
                **provider_capabilities(mp.name),
            })

    # Available first, then by provider, then by name: the list reads
    # usable-first rather than in table order.
    options.sort(key=lambda o: (not o["available"], (o["provider"] or "").lower(), o["name"].lower()))
    return {"models": options + _mlstudio_model_options(session)}


def _mlstudio_model_options(session: Session) -> List[Dict[str, Any]]:
    """Models deployed in ML Studio, as one "ML Studio" group at the end of
    the picker. LLM deployments run (mlstudio_client / model_adapter) once
    their endpoint's client secret is stored; classical ML models are listed
    disabled — they take feature columns, not a prompt."""
    from .. import mlstudio_client
    name = mlstudio_client.PROVIDER_NAME
    no_tools = {"supports_chat": True, "supports_tool_calling": False, "supports_vision": False}
    try:
        deployments = mlstudio_client.list_deployments()
    except mlstudio_client.MlStudioError as e:
        return [{
            "name": "ML Studio — unavailable", "provider": name, "api_model_id": None, "builtin": False,
            "available": False, "inferred": False, "types": ["LLM"], "placeholder": True,
            "note": f"Couldn't load ML Studio models: {e}", **no_tools,
        }]
    stored = {c.model_id for c in session.exec(select(MlStudioCredential)).all()}
    out: List[Dict[str, Any]] = []
    for d in deployments:
        mid = d.get("modelId")
        if not mid:
            continue
        is_llm = d.get("domain") == "llm"
        active = (d.get("endpointStatus") or "active") == "active"
        if not is_llm:
            available, note = False, (f"{d.get('task') or 'Classical ML'} model — it takes feature columns, "
                                      f"not a prompt, so it can't be an LLM node's model.")
        elif not active:
            available, note = False, "Its ML Studio endpoint isn't active."
        elif mid not in stored:
            available, note = False, ("Add this endpoint's client secret in Integrations > Model Providers > "
                                      "ML Studio models to use it.")
        else:
            available, note = True, None
        out.append({
            "name": f"{mlstudio_client.MODEL_PREFIX}{mid}", "provider": name, "api_model_id": mid,
            "builtin": False, "available": available, "inferred": False,
            "types": ["LLM"] if is_llm else ["ML"], "note": note,
            "mlstudio": {"model_id": mid, "domain": d.get("domain"), "task": d.get("task"),
                         "version": d.get("currentVersion"), "deployed_by": d.get("deployedBy")},
            **no_tools,
        })
    out.sort(key=lambda o: (not o["available"], o["types"] != ["LLM"], o["name"].lower()))
    return out


class MlStudioCredentialIn(BaseModel):
    client_secret: str


@router.get("/mlstudio-credentials", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO))])
def list_mlstudio_credentials(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Which ML Studio models have an endpoint client secret stored. The
    secrets themselves are never returned."""
    rows = session.exec(select(MlStudioCredential)).all()
    return {"models": sorted(r.model_id for r in rows)}


@router.put("/mlstudio-credentials/{model_id}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def set_mlstudio_credential(model_id: str, body: MlStudioCredentialIn,
                            session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Store (or replace) one ML Studio endpoint's client secret, so LLM nodes
    can run that model. Encrypted at rest; write-only."""
    secret = (body.client_secret or "").strip()
    model_id = model_id.strip()
    if not model_id or not secret:
        raise HTTPException(400, "model id and client secret are required")
    _require_durable_secret_key(f"the client secret for ML Studio model {model_id}")
    row = session.get(MlStudioCredential, model_id) or MlStudioCredential(model_id=model_id, client_secret="")
    row.client_secret = encrypt_secret(secret)
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    return {"model_id": model_id, "configured": True}


@router.delete("/mlstudio-credentials/{model_id}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def delete_mlstudio_credential(model_id: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    row = session.get(MlStudioCredential, model_id)
    if row:
        session.delete(row)
        session.commit()
    return {"model_id": model_id, "configured": False}


@router.get("/agent-tool-catalog", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def agent_tool_catalog(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Everything the Agent node's Tools picker can list, with whether the
    runtime can actually call it. The picker shows this rather than guessing:
    an entry with callable=false carries the reason and can't be added."""
    from ..models import Agent
    from ..tool_connectors import is_builtin_connector
    entries = []
    actions = session.exec(select(ToolAction)).all()
    tools = {t.name: t for t in session.exec(select(Tool)).all()}
    for a in sorted(actions, key=lambda x: (x.tool_name.lower(), x.name.lower())):
        if a.tool_name not in tools:
            continue
        value = f"tool:{a.tool_name}:{a.name}"
        ok = is_builtin_connector(value)
        entries.append({
            "value": value, "label": a.name, "group": a.tool_name, "source_type": "tool", "desc": a.desc,
            "callable": ok,
            "reason": None if ok else "No runtime connector is implemented for this action yet.",
            "params": a.params or [],
        })
    for m in session.exec(select(MCPServer)).all():
        entries.append({
            "value": f"mcp:{m.identifier}", "label": m.name, "group": "MCP servers", "source_type": "mcp",
            "desc": f"{m.actions} actions", "callable": False,
            "reason": "MCP servers can't be called from an Agent node yet.", "params": [],
        })
    for ag in session.exec(select(Agent).where(Agent.plane == "App")).all():
        entries.append({
            "value": f"workflow:{ag.id}", "label": ag.name, "group": "Workflows", "source_type": "workflow",
            "desc": ag.description or "", "callable": False,
            "reason": "Workflows can't be called from an Agent node yet. Use a Sub-Agent node.", "params": [],
        })
    return {"tools": entries}


@router.get("/rerank-availability", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def rerank_availability(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Whether turning on Rerank Model will genuinely rerank anything.

    The RAG Settings card used to state this in a hardcoded amber
    warning that rendered whether or not a key existed, alongside a
    "Check config" chip that was a plain <span> with no handler. So a
    workspace with a perfectly good Anthropic key was still shown a
    warning telling it reranking would fall back to Weighted Score —
    the caption asserted a state nobody had checked.

    The real availability rule lives in retrieval._try_llm_rerank_note
    and is reused here rather than restated, so this endpoint cannot
    drift from what retrieval actually does at query time.

    Any Rerank-type models registered on this page are reported too,
    but explicitly as not wired: the only implemented reranker is the
    LLM one below. A dedicated reranking API (Cohere's rerank, a
    cross-encoder) has no execution path here, and listing one as
    selectable would be the same empty-control problem again.
    """
    from ..retrieval import _try_llm_rerank_note
    import os

    note = _try_llm_rerank_note()
    available = note is None
    source = None
    if available:
        source = "environment" if os.getenv("ANTHROPIC_API_KEY") else "Model Providers"

    catalogued = [
        {"name": r.name, "provider": r.provider_name, "api_model_id": r.api_model_id}
        for r in session.exec(select(ProviderModel)).all()
        if "Rerank" in (r.types or [])
    ]
    return {
        "available": available,
        # Where the credential was found, so "it works" is checkable
        # rather than something the UI just claims.
        "source": source,
        "kind": "llm" if available else None,
        "reason": note,
        "catalogued_rerank_models": catalogued,
        "models": _rerank_model_options(session),
    }


class StorageDestinationIn(BaseModel):
    """New upload destination. `name` is optional — a bucket name is
    already a serviceable label."""
    name: Optional[str] = None
    bucket: str
    region: Optional[str] = None
    # For MinIO, R2, Wasabi and other S3-compatible services; leave unset
    # for real AWS.
    endpoint_url: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    # Whether new uploads should go here from now on. The dialog's own
    # "use these for future uploads too" choice.
    make_default: bool = True


class StorageDestinationTestIn(StorageDestinationIn):
    """Same fields, tested without being stored."""
    bucket: str = ""


@router.get("/storage-target", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def storage_target(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Where an uploaded file itself would be kept, and what else it
    could be sent to instead.

    The Documents tab offers "also keep the original file", and that
    control has to say where the file is actually going: "kept in S3"
    and "kept on this server's disk" are different promises, and only
    the server knows which is configured. Everything here is read from
    the real configuration rather than assumed in the UI, so the label
    cannot claim a bucket nobody set.

    `destinations` is what the change-destination dialog lists: the
    server's own .env configuration first (not editable from here — it
    lives on the server), then every destination someone has added.
    """
    from .. import storage_destinations as sd

    env = sd.env_summary()
    stored = [sd.describe(d) for d in session.exec(select(StorageDestination)).all()]
    active_default = sd.default_destination(session)
    # .env is only "the default" when no stored destination claims it.
    env["is_default"] = active_default is None
    current = sd.describe(active_default) if active_default else env

    return {
        # The shape the existing callers already read — the active
        # destination, flattened.
        "backend": current.get("provider", "local"),
        "configured": current.get("configured", False),
        "label": current.get("label"),
        "bucket": current.get("bucket"),
        "endpoint_url": current.get("endpoint_url"),
        "retention_days": int(os.getenv("UPLOAD_DEFAULT_TTL_DAYS", "30")),
        # What the dialog needs on top of that.
        "destination_id": current.get("id"),
        "destinations": [env] + stored,
        # Adding a destination stores credentials, which is refused on a
        # throwaway encryption key — say so before someone types a
        # secret into a form that cannot keep it.
        "can_store_credentials": not using_ephemeral_key(),
    }


@router.post("/storage-destinations/test", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def test_storage_destination(body: StorageDestinationTestIn) -> Dict[str, Any]:
    """Really write, read back and delete one small object in the given
    bucket, with the given credentials, and report what happened.

    Nothing weaker is worth offering: head_bucket succeeds for a bucket
    this key cannot write to, and a key can be perfectly valid while the
    policy refuses PutObject. This is the same call the upload itself
    will make, so a pass here means the next upload works.
    """
    from .. import storage_destinations as sd

    if not (body.bucket or "").strip():
        raise HTTPException(400, "A bucket name is required.")
    try:
        backend = sd.build_unsaved(
            bucket=body.bucket, region=body.region, endpoint_url=body.endpoint_url,
            access_key_id=body.access_key_id, secret_access_key=body.secret_access_key,
            session_token=body.session_token,
        )
    except Exception as e:
        return {"ok": False, "message": str(e)}
    ok, message = sd.probe(backend)
    return {"ok": ok, "message": message}


@router.post("/storage-destinations", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def create_storage_destination(body: StorageDestinationIn,
                               session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Store a destination after proving it works.

    Tested before it is saved, deliberately: a destination that is
    listed but broken is worse than none, because the failure then
    surfaces at the end of somebody's upload instead of in the dialog
    they are already looking at.
    """
    from .. import storage_destinations as sd

    if not (body.bucket or "").strip():
        raise HTTPException(400, "A bucket name is required.")
    try:
        backend = sd.build_unsaved(
            bucket=body.bucket, region=body.region, endpoint_url=body.endpoint_url,
            access_key_id=body.access_key_id, secret_access_key=body.secret_access_key,
            session_token=body.session_token,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    ok, message = sd.probe(backend)
    if not ok:
        raise HTTPException(
            400,
            f"These credentials could not write to '{body.bucket}': {message}. "
            f"Nothing was saved.",
        )
    try:
        dest = sd.create(
            session, name=body.name or "", bucket=body.bucket, region=body.region,
            endpoint_url=body.endpoint_url, access_key_id=body.access_key_id,
            secret_access_key=body.secret_access_key, session_token=body.session_token,
            make_default=body.make_default, verified=message,
        )
    except RuntimeError as e:
        # require_durable_key refuses on an ephemeral encryption key.
        raise HTTPException(503, str(e))
    return {**sd.describe(dest), "verified": message}


@router.post("/storage-destinations/{destination_id}/default", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def set_default_storage_destination(destination_id: str,
                                    session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Send new uploads here from now on. Files already written stay
    exactly where they are, and are still read back from there."""
    from .. import storage_destinations as sd
    try:
        sd.set_default(session, destination_id)
    except sd.DestinationError as e:
        raise HTTPException(404, str(e))
    return storage_target(session)


@router.post("/storage-destinations/{destination_id}/test", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def test_stored_destination(destination_id: str,
                            session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Re-test a destination already saved — credentials expire, buckets
    get new policies, and a stored row is not evidence of either."""
    from .. import storage_destinations as sd
    dest = session.get(StorageDestination, destination_id)
    if not dest:
        raise HTTPException(404, f"No upload destination with id '{destination_id}'.")
    try:
        backend = sd.backend_for(dest)
    except sd.DestinationError as e:
        ok, message = False, str(e)
    else:
        ok, message = sd.probe(backend)
    dest.last_checked_at = datetime.utcnow()
    dest.last_error = None if ok else message[:1000]
    session.add(dest)
    session.commit()
    return {"ok": ok, "message": message, **sd.describe(dest)}


@router.delete("/storage-destinations/{destination_id}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def delete_storage_destination(destination_id: str,
                               session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Forget a destination. Refused while any stored file still lives
    there: deleting the row would leave those objects unreachable — the
    platform would hold a download link it can no longer resolve, and a
    delete it can no longer perform."""
    from .. import storage_destinations as sd
    from ..models import UploadedFile

    dest = session.get(StorageDestination, destination_id)
    if not dest:
        raise HTTPException(404, f"No upload destination with id '{destination_id}'.")
    in_use = session.exec(
        select(UploadedFile).where(UploadedFile.storage_destination_id == destination_id)
    ).all()
    if in_use:
        raise HTTPException(
            409,
            f"{len(in_use)} stored file(s) still live in '{dest.name}'. Delete those documents "
            f"first — removing this destination would leave their bytes unreachable.",
        )
    session.delete(dest)
    session.commit()
    sd.forget(destination_id)
    return {"deleted": destination_id}


@router.get("/transcription-availability", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def transcription_availability() -> Dict[str, Any]:
    """Whether an audio file can genuinely be ingested right now.

    Audio has no text layer, so ingesting a .wav means transcribing it,
    which means a real speech provider with a real key. The Documents
    tab asks this before offering audio at all: a picker that lists a
    .wav on a deployment with no key configured would accept the upload
    and fail at the end of it, having explained nothing.

    The answer comes from transcription.availability(), the same
    function ingestion.parse_file's own transcribe path uses, so this
    cannot claim a capability the upload then refuses.
    """
    from ..transcription import availability
    # refresh=True: this endpoint is the one the UI calls right after
    # someone adds a key, which is exactly when a 30-second cache would
    # answer with the state from before they added it.
    return availability(refresh=True).as_dict()


def _rerank_model_options(session: Session) -> List[Dict[str, Any]]:
    """Models that can genuinely do the reranking, for the RAG Settings
    dropdown.

    Reranking here works by asking a chat model to score each candidate
    0-10 and re-sorting (retrieval._llm_rerank), so the usable set is
    every LLM this platform can actually execute — the built-in catalog
    plus anything registered on this page — not just Anthropic's.

    Deliberately NOT listed: models under a provider with no runtime
    (Amazon Bedrock, Replicate) and models registered as type "Rerank"
    for a dedicated rerank API (Cohere's /rerank, a cross-encoder).
    Those have no execution path here, and offering one would be a
    control that silently changes nothing — the exact problem the old
    hardcoded "Check config" box had.
    """
    import inspect
    from ..model_adapter import (
        RUNTIME_SUPPORTED_PROVIDERS, _MODEL_PROVIDERS, call_llm, provider_display_name,
    )
    from ..embeddings import is_embedding_model

    # Read off call_llm's own signature rather than restating it, so the
    # name shown as "Platform default" cannot drift from the model that
    # actually runs when no override is saved.
    default_model = inspect.signature(call_llm).parameters["model"].default

    providers = {p.name: p for p in session.exec(select(ModelProvider)).all()}

    def entry(name: str, provider: str, builtin: bool) -> Dict[str, Any]:
        mp = providers.get(provider)
        has_key = bool(mp and mp.api_key_configured)
        return {
            "name": name, "provider": provider, "builtin": builtin,
            "available": has_key,
            "note": None if has_key else f"Add an API key for {provider} to use this model.",
        }

    options: List[Dict[str, Any]] = [{
        "name": "", "provider": None, "builtin": True, "available": True,
        "note": f"Platform default ({default_model}).",
    }]
    seen = {""}
    for name, kind in sorted(_MODEL_PROVIDERS.items()):
        provider = provider_display_name(kind)
        if provider in RUNTIME_SUPPORTED_PROVIDERS and name not in seen:
            options.append(entry(name, provider, True))
            seen.add(name)
    for r in session.exec(select(ProviderModel)).all():
        if r.name in seen or r.provider_name not in RUNTIME_SUPPORTED_PROVIDERS:
            continue
        # An embedding model cannot score relevance in prose; listing one
        # would just fail at query time.
        if is_embedding_model(r.name, r.api_model_id, r.types)[0]:
            continue
        options.append(entry(r.name, r.provider_name, False))
        seen.add(r.name)
    # Usable first, then alphabetical; the default entry stays pinned.
    options[1:] = sorted(options[1:], key=lambda o: (not o["available"], o["name"].lower()))
    return options


class LocalPathProbeIn(BaseModel):
    """Either a path relative to a picked folder
    ("audio/incident_review_INC-88421.vtt" — first segment is the
    folder's own name), or a bare filename from the single-file picker
    ("USER_GUIDE.md"), which is all that picker ever exposes.

    `size` is the browser-reported byte size. It is what makes a bare
    filename resolvable in practice: a name alone can match several
    files, but name + exact size almost never does.
    """
    relative_path: str
    size: Optional[int] = None


@router.post("/resolve-local-path", dependencies=[Depends(require_any_permission(P.STUDIO, P.RAG, P.INTEGRATIONS))])
def resolve_local_path(body: LocalPathProbeIn) -> Dict[str, Any]:
    """Where a browser-picked folder actually lives, found by looking on
    this server's own filesystem.

    A directory pick gives `webkitRelativePath` — real nested structure —
    but never the absolute root, and no browser API will ever provide
    it: File carries only `name` and `input.value` is the literal
    "C:\\fakepath\\<name>", deliberately, because a page able to read
    your filesystem layout would be a genuine privacy leak. Guessing the
    root produced things like `/Users/audio/file.vtt`, which looks
    absolute and points nowhere.

    So this asks the machine instead of guessing. It only works when the
    API is running on the SAME machine as the browser — true for local
    development, false for the containerised api (a different
    filesystem) and false for any remote deployment. When it cannot
    find the folder it says so, and the caller keeps its honest
    "assumed, not known" wording rather than inventing a root.

    Scope and bounds, deliberately narrow: read-only, searches only
    inside this process's own working directory and the home directory,
    skips hidden/system/dependency folders, and gives up after a fixed
    number of directories and seconds. It returns a path only when the
    ENTIRE relative path it was given exists there, so a bare directory
    name cannot be used to fish for unrelated locations.
    """
    import os
    import time
    from pathlib import Path

    rel = (body.relative_path or "").strip().strip("/")
    if not rel:
        raise HTTPException(400, "relative_path is required")
    if ".." in Path(rel).parts:
        raise HTTPException(400, "relative_path must not contain '..'")

    parts = Path(rel).parts
    folder_name = parts[0]
    # A single segment is a bare filename from the single-file picker;
    # anything longer came from a directory pick and carries its own
    # folder structure to verify against.
    single_file = len(parts) == 1
    # Search budget. A bind-mounted macOS/Windows filesystem is an order
    # of magnitude slower to walk than a native one — the first attempt
    # here exhausted a 2-second budget after FOUR directories — so a
    # mount gets a much larger allowance. Still bounded: this runs while
    # someone waits for a snippet to update.
    mounted = bool((os.getenv("HOST_FILES_MOUNT") or "").strip()) and \
        os.path.isdir((os.getenv("HOST_FILES_MOUNT") or "").strip())
    deadline = time.monotonic() + (20.0 if mounted else 2.0)
    max_dirs = 60000 if mounted else 20000
    skip = {"node_modules", ".git", "Library", "Applications", "System",
            ".venv", "venv", "__pycache__", ".cache", "dist", "build",
            ".Trash", ".npm", ".docker", "Music", "Movies", "Pictures",
            "Photos Library.photoslibrary", ".Trashes", "site-packages",
            ".pyenv", ".rustup", ".cargo", ".gradle", ".m2", "vendor"}

    # A read-only mount of the caller's machine, when one is configured
    # (HOST_FILES_MOUNT/HOST_FILES_PATH — see docker-compose.yml). It is
    # searched FIRST because it is the filesystem the browser is actually
    # picking files from; cwd and home inside a container are /app and
    # /root, which hold nothing a person chose.
    mount = (os.getenv("HOST_FILES_MOUNT") or "").strip()
    host_prefix = (os.getenv("HOST_FILES_PATH") or "").strip()

    roots: List[Path] = []
    candidates = []
    if mount and os.path.isdir(mount):
        # The places people actually keep files, before the whole tree.
        # Walking a mounted home directory top-down reaches Desktop only
        # after Applications and Library, which is where the budget goes.
        for likely in ("Desktop", "Documents", "Downloads", "Projects", "src", "code"):
            sub = Path(mount) / likely
            if sub.is_dir():
                candidates.append(sub)
        candidates.append(Path(mount))
    candidates += [Path.cwd(), Path.home()]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)

    def as_host_path(p: Path) -> str:
        """A path the CALLER can use, not the container's view of it.

        A match under the mount is reported with the mount prefix
        rewritten back to HOST_FILES_PATH, because '/host-files/Desktop/x'
        means nothing in a curl command run on the person's own machine —
        the whole point of showing the path is that it can be copied and
        used there.
        """
        text = str(p)
        if mount and host_prefix and (text == mount or text.startswith(mount + os.sep)):
            return host_prefix.rstrip(os.sep) + text[len(mount):]
        return text

    scanned = 0
    matches: List[Dict[str, Any]] = []
    max_matches = 12

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            scanned += 1
            if scanned > max_dirs or time.monotonic() > deadline:
                break
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]

            if single_file:
                if folder_name not in filenames:
                    continue
                candidate = Path(dirpath) / folder_name
                try:
                    actual_size = candidate.stat().st_size
                except OSError:
                    continue
                # A name can repeat across a filesystem; a name AND an
                # exact byte size almost never does. Without a size to
                # compare, every same-named file is reported rather than
                # one being picked arbitrarily.
                if body.size is not None and actual_size != body.size:
                    continue
                entry = {"root": as_host_path(candidate.parent),
                         "absolute_path": as_host_path(candidate),
                         "size": actual_size}
                if entry not in matches:
                    matches.append(entry)
                if len(matches) >= max_matches:
                    break
                continue

            if folder_name not in dirnames:
                continue
            picked = Path(dirpath) / folder_name
            # Only a match if everything below it is really there.
            if not (picked.parent / Path(rel)).exists():
                continue
            return {
                "found": True,
                # The root the caller needs: the picked folder's PARENT,
                # so root + webkitRelativePath is the absolute path.
                "root": as_host_path(picked.parent),
                "absolute_path": as_host_path((picked.parent / Path(rel)).resolve()),
                "scanned": scanned,
            }
        if len(matches) >= max_matches:
            break

    if len(matches) == 1:
        return {"found": True, **matches[0], "scanned": scanned}
    if matches:
        # Ambiguous on purpose: guessing one of several real files would
        # put a plausible but wrong path in a command someone then runs.
        return {"found": False, "ambiguous": True, "matches": matches,
                "reason": f"{len(matches)} files named '{folder_name}' with this size — "
                          f"pick which one you meant",
                "scanned": scanned}
    if scanned > max_dirs or time.monotonic() > deadline:
        return {"found": False, "reason": "search budget exhausted", "scanned": scanned}

    # Distinguish "this file is not here" from "this server can never see
    # your files". A containerised API has its own filesystem (/app,
    # /root), so no local path will EVER resolve through it — telling
    # someone the file was not found invites them to go looking for a
    # file that is exactly where they thought it was. The whole
    # explanation is which process is answering.
    # A container WITH a host mount is no longer blind, so the
    # "runs in a container" explanation would be wrong — the file
    # genuinely was not found under what was mounted.
    containerised = os.path.exists("/.dockerenv") and not (mount and os.path.isdir(mount))
    if containerised:
        return {
            "found": False, "containerised": True, "scanned": scanned,
            "reason": "This API is running inside a container, so it cannot see your machine's "
                      "filesystem and no local path can be resolved through it. Start the local "
                      "dev server (run-dev.sh) to get absolute paths, or use 'Pick a folder', "
                      "which gets its paths from the browser instead.",
        }
    return {"found": False, "containerised": False, "scanned": scanned,
            "reason": f"No file by that name was found under {Path.cwd()} or {Path.home()}."}


# ---------------- tools ----------------

@router.get("/tools", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_tools(session: Session = Depends(get_session)) -> List[Tool]:
    return session.exec(select(Tool)).all()


@router.get("/tools/{name}/actions", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def get_tool_actions(name: str, session: Session = Depends(get_session)) -> List[ToolAction]:
    return session.exec(select(ToolAction).where(ToolAction.tool_name == name)).all()


# ---------------- MCP servers ----------------

class NewMcpServerIn(BaseModel):
    name: str
    identifier: str
    url: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    use_dynamic_registration: bool = True


@router.get("/mcp-servers", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_mcp_servers(session: Session = Depends(get_session)) -> List[MCPServer]:
    return session.exec(select(MCPServer)).all()


@router.post("/mcp-servers", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def add_mcp_server(body: NewMcpServerIn, session: Session = Depends(get_session)) -> MCPServer:
    """STUB: doesn't perform a real OAuth handshake against the MCP server's
    HTTP endpoint. Registers it as 'connected' with a placeholder action
    count — a real implementation would call the server's discovery
    endpoint to enumerate actions."""
    existing = session.get(MCPServer, body.identifier)
    if existing:
        raise HTTPException(400, "A server with this identifier already exists")
    server = MCPServer(id=body.identifier, name=body.name, identifier=body.identifier,
                        url=body.url, status="connected", actions=0)
    session.add(server)
    session.commit()
    session.refresh(server)
    return server


# ---------------- data sources ----------------

DS_MASK = "••••••••"
# Anything that must never leave the server in plaintext once stored.
# Mirrors SENSITIVE_KEYS in knowledge_bases.py: encrypted with Fernet
# at rest, replaced with DS_MASK on every read, and re-used unchanged
# when a later save sends the mask back instead of a new value.
DS_SENSITIVE_KEYS = {"client_secret", "credentials"}


class OAuthConfigIn(BaseModel):
    # Optional because the two vendors behind these sources need
    # different things: Microsoft takes a client id + secret
    # (client_credentials), while Google has no client_credentials grant
    # at all and can only act without a signed-in user through a
    # service-account key, supplied here as `credentials`.
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    sharepoint_subdomain: Optional[str] = None
    tenant_id: Optional[str] = None
    credentials: Optional[str] = None
    impersonate_subject: Optional[str] = None
    # One user's OneDrive, by email/UPN. Addresses /users/{upn}/drive
    # directly, so browsing needs no User.Read.All over the directory.
    onedrive_user: Optional[str] = None


class ApiKeyDsConfigIn(BaseModel):
    authorization_name: Optional[str] = None
    credentials: str
    bucket_names: Optional[str] = None
    who_can_use: str = "All team members"
    # Google service accounts can only see a real user's Drive through
    # domain-wide delegation, which needs the user to impersonate.
    impersonate_subject: Optional[str] = None


def _normalized_subdomain(raw: Optional[str]) -> Optional[str]:
    """The SharePoint/OneDrive tenant subdomain, validated.

    This becomes a hostname — "<sub>.sharepoint.com" — so a value with a
    space in it ("aadhyaanalytics com") builds an address that cannot
    resolve, and the failure appears much later as an opaque Graph error
    about something else entirely. Rejecting it here, where the person
    can see the field, is the only place the mistake is obvious.

    Accepts what people actually paste and trims it to the label:
    "contoso", "contoso.sharepoint.com", or a full URL all yield
    "contoso".
    """
    if raw is None:
        return None
    value = raw.strip().rstrip("/")
    if not value:
        return None
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]
    if value.lower().endswith(".sharepoint.com"):
        value = value[: -len(".sharepoint.com")]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,63}", value):
        raise HTTPException(
            400,
            f"'{raw}' is not a usable SharePoint subdomain. It becomes the hostname "
            f"'<subdomain>.sharepoint.com', so it can only contain letters, digits and "
            f"hyphens — no spaces or dots. For https://contoso.sharepoint.com enter 'contoso'.",
        )
    return value


def _ds_masked(ds: DataSource) -> Dict[str, Any]:
    cfg = {k: (DS_MASK if k in DS_SENSITIVE_KEYS and v else v)
           for k, v in (ds.config or {}).items()}
    out = {"name": ds.name, "version": ds.version, "status": ds.status,
           "auth": ds.auth, "config": cfg}
    # The service account's own address, for the Google sources. Not a
    # secret — it is an identity, and Google publishes it on the key's
    # own project page — but it IS the one thing someone needs in order
    # to share a Drive folder with this platform, and masking the key
    # buried it. A Drive that reads as empty is almost always a folder
    # nobody has shared with this address yet.
    email = _service_account_field(ds, "client_email")
    if email:
        out["service_account_email"] = email
    # The NUMERIC OAuth client id, which is what Google Workspace's
    # domain-wide delegation screen asks for — it will not accept the
    # email address. Sharing a folder with the service account is
    # refused outright on a Workspace domain with external sharing
    # locked down, which makes delegation the only route, which makes
    # this number necessary. It was only inside the masked key.
    client_id = _service_account_field(ds, "client_id")
    if client_id:
        out["service_account_client_id"] = client_id
    return out


def _service_account_email(ds: DataSource) -> Optional[str]:
    """The identity a stored service-account key authenticates as."""
    return _service_account_field(ds, "client_email")


def _service_account_field(ds: DataSource, field: str) -> Optional[str]:
    """One non-secret field out of a stored service-account key. Never
    touches private_key, and returns None for the many sources whose
    credential is a plain token rather than a JSON key."""
    raw = (ds.config or {}).get("credentials")
    if not raw:
        return None
    try:
        from ..secrets_crypto import decrypt_secret
        import json as _json
        plain = decrypt_secret(raw) if isinstance(raw, str) and raw.startswith("enc:") else raw
        data = _json.loads(plain) if isinstance(plain, str) else plain
        value = (data or {}).get(field)
        return str(value) if value else None
    except Exception:
        # A non-JSON credential (a GitHub token, a Notion key) is the
        # normal case for most sources, not an error worth surfacing.
        return None


def _ds_store(ds: DataSource, incoming: Dict[str, Any]) -> None:
    """Encrypts the secret fields and preserves any the caller sent back
    as the mask, so re-saving a form that only changed the subdomain
    doesn't wipe the secret the user never retyped."""
    # Same reasoning as the provider key path: a client secret encrypted
    # under a throwaway key reads as connected and then fails on every
    # browse, with the cause nowhere in sight.
    if any(k in DS_SENSITIVE_KEYS and v and v != DS_MASK for k, v in incoming.items()):
        _require_durable_secret_key(f"credentials for {ds.name}")
    existing = ds.config or {}
    cfg: Dict[str, Any] = {}
    for key, value in incoming.items():
        if value is None:
            continue
        if key in DS_SENSITIVE_KEYS:
            if value == DS_MASK:
                if key in existing:
                    cfg[key] = existing[key]
                continue
            cfg[key] = encrypt_secret(str(value))
        else:
            cfg[key] = value
    ds.config = cfg


def _ds_plain_config(ds: DataSource) -> Dict[str, Any]:
    """Decrypted config for server-side use only — never returned."""
    from ..secrets_crypto import decrypt_secret
    cfg = dict(ds.config or {})
    for key in DS_SENSITIVE_KEYS:
        if cfg.get(key):
            cfg[key] = decrypt_secret(cfg[key])
    return cfg


@router.get("/data-sources", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def list_data_sources(session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    """Config comes back with secrets masked. `browsable` says whether
    documents can actually be listed and ingested from this source —
    status alone used to claim "connected" with nothing stored behind
    it, so the Documents tab keys off this instead."""
    from ..secrets_crypto import decrypt_secret, SecretDecryptionError

    def credentials_usable(ds: DataSource) -> Optional[bool]:
        """Whether the stored secrets can be READ, not just whether they
        exist. A secret encrypted under a different SECRETS_ENCRYPTION_KEY
        still shows as a masked value and still reads as connected, while
        every browse fails — with a raw crypto error that says nothing
        about what to do. None when there is nothing stored to test."""
        stored = [(ds.config or {}).get(k) for k in DS_SENSITIVE_KEYS]
        stored = [v for v in stored if v]
        if not stored:
            return None
        for value in stored:
            try:
                decrypt_secret(value)
            except Exception:
                return False
        return True

    rows = session.exec(select(DataSource)).all()
    out = []
    for ds in rows:
        item = _ds_masked(ds)
        usable = credentials_usable(ds)
        item["credentials_usable"] = usable
        # Unreadable credentials are not browsable, however "connected"
        # the row looks — otherwise the dropdown offers a source that
        # fails on every click.
        item["browsable"] = bool(ds.status == "connected"
                                 and ds.name in data_sources.SUPPORTED
                                 and (ds.config or {})
                                 and usable is not False)
        item["browse_supported"] = ds.name in data_sources.SUPPORTED
        out.append(item)
    return out


@router.post("/data-sources/{name}/oauth", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def configure_data_source_oauth(name: str, body: OAuthConfigIn,
                                session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Stores the client credentials this form has always collected.

    Previously it validated nothing, stored nothing, and set
    status="connected" — so a source could read as connected with no
    credential anywhere in the database, and nothing could ever be
    ingested from it.

    "oauth" here is the client_credentials grant, not a redirect flow:
    the client ID and secret the user pastes are exchanged directly for
    an app-only token at browse time (see data_sources.py). That is why
    there is no consent screen to send anyone through. The trade-off is
    that an app-only token sees only what the registered app has
    *application* permissions for — a personal OneDrive has no app-only
    drive at all, which is why a site/tenant subdomain is needed there.

    The credentials are not verified against the provider here; the
    first browse makes the real call and reports the provider's own
    error verbatim."""
    ds = session.get(DataSource, name)
    if not ds:
        raise HTTPException(404, "Data source not found")
    if ds.auth != "oauth":
        raise HTTPException(400, "This data source uses API-key auth, not OAuth")
    existing = ds.config or {}
    has_client = (body.client_id or existing.get("client_id")) and \
                 (body.client_secret or existing.get("client_secret"))
    has_sa = body.credentials or existing.get("credentials")
    if not has_client and not has_sa:
        raise HTTPException(
            400,
            "Provide a client ID and client secret (Microsoft) or a service-account JSON key "
            "(Google) for this source.",
        )
    _ds_store(ds, {"client_id": body.client_id, "client_secret": body.client_secret,
                   "sharepoint_subdomain": _normalized_subdomain(body.sharepoint_subdomain),
                   "tenant_id": body.tenant_id,
                   "credentials": body.credentials,
                   "impersonate_subject": body.impersonate_subject,
                   "onedrive_user": body.onedrive_user})
    ds.status = "connected"
    session.add(ds)
    session.commit()
    session.refresh(ds)
    return _ds_masked(ds)


@router.post("/data-sources/{name}/api-key", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def configure_data_source_apikey(name: str, body: ApiKeyDsConfigIn,
                                 session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Same fix as the OAuth endpoint: `credentials` is now actually
    stored (Fernet-encrypted) instead of being validated for
    non-emptiness and thrown away.

    What `credentials` holds depends on the source — a personal access
    token for GitHub, an integration token for Notion, a
    service-account JSON key for Google Cloud Storage and Google
    Drive."""
    ds = session.get(DataSource, name)
    if not ds:
        raise HTTPException(404, "Data source not found")
    if ds.auth != "apikey":
        raise HTTPException(400, "This data source uses OAuth, not an API key")
    if not body.credentials:
        raise HTTPException(400, "Credentials are required")
    if body.credentials == DS_MASK and not (ds.config or {}).get("credentials"):
        raise HTTPException(400, "Credentials are required")
    _ds_store(ds, {"authorization_name": body.authorization_name,
                   "credentials": body.credentials,
                   "bucket_names": body.bucket_names,
                   "who_can_use": body.who_can_use,
                   "impersonate_subject": body.impersonate_subject})
    ds.status = "connected"
    session.add(ds)
    session.commit()
    session.refresh(ds)
    return _ds_masked(ds)


@router.delete("/data-sources/{name}/credentials", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def disconnect_data_source(name: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Drops the stored credentials and returns the source to
    needs_auth, so a wrong key can be cleared rather than only
    overwritten."""
    ds = session.get(DataSource, name)
    if not ds:
        raise HTTPException(404, "Data source not found")
    ds.config = {}
    ds.status = "needs_auth"
    session.add(ds)
    session.commit()
    session.refresh(ds)
    return _ds_masked(ds)


@router.get("/data-sources/{name}/browse", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO, P.RAG))])
def browse_data_source(name: str, path: str = "",
                       session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Lists one level of the source. `path` is whatever `path` an
    earlier entry carried — a repo path for GitHub, a drive item id for
    SharePoint/OneDrive, a bucket prefix for GCS, a page id for Notion —
    so nesting works to any depth without the caller understanding a
    source's addressing.

    Provider errors are surfaced as 502 with the provider's own message,
    because a browse that silently returns [] is indistinguishable from
    an empty folder."""
    ds = session.get(DataSource, name)
    if not ds:
        raise HTTPException(404, "Data source not found")
    if ds.status != "connected" or not (ds.config or {}):
        raise HTTPException(400, f"{name} is not connected — add credentials on the Integrations page.")
    from ..secrets_crypto import SecretDecryptionError
    try:
        entries = data_sources.list_children(name, _ds_plain_config(ds), path)
    except SecretDecryptionError:
        # The stored secret predates the current encryption key. Saying
        # so, and what to do, beats surfacing the crypto error — which
        # is accurate but tells nobody to re-enter the credential.
        raise HTTPException(
            400,
            f"{name}'s stored credentials cannot be decrypted with this server's current "
            f"SECRETS_ENCRYPTION_KEY, so they cannot be used. They were saved by a process "
            f"using a different key — re-enter them on the Integrations page and they will "
            f"work. Nothing about {name} itself is wrong.",
        )
    except data_sources.DataSourceError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:  # network/parse failures from the provider
        raise HTTPException(502, f"{name} browse failed: {exc}")
    return {"source": name, "path": path, "entries": entries,
            # Explains the listing where the listing itself would
            # mislead — see data_sources.LAST_NOTICE.
            "notice": data_sources.take_notice(name)}


@router.get("/ucs/principals", dependencies=[Depends(require_any_permission(P.WEBAPPS, P.STUDIO, P.AGENTS_PUBLISH))])
def list_ucs_principals() -> dict:
    """The real principals UCS knows about, with the groups each belongs to.

    Exists because the "acting as" chooser and the publish call had two
    hardcoded identities (`user:alice`, `user:bob`) and a hardcoded
    `user:demo`, none of which was a member of any group holding a grant.
    Publishing therefore failed the invoking-user half of the entitlement
    check no matter how the objects were granted — the identity could not
    hold a grant, so there was nothing to find. Offering the principals
    UCS actually has makes the chooser answer a real question.

    `entitled` is the useful distinction for a caller picking a default:
    a principal in no group can never resolve an entitlement check.
    """
    try:
        from ..ucs_sync import _group_listing
        groups = _group_listing()
    except Exception as exc:
        # Not a 500: the chooser should degrade to "we could not ask UCS"
        # rather than break the page it sits on.
        raise HTTPException(
            502,
            f"Could not read group membership from UCS: {exc}. The identity chooser has "
            f"nothing real to offer until UCS responds.",
        )
    members: dict = {}
    for g in groups:
        for name in (g.get("member_names") or []):
            members.setdefault(name, []).append(g["group_name"])
    return {
        "principals": [
            {"name": name, "principal": f"user:{name}", "groups": sorted(gs),
             "entitled": bool(gs)}
            for name, gs in sorted(members.items())
        ],
    }
