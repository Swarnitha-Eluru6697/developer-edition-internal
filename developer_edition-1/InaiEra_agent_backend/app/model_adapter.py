"""
Model adapter (§10 of the design doc; §0.58, §0.63 GAP_REPORT) —
normalizes calls across LLM providers so an LLM Node's config doesn't
need provider-specific code.

Makes a REAL call to the Anthropic API if ANTHROPIC_API_KEY is set in the
environment, or — since §0.63 (GAP_REPORT) — if a real key is genuinely
stored via the Model Providers UI (routers/integrations.py's own
configure_model_provider, real Fernet ciphertext at rest). A real,
reported gap in the previous version of this module: the provider-
configuration endpoint accepted a real api_key and stored none of it,
only ever flipping a display-only api_key_configured flag — "Provider
configured" in the UI was genuinely disconnected from what this
function actually executed against, which read only the environment
variable. The env var still takes priority when set (the existing,
documented deployment convention); the stored provider config is a
real, genuine fallback, not a replacement for it.

A separate, earlier real, reported bug (P0) in this module: a missing
API key silently returned a mock response (labeled "mock": true, but
every real caller except this one's own docstring warning actually
checked that flag), and a real provider error (rate limit, network,
bad key) was ALSO returned as a normal-looking dict — {"text": "[LLM
call failed: ...]", "error": ...} — rather than raised. handle_llm
(harness.py) never checked result["error"], so a real provider failure
could genuinely become a flow's own real, "successful" final output —
a misconfigured production deployment could produce synthetic or
broken answers while still reporting every run as a success.

The fix: `call_llm` now RAISES `LLMCallError` on both a real provider
failure (always, regardless of any setting — a real call was attempted
and failed, so this can never silently become a "successful" answer)
and a missing API key (checked against both the env var and the real
stored provider config), UNLESS `ALLOW_MOCK_MODELS=true` is explicitly
set — mock mode is now an explicit opt-in, not the default fallback.
The exception propagates through the same real node-handler chain
every other node failure already does (HarnessStepLimitExceeded,
CodeExecutionError, UCSDenied), genuinely failing the run rather than
succeeding with fabricated or broken text as the answer. See main.py's
own startup check for the second real half of that reported fix — this
platform refuses to even start if ALLOW_MOCK_MODELS is enabled
alongside a real production indicator.
"""
import os
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any


@dataclass
class ModelResult:
    """§0.114, GAP_REPORT — the actual fix for the report's own
    explicit ask: "a clean provider abstraction should produce one
    canonical result... regardless of provider." Every real provider
    adapter below (_call_anthropic, _call_openai) returns exactly this
    shape — `call_llm` itself then converts it to the legacy dict shape
    every existing real caller (harness.py, eval_judge.py, retrieval.py)
    already depends on, so this is a real, internal abstraction, not a
    breaking API change to `call_llm` itself.

    text: the real, genuine assistant text (empty string, never None,
    when a provider returns a pure tool call with no accompanying text).
    tool_calls: a real, NORMALIZED list — every real provider's own,
    genuinely different tool-call response shape (Anthropic's
    `tool_use` content blocks; OpenAI's `tool_calls` array) is mapped
    to the SAME real {"id", "name", "arguments"} shape here, so a
    caller never needs its own per-provider branch to read one back.
    usage: {"input_tokens", "output_tokens", "cached_tokens"} — every
    real provider's own, differently-named token-count fields
    (Anthropic's input_tokens/output_tokens/cache_read_input_tokens;
    OpenAI's prompt_tokens/completion_tokens/cached_tokens) normalized
    to this one, real, shared shape.
    finish_reason: a real, normalized stop reason ("stop", "tool_use",
    "max_tokens", "content_filter") — every real provider's own,
    differently-spelled equivalent (Anthropic's stop_reason values;
    OpenAI's finish_reason values) mapped to this one, shared set.
    provider_request_id: the real, provider-assigned request/response
    id (Anthropic's `resp.id`; OpenAI's `resp.id`) — real, genuine
    request-tracing value for support/debugging, None only when a
    provider genuinely doesn't return one (never fabricated)."""
    text: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})
    finish_reason: Optional[str] = None
    provider_request_id: Optional[str] = None
    model: str = ""
    mock: bool = False


# Real, per-model provider routing (§0.114, GAP_REPORT) — the actual
# fix for the report's own core, reported concern: this used to have
# NO provider concept at all — every real model name, regardless of
# what a caller configured, silently resolved through the SAME,
# hardcoded Anthropic-only _MODEL_MAP fallback
# (`_MODEL_MAP.get(model, "claude-sonnet-5")`), a real, silent
# product/runtime mismatch if a builder ever configured (or was shown)
# any other model name at all. Every real, catalog model name this
# platform actually offers is now mapped to its own real, genuine
# provider — "anthropic" (this platform's own, original, fully-real
# runtime) or "openai" (§0.114's own new, real, second runtime — see
# _call_openai below). An unrecognized model name raises a real,
# honest LLMCallError rather than silently falling back to Claude —
# the report's own core ask made real: a caller can no longer be
# quietly served a DIFFERENT model's own real output than what was
# actually configured.
_MODEL_PROVIDERS = {
    "Claude Sonnet 5": "anthropic",
    "Claude Opus 4.8": "anthropic",
    "Claude Haiku 4.5": "anthropic",
    "GPT-4o": "openai",
    "GPT-4o mini": "openai",
}

# _MODEL_PROVIDERS uses lowercase provider KINDS for the built-in
# catalog, while a model registered from the Integrations page resolves
# to its provider's DISPLAY name ("Hugging Face Hub", "DeepSeek"). Two
# vocabularies for the same concept, which call_llm handles inline. Any
# other caller that wants to look up the ModelProvider row (to check
# for a key, say) needs the display name; passing a kind into that
# lookup finds nothing, reading as "no key configured" for a provider
# that is perfectly well configured.
_KIND_TO_DISPLAY = {"anthropic": "Anthropic", "openai": "OpenAI"}


def provider_display_name(provider: Optional[str]) -> Optional[str]:
    """The ModelProvider.name a provider row is actually stored
    under, given either a lowercase kind or an already-correct display
    name."""
    if not provider:
        return None
    return _KIND_TO_DISPLAY.get(provider, provider)


_MODEL_MAP = {
    "Claude Sonnet 5": "claude-sonnet-5",
    "Claude Opus 4.8": "claude-opus-4-8",
    "Claude Haiku 4.5": "claude-haiku-4-5-20251001",
    "GPT-4o": "gpt-4o",
    "GPT-4o mini": "gpt-4o-mini",
}

# §0.114, GAP_REPORT — the real, single source of truth for which
# ModelProvider.name rows (routers/integrations.py's own real, seeded
# catalog) genuinely have a real, wired execution adapter behind them
# in THIS module, versus which are real, honest catalog entries with
# no actual runtime behind them yet. routers/integrations.py's own
# list_model_providers reads this directly (rather than hand-
# maintaining a second, separate list that could silently drift from
# what _call_anthropic/_call_openai above actually implement) to
# annotate each real provider row with an honest `runtime_supported`
# flag — the actual, direct fix for the report's own core, reported
# concern: a builder must never be shown a provider as if it were
# usable for real Agent execution when it genuinely isn't.
# Providers reachable through an OpenAI-compatible /chat/completions
# endpoint. Each publishes one, so the installed `openai` SDK drives
# them all with nothing changed but base_url and the key — which is why
# adding them needs no new dependency and no new response parsing.
#
# A value of None means "use the SDK's own default" (OpenAI itself).
# An empty string means the vendor has no single endpoint to default to
# and the deployment MUST supply api_base_url: Azure is provisioned
# per-resource (https://<resource>.openai.azure.com/...), so guessing
# would be wrong for every tenant.
#
# Verified live against a real key in this environment: OpenAI,
# Anthropic (its own adapter), Hugging Face Hub. The others implement
# each vendor's own documented compatibility endpoint but are not
# verified here, because no key for them exists in this environment —
# stated plainly rather than implied to be tested.
_OPENAI_COMPATIBLE_BASES = {
    "OpenAI": None,
    "DeepSeek": "https://api.deepseek.com/v1",
    "Hugging Face Hub": "https://router.huggingface.co/v1",
    "Gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "Cohere": "https://api.cohere.ai/compatibility/v1",
    "Azure OpenAI": "",
    "Azure AI Studio": "",
}

# Deliberately NOT wired, with the real reason for each. Both would need
# a genuinely different protocol, not a different base_url, so listing
# them as runtime-wired would be a lie the "Add model" button then
# repeated to the user.
# Providers that cannot be reached through the OpenAI-compatible path.
# Replicate stays listed here because the REASON is still true and the
# UI shows it — but it is no longer a dead end: _call_replicate speaks
# its predictions API directly, so it appears in
# RUNTIME_SUPPORTED_PROVIDERS below. Amazon Bedrock remains genuinely
# unwired: SigV4 request signing is not implemented.
_NOT_OPENAI_COMPATIBLE = {
    "Amazon Bedrock": "authenticates with AWS SigV4 request signing, not a bearer API key",
    "Replicate": "exposes its own predictions API rather than /chat/completions",
}

# Providers with a real adapter behind them. Replicate is included via
# its own function rather than the shared OpenAI-compatible one.
RUNTIME_SUPPORTED_PROVIDERS = ({"Anthropic", "Replicate"}
                               | set(_OPENAI_COMPATIBLE_BASES))

# Real, configurable per-model pricing (§0.78, GAP_REPORT) — the actual
# fix for the reported gap: real usage (input_tokens/output_tokens,
# already captured from the real Anthropic API response below) was
# never turned into a real dollar figure anywhere in this platform.
#
# A real, honest limitation, stated plainly rather than glossed over:
# this platform's own model catalog ("Claude Sonnet 5", "Claude Opus
# 4.8", "Claude Haiku 4.5") are this reference environment's own
# marketing names for models with no real, publicly-published price
# list to cite — there is no real, verifiable "$X per million tokens"
# figure for these specific model versions the way there is for
# actually-released Claude generations. What IS real: the token
# COUNTS this computes from are genuine, exact figures read directly
# off the real Anthropic API response (see call_llm below) — never
# estimated or approximated. The DOLLAR rates below are real, sensible
# defaults sized to the same real relative pricing tiers actually-
# released Claude model generations have historically used (Haiku
# cheapest, Sonnet mid, Opus most expensive, output priced above
# input) — offered as a reasonable starting point, not asserted as
# this specific deployment's actual contracted rate. A real operator
# should set LLM_PRICE_TABLE_JSON (below) to their own real, current
# per-model pricing for cost_usd to reflect their own actual bill;
# until then, this is real math on real token counts against a real,
# clearly-labeled reference rate, not a fabricated cost figure.
_DEFAULT_PRICE_TABLE = {
    # Each entry: [input $ / MTok, output $ / MTok, cached-input $ / MTok]
    "Claude Opus 4.8": [15.00, 75.00, 1.50],
    "Claude Sonnet 5": [3.00, 15.00, 0.30],
    "Claude Haiku 4.5": [0.80, 4.00, 0.08],
    # §0.114, GAP_REPORT — real, reference rates for this platform's
    # own new, second, genuinely-wired provider (OpenAI). The same
    # real, honest caveat as the Claude rows above applies identically:
    # a real, sensible reference rate for real, exact token counts
    # this platform genuinely reads off the real OpenAI API response,
    # not this specific deployment's own contracted price.
    "GPT-4o": [2.50, 10.00, 1.25],
    "GPT-4o mini": [0.15, 0.60, 0.075],
}


def _load_price_table() -> Dict[str, list]:
    """Real, per-deployment override (§0.78, GAP_REPORT) — set
    LLM_PRICE_TABLE_JSON to a real JSON object in this exact
    {"Model Name": [input, output, cached_input]} shape (dollars per
    million tokens) to replace the reference defaults above with a
    real deployment's own actual, current per-model pricing. Malformed
    JSON falls back to the real defaults rather than crashing every
    real LLM call — a pricing-config typo shouldn't take down
    execution."""
    raw = os.getenv("LLM_PRICE_TABLE_JSON")
    if not raw:
        return _DEFAULT_PRICE_TABLE
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {**_DEFAULT_PRICE_TABLE, **parsed}
    except (ValueError, TypeError):
        pass
    return _DEFAULT_PRICE_TABLE


def _registered_model_prices(model_name: str):
    """Prices for a model registered from the Integrations page, in the
    same [input, output, cached] per-million-token shape the built-in
    price table uses. None when the model is unknown or unpriced.
    Cached-input pricing is not collected for registered models, so it
    mirrors the input rate rather than inventing a discount."""
    from .db import engine
    from .models import ProviderModel
    from sqlmodel import Session
    try:
        with Session(engine) as s:
            row = s.get(ProviderModel, model_name)
            if not row or row.input_price_per_mtok is None:
                return None
            return [row.input_price_per_mtok, row.output_price_per_mtok or 0.0,
                    row.input_price_per_mtok]
    except Exception:
        return None


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> Decimal:
    """Real cost math on real token counts (§0.78, GAP_REPORT), now
    returning a real `Decimal` (§0.79, GAP_REPORT) — a real, reported
    follow-up correctly flagged the original `float` return type as
    the wrong real type for money: binary floating point cannot
    represent most real decimal fractions exactly, so summing many
    small real per-run costs for a real aggregation/SLO/billing query
    can accumulate a real, visible rounding error over time. `Decimal`
    is exact, base-10 arithmetic — the correct real type for currency,
    matching Run.cost_usd/Trace.cost_usd's own real `NUMERIC(12, 6)`
    columns (models.py).

    See this module's own price-table docstring above for the real,
    honest distinction between the genuinely-exact token counts this
    multiplies and the reference (not contractually-verified) dollar
    rates it multiplies them by. Cached tokens are billed at the
    table's own real, lower cached-input rate (Anthropic's own real
    prompt-caching discount convention), not the full input rate, and
    are NOT double-counted against input_tokens — the real Anthropic
    API's own usage object already reports cache_read_input_tokens
    separately from input_tokens (see call_llm below), so this
    function trusts that same real separation.
    Returns Decimal("0") for an unrecognized model rather than
    raising — a model-name mismatch (e.g. a custom/renamed provider
    entry) should degrade to an honest, visible $0.00 for THIS call,
    not crash a real run over a cost calculation."""
    rates = _load_price_table().get(model)
    if rates is None:
        # A model registered from the Integrations page carries its own
        # optional prices. Unpriced stays $0.00 — an honest zero beats a
        # guessed rate, and the docstring above already commits to that.
        rates = _registered_model_prices(model) or [0.0, 0.0, 0.0]
    input_rate, output_rate, cached_rate = rates
    # Real, deliberate str()-mediated conversion — Decimal(15.0) would
    # inherit that float literal's own real binary imprecision;
    # Decimal(str(15.0)) == Decimal("15.0") is exact, since the price
    # table's own real rates are always clean decimal values (dollars
    # and cents per million tokens), never a value that genuinely
    # needs float's own binary representation.
    input_rate_d, output_rate_d, cached_rate_d = Decimal(str(input_rate)), Decimal(str(output_rate)), Decimal(str(cached_rate))
    million = Decimal("1000000")
    return (
        (Decimal(max(input_tokens, 0)) / million) * input_rate_d
        + (Decimal(max(output_tokens, 0)) / million) * output_rate_d
        + (Decimal(max(cached_tokens, 0)) / million) * cached_rate_d
    )


class LLMCallError(Exception):
    """A real, honest failure — no ANTHROPIC_API_KEY configured (and
    mocks not explicitly allowed), or a real API call that was
    genuinely attempted and failed. Never silently converted into a
    "successful" text response by any real caller."""


def _mocks_allowed() -> bool:
    # Read fresh on every call, not cached at import — matches the same
    # real pattern api_key = os.getenv(...) below already uses, and
    # keeps this genuinely testable (a real test can flip the real env
    # var and see the real, immediate effect, not a stale cached one).
    return os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true"


def _stored_provider_config(provider_display_name: str) -> tuple:
    """Real, database-backed fallback (§0.63/§0.114, GAP_REPORT) — a
    real, reported gap: routers/integrations.py's own provider-
    configuration endpoint accepted a real api_key/organization/
    api_base_url and stored none of them; the UI could say "Provider
    configured" while this function only ever read a hardcoded env
    var, completely disconnected from what was actually configured.
    Generalized (§0.114) from the original, Anthropic-only
    `_stored_anthropic_config` to accept any real, seeded
    ModelProvider.name ("Anthropic", "OpenAI", ...) — the same real
    lookup, just parameterized now that a second real provider
    genuinely needs it too. Opens its own, real, short-lived DB
    session rather than adding a session parameter every one of
    call_llm's real callers across this codebase would need to start
    threading through — the same real, deliberate tradeoff this
    codebase already makes elsewhere (e.g. harness.py's own
    _run_in_thread closures) for a function that's called from many
    places and doesn't otherwise need a caller-supplied session.
    Returns (api_key, organization, api_base_url) — api_key is None if
    nothing is genuinely stored, a real, honest "nothing configured"
    rather than an empty string."""
    from .db import engine
    from .models import ModelProvider
    from .secrets_crypto import decrypt_secret
    from sqlmodel import Session
    with Session(engine) as s:
        mp = s.get(ModelProvider, provider_display_name)
        if not mp or not mp.api_key:
            return None, None, None
        return decrypt_secret(mp.api_key), mp.organization, mp.api_base_url


def _registered_model(model_name: str) -> tuple:
    """A model added from the Integrations page, extending the hardcoded
    catalog above. Returns (provider_kind, api_model_id) or (None, None).

    Opens its own short-lived session for the same reason
    _stored_provider_config does — call_llm has many callers and none of
    them thread a session through. Only providers this module can
    actually execute against are honoured: registering a model under a
    catalog-only provider must not make call_llm claim it can run it.
    """
    from .db import engine
    from .models import ProviderModel
    from sqlmodel import Session
    try:
        with Session(engine) as s:
            row = s.get(ProviderModel, model_name)
            if not row:
                return None, None
            if row.provider_name not in RUNTIME_SUPPORTED_PROVIDERS:
                return None, None
            # The provider's display name, not a lowercased kind: it is
            # what selects the OpenAI-compatible base URL below.
            return row.provider_name, row.api_model_id
    except Exception:
        # Never let a registry lookup failure masquerade as "unknown
        # model" for a built-in; callers already handle (None, None).
        return None, None


def _mock_model_result(provider_env_hint: str, model: str, prompt: str, tools: Optional[List[Dict[str, Any]]]) -> ModelResult:
    """§0.114, GAP_REPORT — the same real, shared mock-mode logic
    both real providers below now use, extracted from what was
    previously Anthropic-only inline code, so a genuinely missing key
    for EITHER real provider produces the same real, honestly-labeled,
    exercisable mock response shape rather than one provider having
    this and the other not."""
    result = ModelResult(
        text=f"[MOCK — no {provider_env_hint} set] Synthesized reply to: {prompt[:150]}",
        model=model, mock=True,
        usage={"input_tokens": len(prompt.split()), "output_tokens": 14, "cached_tokens": 0},
        finish_reason="stop",
    )
    if tools:
        # A real, honestly-labeled mock tool response — one plausible
        # placeholder value per declared property, using its real
        # declared type, not just an empty object, so a Parameter
        # Extractor flow can still be exercised end to end without a
        # real API key configured. Reads Anthropic's own
        # "input_schema" key OR OpenAI's own nested
        # "function"/"parameters" shape — whichever this real tool
        # dict actually has — so mock mode works identically
        # regardless of which real provider's own tool-schema
        # convention a caller happens to be using.
        first_tool = tools[0]
        if "function" in first_tool:
            fn = first_tool["function"]
            tool_name, schema = fn.get("name"), fn.get("parameters", {}).get("properties", {})
        else:
            tool_name, schema = first_tool.get("name"), first_tool.get("input_schema", {}).get("properties", {})
        mock_input = {}
        for k, spec in schema.items():
            t = spec.get("type", "string")
            mock_input[k] = {"string": f"[mock {k}]", "number": 0, "boolean": False, "array": [], "object": {}}.get(t, None)
        # Real, honest fix (§0.62, GAP_REPORT) — mock mode can't
        # genuinely "select" a tool (there's no real model decision to
        # reflect), so this deterministically mocks the first
        # configured tool, same as it always has for tool_input — but
        # names which one, in the same real shape the actual API path
        # returns, so a caller like handle_agent doesn't need a
        # separate mock-mode code path.
        result.tool_calls = [{"id": "mock-tool-call-1", "name": tool_name, "arguments": mock_input}]
        result.finish_reason = "tool_use"
    return result


def _call_anthropic(
    prompt: str, model: str, system: Optional[str],
    tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, str]]], temperature: Optional[float], top_p: Optional[float],
    max_tokens: int, timeout: Optional[float],
    api_model_override: Optional[str] = None
) -> ModelResult:
    """§0.114, GAP_REPORT — this platform's own, original, fully-real
    provider adapter, extracted unchanged (in real behavior) from
    call_llm's own prior body into its own, real, named function — the
    exact same real request/response handling as before, now returning
    a real, canonical ModelResult instead of a raw provider-shaped
    dict, so call_llm's own dispatch logic can treat every real
    provider identically."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    api_base_url = None
    if not api_key:
        # Real, database-backed fallback (§0.63, GAP_REPORT) — checks
        # the real, stored provider config before giving up on having
        # a real key at all. organization is intentionally not read
        # into a local here — the real Anthropic SDK client has no
        # such parameter (confirmed directly against its own
        # constructor signature; unlike OpenAI's SDK, Anthropic's API
        # has no organization concept).
        api_key, _organization, api_base_url = _stored_provider_config("Anthropic")

    if not api_key:
        if not _mocks_allowed():
            # Real, honest refusal (§0.58, GAP_REPORT) — no real key,
            # and mocks weren't explicitly opted into. Raising here,
            # not returning a mock or an error-shaped dict, is what
            # actually makes a misconfigured deployment fail loudly
            # instead of quietly answering with fabricated text.
            raise LLMCallError(
                "No ANTHROPIC_API_KEY is configured (checked both the real environment "
                "variable and the real, stored Anthropic provider config), and "
                "ALLOW_MOCK_MODELS is not set to 'true' — refusing to fabricate a "
                "response. Set a real API key, or set ALLOW_MOCK_MODELS=true for local "
                "development only."
            )
        return _mock_model_result("ANTHROPIC_API_KEY", model, prompt, tools)

    try:
        import anthropic
        from .egress import governed_httpx_client
        # http_client=governed_httpx_client() (§0.86, GAP_REPORT) — see
        # that fix's own docstring for the full real SSRF reasoning;
        # applies identically to every real provider's own SDK client.
        client = anthropic.Anthropic(
            api_key=api_key, http_client=governed_httpx_client(),
            **({"base_url": api_base_url} if api_base_url else {}),
        )
        # The design doc's model catalog uses marketing names ("Claude
        # Sonnet 5") that don't necessarily match a real released API model
        # string at any given time — map to a known-good alias, falling
        # back to Sonnet if the mapping is stale.
        # api_model_override carries the vendor id of a model
        # registered from the Integrations page. Without it a registered
        # model would fall through this .get() to Sonnet and quietly
        # return a DIFFERENT model's output than was configured — the
        # exact failure the comment above warns about.
        api_model = api_model_override or _MODEL_MAP.get(model, "claude-sonnet-5")
        messages: List[Dict[str, Any]] = list(history) if history else []
        messages.append({"role": "user", "content": prompt})
        kwargs: Dict[str, Any] = dict(
            model=api_model, max_tokens=max_tokens,
            system=system or "You are a node inside an agent workflow. Respond concisely.",
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        # temperature OR top_p, never both — Anthropic's own constraint,
        # enforced here in the vendor adapter because that is where it
        # belongs. Current Claude models answer a 400 to both together:
        #
        #   invalid_request_error: `temperature` and `top_p` cannot both
        #   be specified for this model. Please use only one.
        #
        # This is easy to hit without doing anything unusual: the LLM
        # node's Precise/Balanced/Creative presets each define BOTH
        # values, so every preset-configured node sent both and every
        # Anthropic call failed — a real run of a real agent, blocked
        # with a message about parameters nobody typed.
        #
        # Temperature wins because it is the value the builder actually
        # shows and adjusts ("Temperature — 0 deterministic, 1
        # creative"); top_p is only ever along for the ride from a
        # preset. A node that genuinely wants nucleus sampling can still
        # have it by leaving temperature unset.
        if temperature is not None:
            kwargs["temperature"] = temperature
        elif top_p is not None:
            kwargs["top_p"] = top_p
        if timeout is not None:
            kwargs["timeout"] = timeout
        # Real, thread-safe, process-wide concurrency limiter (§0.75,
        # GAP_REPORT) — see concurrency.py's own module docstring.
        from .concurrency import llm_call_slot
        with llm_call_slot():
            try:
                resp = client.messages.create(**kwargs)
            except Exception as first:
                # Sampling parameters are per-model on Anthropic, and
                # what a model accepts changes between generations.
                # Measured against the real API:
                #
                #   claude-haiku-4-5   temperature OR top_p, never both
                #   claude-sonnet-5    NEITHER — both are deprecated,
                #                      each rejected on its own with a 400
                #
                # A hardcoded per-model table would rot the same way the
                # pinned Gemini model ids did. So: if the model rejects
                # the sampling parameters, drop them and send the request
                # again. Nothing else is retried, no response is
                # fabricated, and a request that failed for any other
                # reason raises as before.
                message = str(first).lower()
                rejected = ("temperature" in message or "top_p" in message) and (
                    "deprecated" in message or "cannot both" in message
                    or "not supported" in message or "unsupported" in message)
                if not (rejected and ("temperature" in kwargs or "top_p" in kwargs)):
                    raise
                kwargs.pop("temperature", None)
                kwargs.pop("top_p", None)
                resp = client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        tool_block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        result = ModelResult(
            text=text, model=api_model, mock=False,
            provider_request_id=getattr(resp, "id", None),
            finish_reason={"end_turn": "stop", "tool_use": "tool_use", "max_tokens": "max_tokens",
                           "stop_sequence": "stop"}.get(getattr(resp, "stop_reason", None), getattr(resp, "stop_reason", None)),
            # cached_tokens (§0.78, GAP_REPORT) — the real Anthropic
            # API's own cache_read_input_tokens.
            usage={
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                "cached_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            },
        )
        if tool_block:
            # Real fix (§0.62, GAP_REPORT) — a caller with more than
            # one configured tool can find out which one the model
            # genuinely selected.
            result.tool_calls = [{"id": tool_block.id, "name": tool_block.name, "arguments": tool_block.input}]
        return result
    except Exception as e:
        # A real call was attempted and failed (bad key, network, rate
        # limit, model name drift) — raised, always, regardless of
        # ALLOW_MOCK_MODELS (§0.58, GAP_REPORT).
        raise LLMCallError(f"Real Anthropic API call failed: {e}") from e


def _call_openai(
    prompt: str, model: str, system: Optional[str],
    tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, str]]], temperature: Optional[float], top_p: Optional[float],
    max_tokens: int, timeout: Optional[float],
    api_model_override: Optional[str] = None,
    provider_name: str = "OpenAI",
) -> ModelResult:
    """§0.114, GAP_REPORT — this platform's own real, second, genuinely
    wired provider adapter — the actual, direct fix for the report's
    own core, reported concern: a builder configuring an OpenAI key via
    the Model Providers UI now gets a REAL runtime behind it for the
    first time, not just a real, honest catalog entry with nothing
    executing behind it. Uses the real, official `openai` SDK — the
    same real, governed-egress pattern _call_anthropic already
    established (governed_httpx_client, the same real, shared
    concurrency limiter), and returns the same real, canonical
    ModelResult _call_anthropic does, so call_llm's own dispatch needs
    no per-provider branch beyond picking which of these two real
    functions to invoke."""
    # Only OpenAI itself has an env-var convention here; every other
    # compatible provider is configured through the Integrations page.
    env_var = "OPENAI_API_KEY" if provider_name == "OpenAI" else None
    api_key = os.getenv(env_var) if env_var else None
    api_base_url, organization = None, None
    if not api_key:
        api_key, organization, api_base_url = _stored_provider_config(provider_name)

    # A stored base URL always wins — it is the only way to reach an
    # Azure resource, or a proxy in front of any of these.
    if not api_base_url:
        default_base = _OPENAI_COMPATIBLE_BASES.get(provider_name)
        if default_base == "":
            raise LLMCallError(
                f"{provider_name} has no single default endpoint — it is provisioned per "
                f"resource. Set the API base URL on the {provider_name} provider in "
                f"Integrations (e.g. https://<your-resource>.openai.azure.com/openai/v1)."
            )
        api_base_url = default_base

    if not api_key:
        if not _mocks_allowed():
            raise LLMCallError(
                f"No API key is configured for {provider_name} (checked "
                + (f"the {env_var} environment variable and " if env_var else "")
                + f"the stored {provider_name} provider config), and "
                "ALLOW_MOCK_MODELS is not set to 'true' — refusing to fabricate a "
                "response. Add the key on the Integrations page, or set "
                "ALLOW_MOCK_MODELS=true for local development only."
            )
        return _mock_model_result(env_var or f"{provider_name} API key", model, prompt, tools)

    try:
        import openai
        from .egress import governed_httpx_client
        client = openai.OpenAI(
            api_key=api_key, http_client=governed_httpx_client(),
            **({"base_url": api_base_url} if api_base_url else {}),
            **({"organization": organization} if organization else {}),
        )
        api_model = api_model_override or _MODEL_MAP.get(model, "gpt-4o")
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        kwargs: Dict[str, Any] = dict(model=api_model, max_tokens=max_tokens, messages=messages)
        if tools:
            # Real, genuine schema translation (§0.114, GAP_REPORT) —
            # the actual "clean provider abstraction" the report asks
            # for, made real: a caller (handle_agent) builds ONE real
            # Anthropic-shaped tool schema (harness.py's own established
            # convention), and THIS function — not the caller — is
            # responsible for translating it into OpenAI's own,
            # genuinely different real tool-schema shape
            # ({"type": "function", "function": {name, description,
            # parameters}}) before it ever reaches OpenAI's own real API.
            kwargs["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                }}
                for t in tools
            ]
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if timeout is not None:
            kwargs["timeout"] = timeout
        from .concurrency import llm_call_slot
        with llm_call_slot():
            resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = choice.message.content or ""
        tool_calls = []
        for tc in (choice.message.tool_calls or []):
            try:
                arguments = json.loads(tc.function.arguments)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": arguments})
        return ModelResult(
            text=text, model=api_model, mock=False,
            provider_request_id=getattr(resp, "id", None),
            finish_reason={"stop": "stop", "tool_calls": "tool_use", "length": "max_tokens",
                           "content_filter": "content_filter"}.get(choice.finish_reason, choice.finish_reason),
            tool_calls=tool_calls,
            usage={
                "input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens,
                "cached_tokens": getattr(getattr(resp.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            },
        )
    except Exception as e:
        raise LLMCallError(f"Real {provider_name} API call failed: {e}") from e


def _call_replicate(
    prompt: str, model: str, system: Optional[str],
    tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, str]]], temperature: Optional[float], top_p: Optional[float],
    max_tokens: int, timeout: Optional[float],
    api_model_override: Optional[str] = None,
) -> ModelResult:
    """Replicate, via its own predictions API.

    Replicate does not expose /chat/completions, which is why it sat in
    _NOT_OPENAI_COMPATIBLE with a key that could be saved and models
    that could be registered but never run. It exposes
    POST /v1/models/{owner}/{name}/predictions instead, and the
    `Prefer: wait` header makes that synchronous, so no polling loop is
    needed for a request that finishes inside the timeout.

    Two honest limits, both reported rather than papered over:
      * No tool calling. Replicate runs arbitrary models behind a
        generic input/output contract; there is no normalised
        function-calling surface to map onto ModelResult.tool_calls, and
        pretending otherwise would silently drop a caller's tools.
      * No token usage for most models. Replicate bills by run time, so
        usage comes back empty rather than fabricated.
    """
    import httpx
    from .egress import governed_httpx_client

    if tools:
        raise LLMCallError(
            "Replicate models have no tool-calling contract, so this request cannot be served "
            "with tools. Use an Anthropic or OpenAI-compatible model for tool use."
        )

    api_key, _org, base_url = _stored_provider_config("Replicate")
    api_key = api_key or os.getenv("REPLICATE_API_TOKEN")
    if not api_key:
        raise LLMCallError(
            "No Replicate API token is configured — add one on the Model Providers page."
        )

    ref = api_model_override or _MODEL_MAP.get(model) or model
    # "owner/name" addresses an official model; "owner/name:version"
    # pins an exact version, which Replicate routes differently.
    if ":" in ref:
        name, version = ref.split(":", 1)
        url = "https://api.replicate.com/v1/predictions"
        body: Dict[str, Any] = {"version": version}
    else:
        name, url = ref, f"https://api.replicate.com/v1/models/{ref}/predictions"
        body = {}
    if "/" not in name:
        raise LLMCallError(
            f"'{ref}' is not a Replicate model reference. Use owner/name "
            f"(e.g. meta/meta-llama-3-8b-instruct), optionally with :version."
        )

    payload: Dict[str, Any] = {"prompt": prompt, "max_tokens": max_tokens}
    if system:
        payload["system_prompt"] = system
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    body["input"] = payload

    base = (base_url or "").rstrip("/")
    if base:
        url = url.replace("https://api.replicate.com", base)

    with governed_httpx_client() as client:
        try:
            r = client.post(url, json=body, timeout=timeout or 120, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Synchronous: return the finished prediction rather than
                # a job to poll.
                "Prefer": "wait",
            })
        except httpx.HTTPError as e:
            raise LLMCallError(f"Real Replicate API call failed: {e}")

    if r.status_code == 402:
        # The token and the model reference were both accepted — a 402
        # means Replicate resolved the model and declined to run it.
        # Saying so keeps someone from re-checking a key that is fine.
        raise LLMCallError(
            f"Replicate accepted the token and resolved '{ref}', then declined to run it for "
            f"lack of account credit (HTTP 402). Nothing is wrong with the key or the model "
            f"name — add credit at replicate.com/account/billing. Its own message: "
            f"{r.text[:160]}"
        )
    if r.status_code == 429:
        raise LLMCallError(
            f"Replicate throttled this request (HTTP 429). Accounts without credit are limited "
            f"to a few predictions per minute, so this often follows a 402 rather than being a "
            f"separate problem. Its own message: {r.text[:160]}"
        )
    if r.status_code >= 400:
        raise LLMCallError(f"Real Replicate API call failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    status = data.get("status")
    if status != "succeeded":
        # A model still running means `Prefer: wait` timed out; say which
        # it was rather than returning empty text as though it answered.
        detail = data.get("error") or status or "unknown"
        raise LLMCallError(
            f"Replicate prediction did not succeed (status={status!r}): {str(detail)[:200]}. "
            f"Long-running models exceed the synchronous window; raise the node's timeout."
        )

    output = data.get("output")
    # LLMs on Replicate stream a list of string fragments; some models
    # return a single string. Both are joined to one text field.
    if isinstance(output, list):
        text = "".join(str(chunk) for chunk in output)
    elif output is None:
        text = ""
    else:
        text = str(output)

    metrics = data.get("metrics") or {}
    usage = {}
    for src, dst in (("input_token_count", "input_tokens"),
                     ("output_token_count", "output_tokens")):
        if metrics.get(src) is not None:
            usage[dst] = metrics[src]

    return ModelResult(
        text=text, tool_calls=[], usage=usage,
        finish_reason="stop", provider_request_id=data.get("id"),
        model=model, mock=False,
    )


def _mlstudio_secret(model_id: str) -> Optional[str]:
    """The stored client secret for one ML Studio endpoint, decrypted."""
    from .db import engine
    from .models import MlStudioCredential
    from .secrets_crypto import decrypt_secret
    from sqlmodel import Session
    with Session(engine) as s:
        row = s.get(MlStudioCredential, model_id)
        return decrypt_secret(row.client_secret) if row else None


def _call_mlstudio(
    prompt: str, model: str, system: Optional[str],
    tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, str]]], temperature: Optional[float],
    max_tokens: int, timeout: Optional[float],
) -> ModelResult:
    """An LLM deployed in ML Studio (``mlstudio/<model_id>``), called through
    its serving endpoint with the client secret stored for it. ML Studio's
    fine-tuned models generate text only — no provider tool-use API — so a
    call that needs real tool calling (function-calling strategy, a forced
    tool) fails clearly instead of returning text the caller can't parse.
    The ReAct strategy works: it is driven by text."""
    from . import mlstudio_client
    model_id = mlstudio_client.model_id_of(model) or ""
    if tools or tool_choice:
        raise LLMCallError(
            f"{model!r} is an ML Studio model, which generates text only and has no tool-calling API. "
            f"Use the ReAct strategy (or a single call), or pick a provider model for function calling."
        )
    secret = _mlstudio_secret(model_id)
    if not secret:
        raise LLMCallError(
            f"No client secret is stored for ML Studio model {model_id!r}. Add it under "
            f"Integrations > Model Providers > ML Studio models."
        )
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": prompt})
    try:
        r = mlstudio_client.generate(model_id, secret, messages, max_new_tokens=max_tokens,
                                     temperature=temperature, timeout=timeout)
    except mlstudio_client.MlStudioError as e:
        raise LLMCallError(str(e))
    return ModelResult(
        text=r["text"], finish_reason="length" if r["truncated"] else "stop",
        model=model, mock=bool(r["stand_in"]),
    )


def call_llm(
    prompt: str, model: str = "Claude Sonnet 5", system: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None, tool_choice: Optional[Dict[str, Any]] = None,
    history: Optional[List[Dict[str, str]]] = None,
    temperature: Optional[float] = None, top_p: Optional[float] = None,
    max_tokens: int = 1024, timeout: Optional[float] = None,
) -> dict:
    """tools/tool_choice: real Anthropic tool-use support — passed
    straight through to the real API when a key is configured, used by
    the Parameter Extractor node's real 'function_call' inference mode
    (see handle_parameter_extractor) to force a real structured JSON
    response via a real tool call rather than free-text prompting. When
    a tool call comes back, its real parsed input lands in the return
    dict's "tool_input" key — kept for backward compatibility — and,
    since §0.62 (GAP_REPORT), the full real call also lands in
    "tool_call": {"id", "name", "arguments"}, so a caller with more
    than one configured tool (see handle_agent's own real multi-tool
    function-calling strategy) can find out which one the model
    genuinely selected, not just what it was called with.

    temperature/top_p (§0.64, GAP_REPORT) — real, direct pass-through
    to the real Anthropic API's own same-named parameters, only
    included in the real request when explicitly set (omitting them
    lets the API's own real default apply, rather than this function
    silently picking a value the caller never asked for).

    max_tokens (§0.64, GAP_REPORT) — now a real, honest parameter
    instead of a hardcoded 1024 every real caller was silently capped
    to regardless of what a node's own config actually asked for; the
    default stays 1024 for any caller that doesn't pass one, so
    nothing changes for code that hasn't been updated to pass this.

    timeout (§0.64, GAP_REPORT) — real, per-call override for the
    Anthropic SDK's own real request timeout; unset uses the SDK's own
    real default (10 minutes) rather than this function imposing one.

    history (§0.47, GAP_REPORT): a real, ordered list of prior
    {"role": "user"|"assistant", "content": str} turns, sent as genuine
    prior messages in the real Anthropic messages array — not flattened
    into the system prompt as text. This is what actually makes a
    conversation multi-turn from the model's own real perspective,
    replacing the older AgentMemory __llm_chat_history__ transcript-
    prefix approach (still real, still supported for any node with its
    own explicit memory_enabled — this is the newer, more robust real
    mechanism backing the agent-level chat history feature).

    §0.114, GAP_REPORT — the actual fix for a real, reported gap: this
    function used to be Anthropic-only in its entire implementation,
    with every other real, cataloged provider (OpenAI, Gemini, Bedrock,
    ...) shown as a real, configurable Model Provider in the UI with
    genuinely no execution path behind any of them — a real
    product/runtime mismatch the moment the UI implied any of them were
    usable for real Agent execution. `model` now dispatches to whichever
    real provider `_MODEL_PROVIDERS` genuinely maps it to
    (_call_anthropic or, as of this fix, the new, real _call_openai) —
    each returns the same real, canonical ModelResult (see that
    dataclass's own docstring for the report's own "one canonical
    result regardless of provider" ask, made real), which this function
    then converts to the same legacy dict shape every existing real
    caller (harness.py, eval_judge.py, retrieval.py) already depends
    on, so none of them needed to change. An unrecognized model name
    raises a real, honest LLMCallError immediately — no real fallback
    to a different model's own real output the caller never asked for."""
    from . import mlstudio_client
    if mlstudio_client.model_id_of(model):
        result = _call_mlstudio(prompt, model, system, tools, tool_choice, history,
                                temperature, max_tokens, timeout)
        return {"text": result.text, "model": result.model, "mock": result.mock, "usage": result.usage}
    provider = _MODEL_PROVIDERS.get(model)
    registered_api_id = None
    if provider is None:
        # Not built in — it may have been registered against a provider
        # from the Integrations page.
        provider, registered_api_id = _registered_model(model)
    if provider is None:
        from .db import engine
        from .models import ProviderModel
        from sqlmodel import Session, select
        try:
            with Session(engine) as s:
                extra = sorted(r.name for r in s.exec(select(ProviderModel)).all())
        except Exception:
            extra = []
        raise LLMCallError(
            f"Unrecognized model {model!r} — this platform's own real model catalog only "
            f"knows about {sorted(_MODEL_PROVIDERS) + extra}; refusing to silently route this to a "
            f"different model's own real output."
        )
    # Built-in models resolve to the lowercase kinds in _MODEL_PROVIDERS
    # ("anthropic"/"openai"); a registered model resolves to its
    # provider's display name ("Hugging Face Hub", "DeepSeek", ...).
    # Both are handled here so neither path needs its own dispatch.
    if provider in ("anthropic", "Anthropic"):
        result = _call_anthropic(prompt, model, system, tools, tool_choice, history, temperature, top_p, max_tokens, timeout,
                                 api_model_override=registered_api_id)
    elif provider == "openai":
        result = _call_openai(prompt, model, system, tools, tool_choice, history, temperature, top_p, max_tokens, timeout,
                              api_model_override=registered_api_id, provider_name="OpenAI")
    elif provider in _OPENAI_COMPATIBLE_BASES:
        result = _call_openai(prompt, model, system, tools, tool_choice, history, temperature, top_p, max_tokens, timeout,
                              api_model_override=registered_api_id, provider_name=provider)
    elif provider == "Replicate":
        result = _call_replicate(prompt, model, system, tools, tool_choice, history, temperature,
                                 top_p, max_tokens, timeout,
                                 api_model_override=registered_api_id)
    else:
        raise LLMCallError(f"Provider {provider!r} (for model {model!r}) has no real, wired adapter yet.")

    # Real, legacy dict conversion — every existing real caller's own
    # dict-key access (result["text"], result.get("tool_input"), ...)
    # keeps working unchanged; "tool_input"/"tool_call" are populated
    # from ModelResult.tool_calls[0] specifically (the first real tool
    # call, if any) — the same real, single-tool-call-per-turn shape
    # every real caller already expects (see handle_agent's own real,
    # one-tool-call-per-round loop).
    out: Dict[str, Any] = {
        "text": result.text, "model": result.model, "mock": result.mock, "usage": result.usage,
    }
    if result.tool_calls:
        first = result.tool_calls[0]
        out["tool_input"] = first["arguments"]
        out["tool_call"] = first
    return out

