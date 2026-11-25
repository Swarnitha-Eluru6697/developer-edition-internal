"""Model gateway client — the single door to inference.

Replaces the direct Anthropic SDK with an OpenAI-compatible client pointed at
the in-perimeter gateway (LiteLLM/vLLM). Everything speaks logical model names
(codegen-generation / codegen-generation-alt / codegen-utility) so a checkpoint
swap is a gateway config change, never a code change (design §4.4/§10.3).

Deliberately thin: chat completions, optional JSON-mode, and a helper that
strips code fences / ```json wrappers small models sometimes emit around
structured output. No provider-specific message shapes leak past this module.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

log = structlog.get_logger()

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class ModelProviderError(RuntimeError):
    """A completion could not be produced by the selected provider; the message is safe to show (it never contains the API key)."""


class ModelClient:
    """Async client for the OpenAI-compatible /chat/completions endpoint.

    One shared httpx client, reused across turns. All calls are non-streaming:
    the harness streams progress to the UI itself via Redis envelopes (as the
    old loop did), so we don't need token streaming from the gateway.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._base_url = s.codegen_gateway_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if s.codegen_gateway_api_key:
            headers["Authorization"] = f"Bearer {s.codegen_gateway_api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=s.codegen_request_timeout_seconds,
        )
        self._max_tokens = s.codegen_max_output_tokens

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.6,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        """One chat completion, from whichever model the admin chose in Settings: the in-perimeter gateway (default), or OpenAI /
        Anthropic with their key. The logical `model` name only means something to the gateway; an external provider answers with
        its own configured model. There is no fallback between them: an unreadable setting or a failing provider fails the turn
        with its reason, it never sends the prompt to a different model."""
        from app.services.codegen_provider import (
            PROVIDER_ANTHROPIC,
            PROVIDER_OPENAI,
            CodegenConfigError,
            get_codegen_agent_config,
            model_for,
        )

        try:
            cfg = await get_codegen_agent_config()
        except CodegenConfigError as e:
            raise ModelProviderError(str(e)) from None
        provider, api_key = cfg["provider"], cfg.get("api_key")
        if provider in (PROVIDER_OPENAI, PROVIDER_ANTHROPIC):
            if not api_key:
                raise ModelProviderError(f"The {provider} provider is selected but no API key is saved. Enter it in Settings.")
            call = self._complete_openai if provider == PROVIDER_OPENAI else self._complete_anthropic
            return await call(api_key=api_key, model=model_for(provider) or "", system=system, messages=messages,
                              temperature=temperature, json_mode=json_mode, max_tokens=max_tokens)
        return await self._complete_gateway(model=model, system=system, messages=messages, temperature=temperature,
                                            json_mode=json_mode, max_tokens=max_tokens)

    @staticmethod
    def _provider_failure(provider: str, exc: Exception) -> "ModelProviderError":
        """A short, key-free reason for a failed provider call."""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            detail = ""
            try:
                body = exc.response.json()
                err = body.get("error")
                detail = (err.get("message") if isinstance(err, dict) else err) or ""
            except Exception:  # noqa: BLE001
                pass
            hint = {401: "the API key was rejected", 403: "the API key is not allowed to use this model", 404: "the configured model does not exist for this key",
                    429: "rate limit or quota exceeded"}.get(status, "")
            return ModelProviderError(f"{provider} returned HTTP {status}" + (f" ({hint})" if hint else "") + (f": {str(detail)[:300]}" if detail else ""))
        return ModelProviderError(f"{provider} could not be reached ({type(exc).__name__})")

    async def _complete_openai(self, *, api_key: str, model: str, system: str, messages: list[dict[str, Any]], temperature: float,
                               json_mode: bool, max_tokens: int | None) -> Completion:
        s = get_settings()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_completion_tokens": max_tokens or self._max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=s.codegen_request_timeout_seconds) as client:
                resp = await client.post(f"{s.codegen_openai_base_url.rstrip('/')}/chat/completions",
                                         headers={"Authorization": f"Bearer {api_key}"}, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            raise self._provider_failure("OpenAI", e) from None
        text = (data["choices"][0]["message"].get("content")) or ""
        usage = data.get("usage") or {}
        return Completion(text=text, model=data.get("model", model), input_tokens=int(usage.get("prompt_tokens", 0)),
                          output_tokens=int(usage.get("completion_tokens", 0)))

    async def _complete_anthropic(self, *, api_key: str, model: str, system: str, messages: list[dict[str, Any]], temperature: float,
                                  json_mode: bool, max_tokens: int | None) -> Completion:
        s = get_settings()
        turns = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages if m.get("role") != "system"]
        if not turns:
            turns = [{"role": "user", "content": "Please proceed."}]
        sys_prompt = system
        if json_mode:
            sys_prompt += "\n\nIMPORTANT: respond with one valid, parseable JSON object only — no explanation, no markdown fences."
        payload: dict[str, Any] = {"model": model, "system": sys_prompt, "messages": turns, "temperature": temperature,
                                   "max_tokens": max_tokens or self._max_tokens}
        try:
            async with httpx.AsyncClient(timeout=s.codegen_request_timeout_seconds) as client:
                resp = await client.post(f"{s.codegen_anthropic_base_url.rstrip('/')}/messages",
                                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            raise self._provider_failure("Anthropic", e) from None
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(text=text, model=data.get("model", model), input_tokens=int(usage.get("input_tokens", 0)),
                          output_tokens=int(usage.get("output_tokens", 0)))

    async def _complete_gateway(self, *, model: str, system: str, messages: list[dict[str, Any]], temperature: float = 0.6,
                                json_mode: bool = False, max_tokens: int | None = None) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if json_mode:
            # Supported by vLLM/LiteLLM OpenAI-compatible servers. If a given
            # gateway build ignores it, extract_json() still recovers the object.
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {}) or {}
        return Completion(
            text=choice,
            model=data.get("model", model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort parse of a JSON object from model output. Small models
    sometimes wrap JSON in ```json fences or add a sentence of preamble; this
    recovers the object rather than failing the whole turn. Raises ValueError
    only if nothing parseable is present."""
    text = text.strip()
    # 1) straight parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2) fenced block
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # 3) first {...} span
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON object found in model output")


_CODE_FENCE_RE = re.compile(r"```[\w.+-]*[^\S\n]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str | None:
    """Pull the substantive fenced code block out of a generation reply.
    Mirrors the old service's _extract_artifact so plain "just write me code"
    turns still yield an applyable artifact."""
    blocks = _CODE_FENCE_RE.findall(text)
    if not blocks:
        return None
    code = max(blocks, key=len).strip()
    return code or None
