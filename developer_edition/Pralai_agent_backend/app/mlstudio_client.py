"""ML Studio, as a model source for LLM / Agent / parameter-extraction nodes.

ML Studio is a separate, operator-configured internal service (like
UCS_BASE_URL), so it is reached directly rather than through the user-URL
egress guard: a node only ever picks a model id, never a host.

Every ML Studio route sits behind gateway-auth, so each call carries the
shared gateway secret (ENVOY_INTERNAL_SECRET, the same value ML Studio
checks) and a service identity — the same service-to-service pattern ML
Studio itself uses towards UCS (UCS_SERVICE_USERNAME + only the permission
the call needs):

  * listing deployments needs mlstudio:endpoints:view;
  * predicting needs no ML Studio permission — the endpoint authenticates
    the caller by its own client secret (Authorization: Bearer <secret>),
    which an admin stores per model from Integrations > Model Providers >
    ML Studio models (see MlStudioCredential).

A model is selected in the Builder as ``mlstudio/<model_id>`` (MODEL_PREFIX).
Only LLM-domain deployments are runnable from a node; classical ML models
take feature columns, not a prompt, and are listed but disabled.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

MODEL_PREFIX = "mlstudio/"
PROVIDER_NAME = "ML Studio"
LIST_PERMISSION = "mlstudio:endpoints:view"


class MlStudioError(Exception):
    pass


def base_url() -> str:
    return os.getenv("MLSTUDIO_BASE_URL", "http://127.0.0.1:4001").rstrip("/")


def model_id_of(model: str) -> Optional[str]:
    """The ML Studio model id behind a Builder model value, or None."""
    if model and model.startswith(MODEL_PREFIX):
        mid = model[len(MODEL_PREFIX):].strip()
        return mid or None
    return None


def _service_headers(permissions: str = "") -> Dict[str, str]:
    secret = os.getenv("ENVOY_INTERNAL_SECRET", "").strip()
    if not secret:
        raise MlStudioError(
            "ENVOY_INTERNAL_SECRET is not set on the Agent backend, so it can't call ML Studio "
            "(every ML Studio route requires the gateway secret)."
        )
    user = os.getenv("MLSTUDIO_SERVICE_USERNAME", "agent-service").strip() or "agent-service"
    return {
        "X-Inaiera-Gateway-Secret": secret,
        "X-User-Name": user,
        "X-User-Id": user,
        "X-User-Permissions": permissions,
    }


def list_deployments(timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Every model currently deployed in ML Studio (GET /api/deployments)."""
    import httpx
    url = f"{base_url()}/api/deployments"
    try:
        res = httpx.get(url, headers=_service_headers(LIST_PERMISSION), timeout=timeout)
    except httpx.HTTPError as e:
        raise MlStudioError(f"couldn't reach ML Studio at {base_url()}: {e}")
    if res.status_code >= 400:
        raise MlStudioError(f"ML Studio answered {res.status_code} listing deployments: {res.text[:200]}")
    data = res.json()
    return data if isinstance(data, list) else []


def generate(
    model_id: str, client_secret: str, messages: List[Dict[str, str]], *,
    max_new_tokens: Optional[int] = None, temperature: Optional[float] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Chat completion from an ML Studio LLM deployment, through the same
    POST /api/serve/{model}/predict its endpoint callers use (so the call is
    traced and counted in ML Studio's monitoring). Returns
    {"text", "truncated", "stand_in"}."""
    import httpx
    payload: Dict[str, Any] = {"messages": messages}
    if max_new_tokens:
        payload["max_new_tokens"] = int(max_new_tokens)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    headers = {**_service_headers(), "Authorization": f"Bearer {client_secret}"}
    url = f"{base_url()}/api/serve/{quote(model_id, safe='')}/predict"
    try:
        # Local generation on CPU can take a while; ML Studio's own runner
        # has its own timeout, so this only has to outlast that.
        res = httpx.post(url, json=payload, headers=headers, timeout=timeout or 300)
    except httpx.HTTPError as e:
        raise MlStudioError(f"couldn't reach ML Studio at {base_url()}: {e}")
    try:
        body = res.json()
    except ValueError:
        body = {"error": res.text[:300]}
    if res.status_code >= 400:
        reason = body.get("reason") or body.get("error") or body
        raise MlStudioError(f"ML Studio answered {res.status_code} for {model_id!r}: {reason}")
    pred = body.get("prediction")
    if isinstance(pred, dict):
        text = pred.get("prediction")
        if not isinstance(text, str):
            text = pred.get("generatedText")
        truncated = bool(pred.get("truncated"))
    else:
        text, truncated = pred, False
    if not isinstance(text, str):
        raise MlStudioError(f"ML Studio returned no generated text for {model_id!r}: {str(body)[:200]}")
    stand_in = "stand-in" in str(body.get("servedBy", ""))
    return {"text": text, "truncated": truncated, "stand_in": stand_in}
