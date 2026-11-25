"""RUN-FLINK-011 — execute_statements() used to report only op_handle/
state per statement; job_service.py then had to go find the Flink job ID
itself afterwards by listing every job in the cluster and guessing which
one was "ours" (by name prefix, or just picking whatever was RUNNING).
The Gateway already knows the exact job ID the moment a submitting
statement finishes — this is what actually reads it, from the statement's
own operation result, instead of a global scan happening one level up.
"""
from __future__ import annotations

from app.executors.flink_gateway import FlinkGatewayClient, _extract_job_id_from_result

GATEWAY_URL = "http://localhost:8083"


# ---------------------------------------------------------------------
# _extract_job_id_from_result — pure
# ---------------------------------------------------------------------
def test_extract_prefers_top_level_job_id_field():
    payload = {"jobID": "abc123", "results": {"columns": [], "data": []}}
    assert _extract_job_id_from_result(payload) == "abc123"


def test_extract_falls_back_to_the_job_id_result_column():
    payload = {
        "results": {
            "columns": [{"name": "job id"}],
            "data": [{"kind": "INSERT", "fields": ["deadbeefcafebabe0123456789abcdef"]}],
        },
    }
    assert _extract_job_id_from_result(payload) == "deadbeefcafebabe0123456789abcdef"


def test_extract_column_match_is_case_and_whitespace_insensitive():
    payload = {
        "results": {
            "columns": [{"name": " Job ID "}],
            "data": [{"fields": ["xyz"]}],
        },
    }
    assert _extract_job_id_from_result(payload) == "xyz"


def test_extract_finds_the_job_id_column_even_when_not_first():
    payload = {
        "results": {
            "columns": [{"name": "some_other_field"}, {"name": "job id"}],
            "data": [{"fields": ["ignored", "the-real-id"]}],
        },
    }
    assert _extract_job_id_from_result(payload) == "the-real-id"


def test_extract_returns_none_for_a_ddl_result_with_no_job_id_column():
    payload = {"results": {"columns": [{"name": "result"}], "data": [{"fields": ["OK"]}]}}
    assert _extract_job_id_from_result(payload) is None


def test_extract_returns_none_for_an_empty_result():
    assert _extract_job_id_from_result({}) is None
    assert _extract_job_id_from_result({"results": {}}) is None


# ---------------------------------------------------------------------
# execute_statements — the exact job ID is captured per statement,
# never by listing every job afterwards.
# ---------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeHttpClient:
    """Scripts responses by URL suffix — enough to drive
    execute_statements() through create -> status -> (maybe) result for
    each statement, without touching the network or any mocking library."""

    def __init__(self, result_by_op: dict[str, dict]):
        self._result_by_op = result_by_op
        self._op_counter = 0
        self.result_fetches: list[str] = []  # op_handles whose result/0 was fetched

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        assert url.endswith("/statements")
        self._op_counter += 1
        op_handle = f"op-{self._op_counter}"
        return _FakeResponse({"operationHandle": op_handle})

    async def get(self, url):
        if url.endswith("/status"):
            return _FakeResponse({"status": "FINISHED"})
        if "/result/0" in url:
            op_handle = url.split("/operations/")[1].split("/result/0")[0]
            self.result_fetches.append(op_handle)
            return _FakeResponse(self._result_by_op.get(op_handle, {}))
        raise AssertionError(f"unexpected GET {url}")


def _client() -> FlinkGatewayClient:
    c = FlinkGatewayClient.__new__(FlinkGatewayClient)
    c._gw = GATEWAY_URL
    c._jm = "http://localhost:8081"
    c._timeout = 5
    c._submit_timeout = 5
    return c


async def test_execute_statements_captures_the_job_id_for_an_insert(monkeypatch):
    # op-1 = CREATE TABLE (no job id), op-2 = INSERT (has one)
    fake = _FakeHttpClient(result_by_op={
        "op-2": {"jobID": "job-abc-123"},
    })
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    client = _client()
    sql = "CREATE TABLE t (id STRING) WITH ('connector'='kafka');\nINSERT INTO t SELECT 1;"
    results = await client.execute_statements("sess-1", sql)

    assert len(results) == 2
    assert results[0]["flink_job_id"] is None       # CREATE TABLE
    assert results[1]["flink_job_id"] == "job-abc-123"  # INSERT
    # Only the job-submitting statement's result was ever fetched — no
    # result/0 lookup wasted on the DDL, and definitely no /v1/jobs scan.
    assert fake.result_fetches == ["op-2"]


async def test_execute_statements_result_fetch_is_skipped_for_ddl_only_sql(monkeypatch):
    fake = _FakeHttpClient(result_by_op={})
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    client = _client()
    results = await client.execute_statements("sess-1", "CREATE TABLE t (id STRING) WITH ('connector'='kafka');")

    assert results[0]["flink_job_id"] is None
    assert fake.result_fetches == []  # never even asked


async def test_execute_statements_captures_the_job_id_for_a_compiled_statement_set(monkeypatch):
    """Two INSERTs in one job's SQL get compiled into a single STATEMENT
    SET (see execute_statements' own compaction) — the ID still comes
    from that one statement's own result."""
    fake = _FakeHttpClient(result_by_op={"op-1": {"jobID": "statement-set-job-id"}})
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    client = _client()
    sql = "INSERT INTO sink_a SELECT 1;\nINSERT INTO sink_b SELECT 2;"
    results = await client.execute_statements("sess-1", sql)

    assert len(results) == 1
    assert results[0]["flink_job_id"] == "statement-set-job-id"


# ---------------------------------------------------------------------
# RUN-FLINK-011/013: job id unreadable after submit; on_submit ordering
# ---------------------------------------------------------------------
async def test_unreadable_job_id_after_a_finished_insert_raises_instead_of_returning_none(monkeypatch):
    import pytest
    from app.executors.flink_gateway import JobIdUnavailableError

    fake = _FakeHttpClient(result_by_op={})        # result/0 has no job id
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)
    with pytest.raises(JobIdUnavailableError) as exc:
        await _client().execute_statements("sess-1", "INSERT INTO t SELECT 1;")
    assert exc.value.op_handle == "op-1" and exc.value.session_id == "sess-1"


async def test_result_fetch_http_error_is_job_id_unavailable_not_silent(monkeypatch):
    import httpx
    import pytest
    from app.executors.flink_gateway import JobIdUnavailableError

    class _Flaky(_FakeHttpClient):
        async def get(self, url):
            if "/result/0" in url:
                raise httpx.ConnectError("reset")
            return await super().get(url)

    fake = _Flaky(result_by_op={})
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)
    with pytest.raises(JobIdUnavailableError):
        await _client().execute_statements("sess-1", "INSERT INTO t SELECT 1;")


async def test_on_submit_receives_the_operation_handle_before_polling(monkeypatch):
    events = []

    class _Tracking(_FakeHttpClient):
        async def get(self, url):
            if url.endswith("/status"):
                events.append("poll")
            return await super().get(url)

    fake = _Tracking(result_by_op={"op-2": {"jobID": "j-2"}})
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    async def _on_submit(op_handle, stmt):
        events.append(("submitted", op_handle))

    results = await _client().execute_statements(
        "sess-1", "CREATE TABLE t (a INT) WITH ('connector'='blackhole'); INSERT INTO t SELECT 1;",
        on_submit=_on_submit)
    # DDL never triggers on_submit; the INSERT's handle is recorded before its poll.
    assert events == ["poll", ("submitted", "op-2"), "poll"]
    assert results[1]["kind"] == "INSERT" and results[1]["flink_job_id"] == "j-2"
