"""Tests for embedding under a provider rate limit.

The real failure: a pipeline using Cohere's embed-english-v3.0 on a
trial key (100,000 tokens/minute) reported

    Could not compute embeddings for this knowledge base: Error code: 429
    - {'message': 'trial token rate limit exceeded, ...'}

and the whole ingest failed, having written nothing. Batches were
already sent 64 at a time but back to back, with no pacing and no
retry — so a document big enough to exhaust the per-minute budget could
never be ingested at all, rather than simply taking longer.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import embeddings


class _Resp:
    def __init__(self, n, dim=4):
        self.data = [types.SimpleNamespace(embedding=[0.1] * dim) for _ in range(n)]


class _FakeEmbeddings:
    def __init__(self, fail_times, exc_factory):
        self.calls = 0
        self.fail_times = fail_times
        self.exc_factory = exc_factory

    def create(self, model, input):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return _Resp(len(input))


class _FakeClient:
    def __init__(self, fail_times, exc_factory):
        self.embeddings = _FakeEmbeddings(fail_times, exc_factory)


def _rate_limit_error(retry_after=None):
    import openai
    import httpx
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    request = httpx.Request("POST", "https://example.test/v1/embeddings")
    response = httpx.Response(429, headers=headers, request=request, json={"message": "slow down"})
    return openai.RateLimitError("rate limited", response=response, body=None)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """The waits are the point of the feature, but nobody needs to sit
    through them; the durations are asserted separately."""
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    return slept


class TestRetryAfterParsing:
    def test_the_providers_own_retry_after_is_preferred(self):
        """The service knows when its window resets; waiting longer than
        it says is as wasteful as retrying too soon."""
        assert embeddings._retry_after_seconds(_rate_limit_error(12)) == 12.0

    def test_a_missing_or_unparseable_header_falls_back_to_the_schedule(self):
        assert embeddings._retry_after_seconds(_rate_limit_error()) is None
        assert embeddings._retry_after_seconds(Exception("no response")) is None


class TestBatchRetry:
    def test_a_transient_rate_limit_is_waited_out_and_the_batch_succeeds(self, no_real_sleeping):
        client = _FakeClient(fail_times=2, exc_factory=lambda: _rate_limit_error())
        out = embeddings._embed_batch_with_retry(client, "embed-english-v3.0", ["a", "b"], "Cohere")
        assert len(out) == 2
        assert client.embeddings.calls == 3          # two refusals, then success
        assert no_real_sleeping == [5, 15]           # the documented backoff

    def test_the_providers_retry_after_overrides_the_schedule(self, no_real_sleeping):
        client = _FakeClient(fail_times=1, exc_factory=lambda: _rate_limit_error(3))
        embeddings._embed_batch_with_retry(client, "m", ["a"], "Cohere")
        assert no_real_sleeping == [3.0]

    def test_giving_up_names_the_provider_and_what_to_do(self, no_real_sleeping):
        """The raw vendor payload said nothing about which provider
        imposed the cap or how to proceed."""
        client = _FakeClient(fail_times=99, exc_factory=lambda: _rate_limit_error())
        with pytest.raises(RuntimeError) as excinfo:
            embeddings._embed_batch_with_retry(client, "m", ["a"], "Cohere")
        message = str(excinfo.value)
        assert "Cohere rate-limited" in message
        assert "trial key" in message
        assert "fewer or smaller documents" in message
        assert client.embeddings.calls == embeddings._EMBED_MAX_ATTEMPTS

    def test_a_non_rate_limit_error_is_not_retried(self, no_real_sleeping):
        """A bad key or wrong model id fails identically every time;
        retrying only delays an error the caller needs."""
        class Boom(Exception):
            pass

        client = _FakeClient(fail_times=99, exc_factory=Boom)
        with pytest.raises(Boom):
            embeddings._embed_batch_with_retry(client, "m", ["a"], "Cohere")
        assert client.embeddings.calls == 1
        assert no_real_sleeping == []
