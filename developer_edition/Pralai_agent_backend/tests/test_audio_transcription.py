"""Tests for ingesting audio — a .wav becoming a knowledge base document.

Audio was refused outright: ingestion.parse_file's best-effort decode of
a WAV produces mojibake, _looks_binary catches it, and the upload ended
with "contains no extractable text". There is nothing to extract, so the
only honest support is real transcription, which this covers end to end
apart from the network call itself.

Verified live, not only here: a real 320KB spoken WAV (macOS `say` ->
afconvert) went through POST /ingest into the `audio_transcripts`
pipeline and came back out of Postgres as "Acme Corporation refund
policy. Customers may request a refund within 30 days of purchase.
Enterprise contracts renew annually on the 1st of April." — transcribed
by Gemini after the stored OpenAI key answered 429
`credit_balance_exhausted`. What is tested below is the logic around
that call: which files are offered, which provider is chosen, what
happens when one fails, and what the caller is told.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import data_sources, ingestion, transcription, upload_security


@pytest.fixture(autouse=True)
def _clear_availability_cache(monkeypatch):
    """availability() memoises for 30s, which is right in production and
    wrong across tests — one test's "no keys configured" would still be
    the answer in the next."""
    monkeypatch.setattr(transcription, "_availability_cache", (0.0, None))
    for var in ("TRANSCRIPTION_PROVIDER", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
    monkeypatch.setattr(transcription, "_availability_cache", (0.0, None))


def _no_stored_keys(monkeypatch):
    monkeypatch.setattr("app.model_adapter._stored_provider_config",
                        lambda name: (None, None, None))


def _stored_key(monkeypatch, provider, key="sk-test"):
    def fake(name):
        return (key, None, None) if name == provider else (None, None, None)
    monkeypatch.setattr("app.model_adapter._stored_provider_config", fake)


class TestWhichFilesAreAudio:
    def test_the_extensions_every_backend_accepts(self):
        assert transcription.AUDIO_EXTENSIONS == {"wav", "mp3", "m4a", "flac", "ogg", "oga", "mpga"}

    def test_recognised_by_extension_case_insensitively(self):
        assert transcription.is_audio("Quarterly Review.WAV")
        assert transcription.is_audio("notes.mp3")

    def test_video_is_not_audio(self):
        """There is no video path here. Claiming one would mean an .mp4
        offered in the picker and refused at the end of the upload."""
        for name in ("demo.mp4", "screencast.mov", "clip.webm", "recording.mkv"):
            assert not transcription.is_audio(name)

    def test_formats_the_backends_reject_are_not_offered(self):
        """aac/wma/aiff are real audio that Whisper does not accept.
        Offering them would be a control that cannot work."""
        for name in ("voice.aac", "voice.wma", "voice.aiff"):
            assert not transcription.is_audio(name)

    def test_an_audio_content_type_counts_even_with_an_odd_name(self):
        assert transcription.is_audio("recording", "audio/wav")
        assert transcription.is_audio("recording", "audio/mpeg; codecs=mp3")

    def test_a_text_file_is_never_audio(self):
        assert not transcription.is_audio("notes.md", "text/markdown")


class TestAvailability:
    def test_no_key_means_not_available_and_says_what_to_add(self, monkeypatch):
        _no_stored_keys(monkeypatch)
        result = transcription.availability(refresh=True)
        assert result.available is False
        assert "OpenAI or Gemini" in result.reason
        # The formats are still reported, so the UI can explain what
        # WOULD be ingestible once a key exists.
        assert "wav" in result.extensions

    def test_a_stored_openai_key_is_enough(self, monkeypatch):
        _stored_key(monkeypatch, "OpenAI")
        result = transcription.availability(refresh=True)
        assert result.available is True
        assert result.providers == ["OpenAI"]
        assert result.reason is None

    def test_a_stored_gemini_key_is_enough_on_its_own(self, monkeypatch):
        _stored_key(monkeypatch, "Gemini")
        result = transcription.availability(refresh=True)
        assert result.available is True
        assert result.providers == ["Gemini"]

    def test_an_environment_key_counts_too(self, monkeypatch):
        _no_stored_keys(monkeypatch)
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        assert transcription.availability(refresh=True).providers == ["Gemini"]

    def test_the_cache_is_used_but_refresh_overrides_it(self, monkeypatch):
        _no_stored_keys(monkeypatch)
        assert transcription.availability(refresh=True).available is False
        _stored_key(monkeypatch, "OpenAI")
        # Cached: a key added a moment ago is not seen yet.
        assert transcription.availability().available is False
        # The endpoint the UI calls passes refresh=True for exactly this.
        assert transcription.availability(refresh=True).available is True

    def test_a_pinned_provider_excludes_the_other(self, monkeypatch):
        monkeypatch.setattr("app.model_adapter._stored_provider_config",
                            lambda name: ("key", None, None))
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "gemini")
        assert transcription.availability(refresh=True).providers == ["Gemini"]

    def test_a_pinned_provider_with_no_key_names_the_pin(self, monkeypatch):
        _stored_key(monkeypatch, "OpenAI")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "Gemini")
        result = transcription.availability(refresh=True)
        assert result.available is False
        assert "TRANSCRIPTION_PROVIDER" in result.reason


class TestProviderFallthrough:
    """Not defensive decoration: this environment's own stored OpenAI key
    is valid but the account has no credit (429
    `credit_balance_exhausted`) while Gemini transcribes the same file.
    Stopping at the first provider would mean no transcription at all
    despite a working one being configured.
    """

    def test_a_failing_first_provider_falls_through_to_the_second(self, monkeypatch):
        monkeypatch.setattr("app.model_adapter._stored_provider_config",
                            lambda name: ("key", None, None))
        monkeypatch.setitem(transcription._BACKENDS, "OpenAI",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("429 no credits")))
        monkeypatch.setitem(transcription._BACKENDS, "Gemini",
                            lambda *a: ("the transcript", "gemini-flash-latest"))
        result = transcription.transcribe("a.wav", b"RIFF....WAVE")
        assert result.text == "the transcript"
        assert result.provider == "Gemini"
        # The model that actually ran, not the first one configured —
        # the Gemini backend walks a list of them.
        assert result.model == "gemini-flash-latest"

    def test_every_failure_is_reported_together_naming_each_provider(self, monkeypatch):
        monkeypatch.setattr("app.model_adapter._stored_provider_config",
                            lambda name: ("key", None, None))
        monkeypatch.setitem(transcription._BACKENDS, "OpenAI",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("429 no credits")))
        monkeypatch.setitem(transcription._BACKENDS, "Gemini",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("503 high demand")))
        with pytest.raises(transcription.TranscriptionError) as e:
            transcription.transcribe("a.wav", b"RIFF....WAVE")
        assert "OpenAI: 429 no credits" in str(e.value)
        assert "Gemini: 503 high demand" in str(e.value)
        assert "a.wav" in str(e.value)

    def test_an_empty_transcript_is_a_failure_not_a_success(self, monkeypatch):
        _stored_key(monkeypatch, "Gemini")
        monkeypatch.setitem(transcription._BACKENDS, "Gemini", lambda *a: ("   ", "m"))
        with pytest.raises(transcription.TranscriptionError) as e:
            transcription.transcribe("a.wav", b"RIFF")
        assert "empty transcript" in str(e.value)

    def test_no_provider_configured_explains_rather_than_failing_obscurely(self, monkeypatch):
        _no_stored_keys(monkeypatch)
        with pytest.raises(transcription.TranscriptionError) as e:
            transcription.transcribe("a.wav", b"RIFF")
        assert "Integrations" in str(e.value)

    def test_an_empty_file_is_rejected_before_any_provider_is_called(self, monkeypatch):
        called = []
        monkeypatch.setattr("app.model_adapter._stored_provider_config",
                            lambda name: ("key", None, None))
        monkeypatch.setitem(transcription._BACKENDS, "OpenAI",
                            lambda *a: called.append(a) or ("x", "m"))
        with pytest.raises(transcription.TranscriptionError):
            transcription.transcribe("silence.wav", b"")
        assert called == []


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for governed_httpx_client, handing out queued responses
    and recording which model URL each request went to."""

    def __init__(self, responses, seen):
        self._responses = responses
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self._seen.append(url)
        return self._responses.pop(0)


def _fake_gemini(monkeypatch, responses):
    seen = []
    monkeypatch.setattr("app.egress.governed_httpx_client",
                        lambda **kw: _FakeClient(responses, seen))
    monkeypatch.setattr(transcription, "_GEMINI_BACKOFF_SECONDS", (0, 0))
    monkeypatch.setattr(transcription.time, "sleep", lambda s: None)
    return seen


def _ok(text):
    return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


class TestGeminiBackend:
    """The retry and model-fallback behaviour, which exists because of
    what the real API actually did: the first real upload through this
    code got HTTP 503 "This model is currently experiencing high demand"
    from gemini-flash-latest, while gemini-3.5-flash transcribed the same
    file immediately. Retired model ids are the other real case —
    gemini-2.0-flash and gemini-2.5-flash both answer 404 "no longer
    available" against this environment's key.
    """

    def test_a_transient_503_is_retried_on_the_same_model(self, monkeypatch):
        seen = _fake_gemini(monkeypatch, [
            _FakeResponse(503, text="high demand"),
            _ok("transcript text"),
        ])
        text, model = transcription._transcribe_gemini("a.wav", b"RIFF", "key", time.monotonic() + 60)
        assert text == "transcript text"
        assert model == transcription._GEMINI_MODELS[0]
        assert len(seen) == 2 and seen[0] == seen[1]

    def test_an_exhausted_model_hands_over_to_the_next_one(self, monkeypatch):
        seen = _fake_gemini(monkeypatch, [
            _FakeResponse(503, text="high demand"),
            _FakeResponse(503, text="high demand"),
            _FakeResponse(503, text="high demand"),
            _ok("from the second model"),
        ])
        text, model = transcription._transcribe_gemini("a.wav", b"RIFF", "key", time.monotonic() + 60)
        assert text == "from the second model"
        assert model == transcription._GEMINI_MODELS[1]

    def test_a_retired_model_id_moves_on_without_retrying_it(self, monkeypatch):
        seen = _fake_gemini(monkeypatch, [
            _FakeResponse(404, text="no longer available"),
            _ok("from the next model"),
        ])
        text, model = transcription._transcribe_gemini("a.wav", b"RIFF", "key", time.monotonic() + 60)
        assert text == "from the next model"
        # Exactly two calls: a 404 is about the model, so asking it again
        # is pointless, but the next candidate is worth trying.
        assert len(seen) == 2
        assert transcription._GEMINI_MODELS[0] in seen[0]
        assert transcription._GEMINI_MODELS[1] in seen[1]

    def test_a_bad_key_fails_immediately_rather_than_asking_five_models(self, monkeypatch):
        seen = _fake_gemini(monkeypatch, [_FakeResponse(401, text="API key not valid")])
        with pytest.raises(RuntimeError) as e:
            transcription._transcribe_gemini("a.wav", b"RIFF", "key", time.monotonic() + 60)
        assert "API key not valid" in str(e.value)
        assert len(seen) == 1

    def test_a_safety_block_is_reported_as_what_it_is(self, monkeypatch):
        _fake_gemini(monkeypatch, [
            _FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}),
            _FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}),
            _FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}),
        ])
        with pytest.raises(RuntimeError) as e:
            transcription._transcribe_gemini("a.wav", b"RIFF", "key", time.monotonic() + 60)
        assert "SAFETY" in str(e.value)

    def test_the_audio_goes_up_with_its_own_mime_type(self, monkeypatch):
        captured = {}

        class Recording(_FakeClient):
            def post(self, url, **kwargs):
                captured.update(kwargs.get("json") or {})
                return _ok("t")

        monkeypatch.setattr("app.egress.governed_httpx_client",
                            lambda **kw: Recording([], []))
        transcription._transcribe_gemini("interview.flac", b"fLaC-bytes", "key", time.monotonic() + 60)
        part = captured["contents"][0]["parts"][1]["inline_data"]
        assert part["mime_type"] == "audio/flac"
        # temperature 0: the same audio has to ingest as the same text.
        assert captured["generationConfig"]["temperature"] == 0

    def test_a_file_too_large_to_send_inline_says_so(self, monkeypatch):
        with pytest.raises(RuntimeError) as e:
            transcription._transcribe_gemini(
                "long.wav", b"x" * (transcription.GEMINI_INLINE_MAX_BYTES + 1), "key",
                time.monotonic() + 60)
        assert "compress it" in str(e.value)


class TestParseFileRoutesAudio:
    def test_a_wav_becomes_its_transcript(self, monkeypatch):
        monkeypatch.setattr(transcription, "transcribe",
                            lambda f, c, ct="": transcription.Transcript(
                                "Customers may request a refund within thirty days.",
                                "Gemini", "gemini-flash-latest"))
        text = ingestion.parse_file("policy.wav", b"RIFF....WAVE", "audio/wav")
        assert text.startswith("Customers may request a refund")

    def test_provenance_is_handed_back_when_the_caller_asks_for_it(self, monkeypatch):
        monkeypatch.setattr(transcription, "transcribe",
                            lambda f, c, ct="": transcription.Transcript(
                                "hello there", "OpenAI", "whisper-1"))
        notes = {}
        ingestion.parse_file("policy.wav", b"RIFF", "audio/wav", notes)
        assert notes["transcription"] == {"provider": "OpenAI", "model": "whisper-1",
                                          "characters": len("hello there")}

    def test_a_transcription_failure_raises_the_ValueError_callers_expect(self, monkeypatch):
        """Every upload endpoint turns a ValueError from parse_file into
        a 400 naming the file; a TranscriptionError leaking out would be
        a 500 instead."""
        def boom(f, c, ct=""):
            raise transcription.TranscriptionError("Could not transcribe 'policy.wav'. OpenAI: 429")
        monkeypatch.setattr(transcription, "transcribe", boom)
        with pytest.raises(ValueError) as e:
            ingestion.parse_file("policy.wav", b"RIFF", "audio/wav")
        assert "429" in str(e.value)

    def test_a_text_file_never_reaches_a_speech_provider(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("transcription must not be called for text")
        monkeypatch.setattr(transcription, "transcribe", boom)
        assert "hello" in ingestion.parse_file("notes.md", b"# hello", "text/markdown")

    def test_an_unsupported_media_file_is_still_refused_but_now_mentions_audio(self):
        """A video file cannot be ingested, and the message that says so
        also has to stop claiming audio can't be — it can."""
        with pytest.raises(ValueError) as e:
            ingestion.parse_file("clip.mp4", bytes(range(256)) * 20, "video/mp4")
        message = str(e.value)
        assert "Video and image files have no text layer" in message
        assert "transcribed automatically" in message


class TestWhatTheBrowserOffers:
    def test_audio_is_offered_only_when_it_can_actually_be_transcribed(self, monkeypatch):
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(True, ["Gemini"], ["wav"]))
        assert data_sources._is_ingestible("standup.wav") is True
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(False, [], ["wav"], "no key"))
        assert data_sources._is_ingestible("standup.wav") is False

    def test_the_note_explains_which_of_the_two_it_is(self, monkeypatch):
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(True, ["Gemini"], ["wav"]))
        assert data_sources.file_note("standup.wav") == "audio — transcribed on ingest"
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(False, [], ["wav"], "no key"))
        assert "add an OpenAI or Gemini key" in data_sources.file_note("standup.wav")

    def test_video_keeps_the_plain_binary_note_whatever_is_configured(self, monkeypatch):
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(True, ["Gemini"], ["wav"]))
        assert data_sources.file_note("demo.mp4") == "no text to extract (binary format)"
        assert data_sources._is_ingestible("demo.mp4") is False

    def test_text_files_are_unaffected(self, monkeypatch):
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(False, [], [], "no key"))
        assert data_sources._is_ingestible("schema.sql") is True
        assert data_sources.file_note("schema.sql") == ""

    def test_a_secret_named_file_stays_excluded_even_as_audio(self, monkeypatch):
        monkeypatch.setattr(transcription, "availability",
                            lambda **kw: transcription.Availability(True, ["Gemini"], ["wav"]))
        assert data_sources._is_ingestible(".env") is False


class TestAudioGetsPastUploadValidation:
    """Staging a file for the Upload API goes through
    upload_security.validate_content_type, whose allowlist is checked
    against SNIFFED bytes. A WAV sniffed as nothing at all fell back to
    its declared type, which was not on the list — so a .wav could be
    ingested directly but not staged.
    """

    WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 32

    def test_a_wav_is_recognised_from_its_bytes(self):
        assert upload_security.sniff_content_type(self.WAV) == "audio/wav"

    def test_a_wav_passes_the_allowlist(self):
        allowed, effective, reason = upload_security.validate_content_type(self.WAV, "audio/wav")
        assert allowed is True and effective == "audio/wav" and reason is None

    def test_an_avi_is_not_mistaken_for_audio(self):
        """RIFF is a container, not a format: the form type at offset 8
        is the only thing that separates a WAV from an AVI."""
        avi = b"RIFF" + (36).to_bytes(4, "little") + b"AVI LIST" + b"\x00" * 32
        assert upload_security.sniff_content_type(avi) != "audio/wav"

    def test_an_mp3_with_an_id3_tag_is_recognised(self):
        assert upload_security.sniff_content_type(b"ID3\x03\x00" + b"\x00" * 40) == "audio/mpeg"

    def test_flac_and_ogg_are_recognised(self):
        assert upload_security.sniff_content_type(b"fLaC" + b"\x00" * 40) == "audio/flac"
        assert upload_security.sniff_content_type(b"OggS" + b"\x00" * 40) == "audio/ogg"

    def test_an_m4a_is_recognised_but_an_mp4_video_is_not(self):
        m4a = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32
        mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
        assert upload_security.sniff_content_type(m4a) == "audio/mp4"
        assert upload_security.sniff_content_type(mp4) != "audio/mp4"

    def test_random_binary_is_not_waved_through_as_audio(self):
        """The loosest possible audio signal — an MPEG frame sync — is
        two bytes wide and matches plenty of unrelated binary. Sniffing
        it would ALLOW that binary through the allowlist, so it is
        deliberately not sniffed."""
        assert upload_security.sniff_content_type(b"\xff\xfb" + bytes(range(200))) is None
        allowed, _effective, reason = upload_security.validate_content_type(
            b"\xff\xfb" + bytes(range(200)), "application/x-executable")
        assert allowed is False and reason
