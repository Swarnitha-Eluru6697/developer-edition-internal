"""
Speech-to-text for audio documents, so a .wav (and the other audio
formats below) can be ingested into a knowledge base as its transcript.

Audio has no text layer to extract: ingestion.parse_file's own
best-effort decode of a real 300KB WAV came through as 44% U+FFFD, which
is exactly why it is rejected there as binary. The only honest way to
ingest it is to actually transcribe it, which means a real speech
model and therefore a real credential — this module owns that, and
reports plainly when there isn't one rather than letting the browser
offer an audio file that will fail at the end of an upload.

Two real backends, both driven by whatever is already configured on the
Integrations page (no new credential to manage):

  OpenAI  — /v1/audio/transcriptions (whisper-1), the purpose-built
            endpoint, used first when an OpenAI key is present.
  Gemini  — generateContent with the audio inline, which the Flash
            models genuinely accept as input.

They are tried in order and the first one that returns a transcript
wins. Falling through matters here rather than being defensive
decoration: in this environment the stored OpenAI key is valid but the
account has no credit (a real 429 `credit_balance_exhausted`), and
Gemini transcribes the same file correctly — refusing at the first
provider would have meant no transcription at all despite a working one
being configured. Every provider's own failure reason is kept and
reported together if they all fail, so "it didn't work" always names
which provider said what.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Audio formats offered for ingestion. Deliberately the intersection of
# what the backends below actually accept and what people actually have:
# every one of these is documented as supported by the Whisper endpoint,
# and the Gemini Flash models accept the same set.
#
# Not here, on purpose: aac/wma/aiff (Whisper rejects them) and the video
# containers (mp4/mov/mkv/webm). Those stay on data_sources.NO_TEXT_LAYER
# with its existing "convert to a transcript first" note — offering a
# format a backend will reject is the empty-control problem again.
AUDIO_EXTENSIONS = {"wav", "mp3", "m4a", "flac", "ogg", "oga", "mpga"}

_MIME_BY_EXT = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mpga": "audio/mpeg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
}

# Uncompressed audio is big: one minute of 16-bit 44.1kHz stereo is
# ~10MB. The platform's own upload ceiling (uploads.MAX_UPLOAD_BYTES,
# 20MB) already bounds what can arrive here, and Gemini's inline-data
# path is bounded lower still — base64 inflates by 4/3, so 14MB of audio
# is an ~19MB request, just inside Gemini's own ~20MB request limit.
# Above that the file has to be compressed first; the error says so.
GEMINI_INLINE_MAX_BYTES = int(os.getenv("TRANSCRIPTION_GEMINI_INLINE_MAX_BYTES", str(14 * 1024 * 1024)))
# Whisper's own documented per-file limit.
OPENAI_MAX_BYTES = int(os.getenv("TRANSCRIPTION_OPENAI_MAX_BYTES", str(25 * 1024 * 1024)))

# Per-request timeout, and a ceiling on the whole attempt across every
# provider and model. Both matter because this runs while someone waits
# for an upload to finish: without the total budget, two providers times
# three Gemini models times a retry each is a worst case measured in
# tens of minutes, and the person watching has no idea whether it is
# still working.
TIMEOUT_SECONDS = float(os.getenv("TRANSCRIPTION_TIMEOUT_SECONDS", "120"))
TOTAL_BUDGET_SECONDS = float(os.getenv("TRANSCRIPTION_TOTAL_BUDGET_SECONDS", "300"))

_OPENAI_MODEL = os.getenv("TRANSCRIPTION_OPENAI_MODEL", "whisper-1")
# An alias rather than a pinned version, and for a real reason: pinned
# Gemini model ids are retired (`gemini-2.0-flash` and even
# `gemini-2.5-flash` both answer 404 "no longer available" against this
# environment's own key), while `gemini-flash-latest` resolves to
# whatever the current Flash actually is. Override to pin one.
# A LIST, not one id, and both halves of that matter. Pinned Gemini ids
# get retired — `gemini-2.0-flash` and `gemini-2.5-flash` both answer
# 404 "no longer available" against this environment's own key — so the
# first choice is the alias that always resolves to the current Flash.
# And a single model can be temporarily out of capacity: the first real
# upload through this code failed on HTTP 503 "experiencing high demand"
# from `gemini-flash-latest` while `gemini-3.5-flash` transcribed the
# same file immediately. Set TRANSCRIPTION_GEMINI_MODEL to pin exactly
# one.
_GEMINI_MODELS = [m.strip() for m in (
    os.getenv("TRANSCRIPTION_GEMINI_MODEL")
    or "gemini-flash-latest,gemini-3.5-flash,gemini-flash-lite-latest"
).split(",") if m.strip()]


def url_for(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Statuses that mean "the request was fine, the service wasn't".
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# One retry per model, then the next model — which is the better lever
# against a busy model anyway (a different model is a different capacity
# pool, as the real 503 below showed).
_GEMINI_BACKOFF_SECONDS = (4,)

_GEMINI_PROMPT = (
    "Transcribe this audio verbatim into plain text. Output only the transcript itself — "
    "no preamble, no commentary, no timestamps, no speaker labels unless they are spoken."
)


class TranscriptionError(RuntimeError):
    """No backend could produce a transcript. The message names each
    provider that was tried and what it actually said."""


@dataclass
class Transcript:
    text: str
    provider: str
    model: str


@dataclass
class Availability:
    """Whether an audio file can genuinely be turned into text right
    now — what the browse listing and the upload UI key off, so neither
    offers an audio file this deployment cannot process."""
    available: bool
    providers: List[str] = field(default_factory=list)
    extensions: List[str] = field(default_factory=list)
    reason: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {"available": self.available, "providers": self.providers,
                "extensions": self.extensions, "reason": self.reason}


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def is_audio(filename: str, content_type: str = "") -> bool:
    if ext_of(filename) in AUDIO_EXTENSIONS:
        return True
    # A content type is only trusted to CLASSIFY, never to pick a
    # backend — the extension does that, and a file with an audio MIME
    # but no known audio extension still has to name a format the
    # backends accept.
    return (content_type or "").split(";")[0].strip().lower() in set(_MIME_BY_EXT.values())


def _provider_key(display_name: str, *env_names: str) -> Optional[str]:
    """A usable key for one provider: what the Integrations page stored,
    else an environment variable. Same order the rest of the platform
    uses, so a key added in the UI takes effect without a restart."""
    from .model_adapter import _stored_provider_config
    try:
        key, _org, _base = _stored_provider_config(display_name)
    except Exception:
        key = None
    if key and key.strip():
        return key.strip()
    for name in env_names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def _configured() -> List[Tuple[str, Optional[str]]]:
    """The backends with a credential, in the order they get tried.

    TRANSCRIPTION_PROVIDER pins one ("OpenAI" or "Gemini"), for a
    deployment that wants its audio going to exactly one vendor.
    """
    order: List[Tuple[str, Optional[str]]] = [
        ("OpenAI", _provider_key("OpenAI", "OPENAI_API_KEY")),
        ("Gemini", _provider_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ]
    pinned = (os.getenv("TRANSCRIPTION_PROVIDER") or "").strip().lower()
    if pinned:
        order = [(name, key) for name, key in order if name.lower() == pinned]
    return [(name, key) for name, key in order if key]


_AVAILABILITY_TTL_SECONDS = 30.0
_availability_cache: Tuple[float, Optional[Availability]] = (0.0, None)


def availability(*, refresh: bool = False) -> Availability:
    """Cached briefly because it is asked per FILE: a folder listing of
    200 entries would otherwise open 200 short-lived DB sessions to ask
    the same question. 30 seconds is short enough that adding a key on
    the Integrations page shows up while someone is still looking at the
    same page."""
    global _availability_cache
    cached_at, cached = _availability_cache
    if cached is not None and not refresh and (time.monotonic() - cached_at) < _AVAILABILITY_TTL_SECONDS:
        return cached

    providers = [name for name, _ in _configured()]
    if providers:
        result = Availability(True, providers, sorted(AUDIO_EXTENSIONS))
    else:
        pinned = (os.getenv("TRANSCRIPTION_PROVIDER") or "").strip()
        result = Availability(
            False, [], sorted(AUDIO_EXTENSIONS),
            reason=(
                f"Audio needs transcribing before it can be ingested, and no API key is "
                f"configured for a provider that can do it. Add an OpenAI or Gemini key under "
                f"Integrations → Model Providers."
                + (f" (TRANSCRIPTION_PROVIDER is pinned to '{pinned}'.)" if pinned else "")
            ),
        )
    _availability_cache = (time.monotonic(), result)
    return result


def _transcribe_openai(filename: str, content: bytes, key: str, deadline: float) -> Tuple[str, str]:
    if len(content) > OPENAI_MAX_BYTES:
        raise RuntimeError(
            f"file is {len(content) // (1024 * 1024)}MB, over the "
            f"{OPENAI_MAX_BYTES // (1024 * 1024)}MB limit of the transcription endpoint"
        )
    import openai
    from .egress import governed_httpx_client
    from .model_adapter import _stored_provider_config
    try:
        _key, _org, base_url = _stored_provider_config("OpenAI")
    except Exception:
        base_url = None
    client = openai.OpenAI(
        api_key=key, http_client=governed_httpx_client(),
        timeout=max(5.0, min(TIMEOUT_SECONDS, deadline - time.monotonic())),
        # The SDK retries 429s on its own, honouring a Retry-After that
        # on an out-of-credit account can be long — which is how one
        # audio upload held the request open for minutes before giving
        # up. transcribe() already has a better answer to a failure
        # here: try the next provider.
        max_retries=0,
        **({"base_url": base_url} if base_url else {}))
    mime = _MIME_BY_EXT.get(ext_of(filename), "application/octet-stream")
    result = client.audio.transcriptions.create(
        model=_OPENAI_MODEL,
        file=(filename or "audio", content, mime),
        response_format="text",
    )
    # response_format="text" gives a bare string; the SDK still returns
    # an object for the json formats, so handle both.
    text = result if isinstance(result, str) else (getattr(result, "text", "") or "")
    return text, _OPENAI_MODEL


def _transcribe_gemini(filename: str, content: bytes, key: str, deadline: float) -> Tuple[str, str]:
    if len(content) > GEMINI_INLINE_MAX_BYTES:
        raise RuntimeError(
            f"file is {len(content) // (1024 * 1024)}MB, over the "
            f"{GEMINI_INLINE_MAX_BYTES // (1024 * 1024)}MB this backend sends inline — "
            f"compress it to MP3 or M4A first"
        )
    from .egress import governed_httpx_client
    mime = _MIME_BY_EXT.get(ext_of(filename), "audio/wav")
    payload = {
        "contents": [{"parts": [
            {"text": _GEMINI_PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(content).decode()}},
        ]}],
        # A transcript is not a creative task: temperature 0 so the same
        # audio ingests as the same text twice.
        "generationConfig": {"temperature": 0},
    }

    failures: List[str] = []
    for model in _GEMINI_MODELS:
        body = None
        # A short retry on the statuses that genuinely mean "ask again",
        # and only those. Seen on the very first real run of this code:
        # HTTP 503 "This model is currently experiencing high demand.
        # Spikes in demand are usually temporary" — a transient
        # condition that a single attempt turns into a failed upload.
        for pause in _GEMINI_BACKOFF_SECONDS + (None,):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append("ran out of time before every model had been tried")
                raise RuntimeError("; ".join(failures))
            with governed_httpx_client(timeout=max(5.0, min(TIMEOUT_SECONDS, remaining))) as client:
                r = client.post(url_for(model),
                                headers={"x-goog-api-key": key, "content-type": "application/json"},
                                json=payload)
            if r.status_code < 400:
                body = r.json()
                break
            detail = f"{model} HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in _RETRYABLE_STATUS and pause is not None:
                time.sleep(pause)
                continue
            # 404 is this model, not this request: Google retires
            # pinned ids ("no longer available"), so move to the next
            # candidate. A 400/401/403 is about the key or the payload
            # and every other model would answer it identically —
            # trying five of them would just be five identical errors.
            failures.append(detail)
            if r.status_code == 404 or r.status_code in _RETRYABLE_STATUS:
                break
            raise RuntimeError("; ".join(failures))
        if body is None:
            continue  # this model is exhausted; the next one may have capacity

        candidates = body.get("candidates") or []
        if not candidates:
            # A prompt-feedback block (safety, recitation) is a real,
            # explainable outcome, not an empty response.
            feedback = body.get("promptFeedback") or {}
            failures.append(f"{model}: no transcript returned ({feedback or 'empty response'})")
            continue
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if text.strip():
            return text, model
        failures.append(f"{model}: empty transcript")

    raise RuntimeError("; ".join(failures) or "no Gemini model returned a transcript")


_BACKENDS = {"OpenAI": _transcribe_openai, "Gemini": _transcribe_gemini}


def transcribe(filename: str, content: bytes, content_type: str = "") -> Transcript:
    """The transcript of one audio file, or TranscriptionError naming
    every provider that was tried and what each said."""
    if not content:
        raise TranscriptionError(f"'{filename}' is empty — there is no audio to transcribe.")
    configured = _configured()
    if not configured:
        raise TranscriptionError(availability(refresh=True).reason or "No transcription provider configured.")

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    failures: List[str] = []
    for name, key in configured:
        try:
            raw, model = _BACKENDS[name](filename, content, key, deadline)
        except Exception as e:
            failures.append(f"{name}: {e}")
            continue
        text = (raw or "").strip()
        if not text:
            failures.append(f"{name}: returned an empty transcript")
            continue
        return Transcript(text=text, provider=name, model=model)

    raise TranscriptionError(
        f"Could not transcribe '{filename}'. " + "; ".join(failures)
    )
