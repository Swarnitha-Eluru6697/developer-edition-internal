import base64
import json

import httpx
import pytest

from app.core.config import settings
from app.services import ucs_quality_sync


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


@pytest.fixture(autouse=True)
def _enable_sync(monkeypatch):
    monkeypatch.setattr(settings, "ucs_quality_sync_enabled", True)


def _mock_get_client(monkeypatch, handler):
    """Same MockTransport pattern as test_ucs_lineage_sync.py — patching
    ucs_lineage_sync.get_client covers this module too, since
    emit_quality_result goes through ucs_lineage_sync.send_ol_event."""
    requests: list[httpx.Request] = []

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    shared_client = httpx.AsyncClient(
        base_url="http://ucs:8000", transport=httpx.MockTransport(_capturing_handler)
    )

    async def _fake_get_client(base_url: str):
        assert base_url == settings.ucs_base_url
        return shared_client

    monkeypatch.setattr("app.services.ucs_lineage_sync.get_client", _fake_get_client)
    return requests, shared_client


POLICY = {"id": "dqp_1", "name": "Stock_count"}
PASSING_RESULT = {
    "id": "res_1", "topic_name": "raw__db__postgres__products__batch__v1",
    "status": "passed", "failed_count": 0, "total_count": 50,
}
FAILING_RESULT = {
    "id": "res_2", "topic_name": "raw__db__postgres__products__batch__v1",
    "status": "failed", "failed_count": 13, "total_count": 50,
}
TOTAL_FAILURE_RESULT = {
    "id": "res_3", "topic_name": "raw__db__postgres__products__batch__v1",
    "status": "failed", "failed_count": 50, "total_count": 50,
}


@pytest.mark.asyncio
async def test_emit_quality_result_sends_a_passing_assertion(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, PASSING_RESULT)

        assert len(requests) == 1
        body = _body(requests[0])
        assert body["run"] == {"runId": "quality-res_1"}
        output = body["outputs"][0]
        assert output["namespace"] == "dataconnect:kafka_topic"
        assert output["name"] == "raw__db__postgres__products__batch__v1"
        assertion = output["facets"]["dataQualityAssertions"]["assertions"][0]
        assert assertion == {
            "assertion": "Stock_count", "success": True, "status": "pass", "passRate": 100.0,
            "dimension": "validity", "detail": "0/50 failed",
        }
        assert requests[0].headers["X-Tenant-Id"] == "org_1001"
        _hdr = requests[0].headers
        assert "x-user-roles" not in {k.lower() for k in _hdr.keys()}
        assert "x-user-role" not in {k.lower() for k in _hdr.keys()}
        # The event goes to /lineage/events, which needs ucs:lineage-events:create (UCS docs/auth/UCS-PERMISSIONS.md);
        # ucs:quality-results:create is for UCS's own /quality/results endpoints and got every result a 403 here.
        assert json.loads(base64.b64decode(_hdr["X-User-Permissions"])) == ['ucs:lineage-events:create']
        assert _hdr["X-User-Name"] == settings.ucs_lineage_sync_user
        assert "X-Inaiera-Gateway-Secret" in _hdr
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_sends_a_partial_failure_as_warn_with_its_real_pass_rate(
    monkeypatch,
) -> None:
    """A batch where SOME records failed (37 of 50 passed) is a "warn", not a
    "fail" — UCS's score for a failing dimension is hard-capped regardless of
    the real percentage (quality_service.py's DQ_FAIL_SCORE_CAP), so reporting
    "fail" here would silently throw the real 74% pass rate away. Only a batch
    where EVERY record failed (see the next test) is a genuine "fail"."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, FAILING_RESULT)

        assertion = _body(requests[0])["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"][0]
        assert assertion["success"] is False
        assert assertion["status"] == "warn"
        assert assertion["passRate"] == 74.0
        assert assertion["detail"] == "13/50 failed"
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_sends_a_total_failure_as_fail(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, TOTAL_FAILURE_RESULT)

        assertion = _body(requests[0])["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"][0]
        assert assertion["success"] is False
        assert assertion["status"] == "fail"
        assert assertion["passRate"] == 0.0
        assert assertion["detail"] == "50/50 failed"
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_falls_back_to_policy_id_when_unnamed(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    policy = {"id": "dqp_9"}

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", policy, PASSING_RESULT)

        assertion = _body(requests[0])["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"][0]
        assert assertion["assertion"] == "policy-dqp_9"
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_is_a_noop_when_sync_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ucs_quality_sync_enabled", False)
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, PASSING_RESULT)

        assert requests == []
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_swallows_a_ucs_outage(monkeypatch) -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    requests, shared_client = _mock_get_client(monkeypatch, _raise)

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, PASSING_RESULT)  # must not raise
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_quality_result_skips_when_result_has_no_topic_name(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    result = {**PASSING_RESULT, "topic_name": None}

    try:
        await ucs_quality_sync.emit_quality_result("org_1001", POLICY, result)

        assert requests == []
    finally:
        await shared_client.aclose()


STREAM_PIPELINE = {"id": "pipe_9", "org_id": "org_1001"}


@pytest.mark.asyncio
async def test_emit_stream_quality_reports_the_running_totals_as_one_assertion(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    try:
        await ucs_quality_sync.emit_stream_quality(
            "org_1001", STREAM_PIPELINE, POLICY, topic_name="raw-shop-pg-products-batch-v1", total=64, failed=15)

        assert len(requests) == 1
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "quality:Stock_count"}   # the same check as a batch result
        assert body["run"] == {"runId": "quality-stream-pipe_9-64-15"}
        output = body["outputs"][0]
        assert output["name"] == "raw-shop-pg-products-batch-v1"
        assert output["facets"]["dataQualityAssertions"]["assertions"][0] == {
            "assertion": "Stock_count", "success": False, "status": "warn", "passRate": 76.5625,
            "dimension": "validity", "detail": "15/64 failed",
        }
        assert json.loads(base64.b64decode(requests[0].headers["X-User-Permissions"])) == ["ucs:lineage-events:create"]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_stream_quality_sends_nothing_before_a_record_was_checked_or_when_sync_is_off(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    try:
        await ucs_quality_sync.emit_stream_quality("org_1001", STREAM_PIPELINE, POLICY, topic_name="t", total=0, failed=0)
        monkeypatch.setattr(settings, "ucs_quality_sync_enabled", False)
        await ucs_quality_sync.emit_stream_quality("org_1001", STREAM_PIPELINE, POLICY, topic_name="t", total=5, failed=1)
        assert requests == []
    finally:
        await shared_client.aclose()


def _worker_harness(monkeypatch, *, pipeline_extra=None, policy=None):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services import quality_stream_worker as qsw

    totals = {"value": SimpleNamespace(total_count=10, failed_count=2)}

    class _Repo:
        def __init__(self, session):
            pass

        async def get_by_pipeline_id(self, org_id, pipeline_id):
            return totals["value"]

    @asynccontextmanager
    async def _session():
        yield None

    sent: list[dict] = []

    async def _emit(org_id, pipeline, policy, **report):
        sent.append(report)

    clock = {"now": 1000.0}
    monkeypatch.setattr(qsw, "QualityStreamStatsRepository", _Repo)
    monkeypatch.setattr(qsw, "AsyncSessionLocal", _session)
    monkeypatch.setattr(qsw, "emit_stream_quality", _emit)
    monkeypatch.setattr(qsw.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(settings, "ucs_quality_stream_sync_interval_seconds", 60.0)
    pipeline = {"id": "pipe_9", "org_id": "org_1001", "domain": "shop", "source_system": "pg", "entity": "products", "mode": "batch",
                "version": "v1", "zone": "raw", "topic_name": "raw-shop-pg-products-batch-v1", **(pipeline_extra or {})}
    worker = qsw.QualityStreamWorker(org_id="org_1001", pipeline=pipeline, policy=policy or {"id": "dqp_1", "name": "checks"})
    return worker, totals, sent, clock, SimpleNamespace


@pytest.mark.asyncio
async def test_the_stream_worker_reports_to_ucs_when_its_totals_change_and_no_more_often_than_the_interval(monkeypatch) -> None:
    worker, totals, sent, clock, ns = _worker_harness(monkeypatch)

    await worker._report_to_ucs()
    # OBSERVE, failures copied to the Kafka quarantine: the checked topic, and the quarantine topic at 0% — as "warn", never "fail"
    # (a quarantine topic full of failures is it working; UCS's default quality-breach alert fires on "fail").
    assert sent == [
        {"topic_name": "raw-shop-pg-products-batch-v1", "total": 10, "failed": 2},
        {"topic_name": "quarantine-shop-pg-products-validation-v1", "total": 2, "failed": 2, "status": "warn", "run_suffix": "-q"},
    ]

    totals["value"] = ns(total_count=20, failed_count=3)
    clock["now"] += 30                       # changed, but within the interval: wait
    await worker._report_to_ucs()
    assert len(sent) == 2

    clock["now"] += 31                       # interval passed: the new totals go out
    await worker._report_to_ucs()
    assert sent[-2]["total"] == 20
    assert sent[-1]["total"] == 3

    clock["now"] += 61                       # interval passed, but nothing changed: nothing is sent
    await worker._report_to_ucs()
    assert len(sent) == 4

    monkeypatch.setattr(settings, "ucs_quality_sync_enabled", False)
    totals["value"] = ns(total_count=30, failed_count=3)
    clock["now"] += 61
    await worker._report_to_ucs()
    assert len(sent) == 4                    # sync off: nothing


@pytest.mark.asyncio
async def test_an_enforce_worker_also_reports_its_prepared_topic_as_all_passed(monkeypatch) -> None:
    worker, totals, sent, clock, ns = _worker_harness(monkeypatch, pipeline_extra={"quality_mode": "ENFORCE"})

    await worker._report_to_ucs()

    assert sent[2] == {"topic_name": "prepared-shop-pg-products-batch-v1", "total": 8, "failed": 0, "run_suffix": "-p"}


@pytest.mark.asyncio
async def test_a_policy_that_quarantines_nothing_reports_no_quarantine_topic(monkeypatch) -> None:
    worker, totals, sent, clock, ns = _worker_harness(monkeypatch, policy={"id": "dqp_1", "name": "checks", "on_failure": "log"})

    await worker._report_to_ucs()

    assert [r["topic_name"] for r in sent] == ["raw-shop-pg-products-batch-v1"]


@pytest.mark.asyncio
async def test_a_quarantine_report_is_zero_percent_but_warn(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    try:
        await ucs_quality_sync.emit_stream_quality(
            "org_1001", STREAM_PIPELINE, POLICY, topic_name="quarantine-shop-pg-products-validation-v1", total=15, failed=15,
            status="warn", run_suffix="-q")
        body = _body(requests[0])
        assert body["run"] == {"runId": "quality-stream-pipe_9-15-15-q"}
        assert body["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"][0] == {
            "assertion": "Stock_count", "success": False, "status": "warn", "passRate": 0.0, "dimension": "validity", "detail": "15/15 failed",
        }
    finally:
        await shared_client.aclose()



@pytest.mark.asyncio
async def test_a_sinks_check_of_its_input_topic_has_its_own_name(monkeypatch) -> None:
    """A sink checks a topic another pipeline produces; an ENFORCE source reports its prepared topic under the SAME policy name. UCS keeps one
    check per (topic, name), so the two overwrote each other ("0/1 failed" on a topic holding 49 passed records). The sink's gets its own."""
    worker, totals, sent, clock, ns = _worker_harness(monkeypatch, pipeline_extra={
        "name": "products-sink-enforce", "direction": "SINK", "entity": "products_sink_enforce", "mode": "stream", "quality_mode": "ENFORCE",
        "topic_name": None, "connector_config": {"topics": "prepared-shop-pg-products-batch-v1"}})

    await worker._report_to_ucs()

    assert sent[0] == {"topic_name": "prepared-shop-pg-products-batch-v1", "total": 10, "failed": 2,
                       "check_name": "checks · products-sink-enforce"}
    assert all("check_name" not in r for r in sent[1:])   # its own quarantine and prepared topics keep the policy's name


@pytest.mark.asyncio
async def test_an_enforce_sinks_hand_off_topic_is_not_scored_and_is_trimmed_to_what_its_connector_wrote(monkeypatch) -> None:
    """The hand-off is internal (not in the catalog), so it gets no quality score; and its passing records are deleted once the sink's
    own connector has committed them — at most every sink_handoff_trim_interval_seconds."""
    from app.services import quality_stream_worker as qsw

    worker, totals, sent, clock, ns = _worker_harness(monkeypatch, pipeline_extra={
        "name": "products-sink-enforce", "direction": "SINK", "entity": "products_sink_enforce", "mode": "stream", "quality_mode": "ENFORCE",
        "topic_name": None, "connector_name": "shop--pg--products_sink_enforce--stream--v1",
        "connector_config": {"topics": "prepared-shop-pg-products-batch-v1"}})
    trims: list[tuple] = []

    async def _trim(topic, group_id, *, already=None):
        trims.append((topic, group_id, dict(already or {})))
        return {0: 40}
    monkeypatch.setattr(qsw, "trim_consumed_records", _trim)
    monkeypatch.setattr(settings, "sink_handoff_trim_interval_seconds", 30.0)

    await worker._report_to_ucs()
    assert not any(r["topic_name"].startswith("_dataconnect.gate.") for r in sent)

    await worker._trim_handoff()
    clock["now"] += 10
    await worker._trim_handoff()                          # within the interval: not again
    clock["now"] += 25
    await worker._trim_handoff()
    assert trims == [
        ("_dataconnect.gate.shop-pg-products_sink_enforce-stream-v1", "connect-shop--pg--products_sink_enforce--stream--v1", {}),
        ("_dataconnect.gate.shop-pg-products_sink_enforce-stream-v1", "connect-shop--pg--products_sink_enforce--stream--v1", {0: 40}),
    ]


@pytest.mark.asyncio
async def test_a_source_worker_never_trims_anything(monkeypatch) -> None:
    from app.services import quality_stream_worker as qsw

    worker, totals, sent, clock, ns = _worker_harness(monkeypatch, pipeline_extra={"quality_mode": "ENFORCE", "direction": "SOURCE"})
    called: list = []

    async def _trim(*args, **kwargs):
        called.append(args)
        return {}
    monkeypatch.setattr(qsw, "trim_consumed_records", _trim)
    await worker._trim_handoff()
    assert called == []                                    # a source's prepared topic is data other pipelines read
