"""What a chat model can actually do in this platform's runtime.

Derived from the same tables call_llm dispatches on, so the model picker, the
publish validator and the runtime cannot disagree."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlmodel import Session, select


_PROVIDER_ENV_KEY = {"Anthropic": "ANTHROPIC_API_KEY", "OpenAI": "OPENAI_API_KEY", "Replicate": "REPLICATE_API_TOKEN"}


def provider_configured(session: Session, provider_name: str) -> "tuple[bool, Optional[str]]":
    """Can this deployment actually call the provider? The same sources the
    runtime reads: a key in the environment, or one stored on the provider
    (Integrations). Local mock models count as configured only when
    ALLOW_MOCK_MODELS is on, exactly as the runtime treats them."""
    import os
    from .model_adapter import RUNTIME_SUPPORTED_PROVIDERS
    from .models import ModelProvider
    from . import mlstudio_client
    if provider_name == mlstudio_client.PROVIDER_NAME:
        # Credentials are per ML Studio endpoint, not per provider —
        # checked per model by mlstudio_model_configured.
        return True, None
    if provider_name not in RUNTIME_SUPPORTED_PROVIDERS:
        return False, f"{provider_name} has no execution adapter wired up yet."
    if os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true":
        return True, None
    env = _PROVIDER_ENV_KEY.get(provider_name)
    if env and os.getenv(env):
        return True, None
    mp = session.get(ModelProvider, provider_name)
    if mp and (mp.api_key or mp.api_key_configured):
        return True, None
    return False, f"No API key is configured for {provider_name}. Add one in Integrations."


def provider_capabilities(provider_name: str) -> Dict[str, bool]:
    from .model_adapter import RUNTIME_SUPPORTED_PROVIDERS
    wired = provider_name in RUNTIME_SUPPORTED_PROVIDERS
    return {
        "supports_chat": wired,
        # Replicate has no tool-use path in model_adapter; every other wired
        # provider maps tools through the Anthropic or OpenAI-compatible adapter.
        "supports_tool_calling": wired and provider_name != "Replicate",
        # No adapter sends image content to a model yet.
        "supports_vision": False,
    }


def capabilities_for(session: Session, model_name: str) -> Optional[Dict[str, Any]]:
    """Capabilities of a model by catalog name, or None when it isn't in the catalog."""
    from .model_adapter import _MODEL_PROVIDERS, provider_display_name
    from .models import ProviderModel
    from . import mlstudio_client
    if mlstudio_client.model_id_of(model_name):
        # An ML Studio LLM deployment: text generation only, no tool-use API.
        return {"supports_chat": True, "supports_tool_calling": False, "supports_vision": False,
                "provider": mlstudio_client.PROVIDER_NAME}
    kind = _MODEL_PROVIDERS.get(model_name)
    if kind:
        display = provider_display_name(kind)
        return {**provider_capabilities(display), "provider": display}
    row = session.exec(select(ProviderModel).where(ProviderModel.name == model_name)).first()
    if row:
        return {**provider_capabilities(row.provider_name), "provider": row.provider_name}
    return None


def mlstudio_model_configured(session: Session, model_name: str) -> "tuple[bool, Optional[str]]":
    """Whether an ``mlstudio/<id>`` model has its endpoint client secret stored."""
    from . import mlstudio_client
    from .models import MlStudioCredential
    model_id = mlstudio_client.model_id_of(model_name)
    if not model_id:
        return True, None
    if session.get(MlStudioCredential, model_id):
        return True, None
    return False, (f"no client secret is stored for ML Studio model '{model_id}'. Add it under "
                   f"Integrations > Model Providers > ML Studio models.")
