"""
Real tests for section 0.113, GAP_REPORT.

The real, reported gap: several core collection endpoints (agents,
runs, traces, agent logs, alerts, KB documents, eval runs) used
`session.exec(select(...)).all()` with no real bound — genuinely fine
at this platform's own development-scale row counts, but a real,
growing risk at 10K/100K/1M+ real rows: every one of these loaded the
ENTIRE matching row set into this API process's own memory on every
single real request. Monitoring also computed every real aggregate
(success rate, percentiles, average cost) in Python after loading
every matching Run row, rather than in Postgres.

The fix: a real, shared, keyset-cursor pagination module
(pagination.py) — real `(sort_col, id_col)` ordering, opaque base64
cursors, `X-Next-Cursor`/`X-Has-More` response headers, backward
compatible (still a bare JSON array) — applied to every genuinely
growing collection endpoint. Monitoring's own aggregates are now
single, real Postgres queries (COUNT/AVG/PERCENTILE_CONT/FILTER),
computed entirely server-side.

These tests exercise the real, running API and the real database
directly, using uniquely-identifiable rows and real pagination headers
— not mocked.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import Run, Trace, Alert, EvalRun, EvalSet, KnowledgeDocument, KnowledgeBase
from app.pagination import encode_cursor, decode_cursor, paginate, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

client = TestClient(app)


def create_agent(flow: dict, app_type: str = "workflow") -> dict:
    r = client.post("/api/agents", json={
        "name": f"Pagination Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": app_type,
        "template_flow": flow,
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for t in ("hitlrequest", "run", "trace", "agentlog", "agentversion"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


def plain_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "done"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
    }


class TestRealCursorEncodingDecoding:
    def test_a_datetime_sort_value_round_trips_exactly(self):
        now = datetime.utcnow()
        cursor = encode_cursor(now, "some-id")
        sort_value, row_id = decode_cursor(cursor)
        assert sort_value == now
        assert row_id == "some-id"

    def test_an_integer_sort_value_round_trips_exactly(self):
        cursor = encode_cursor(42, 42)
        sort_value, row_id = decode_cursor(cursor)
        assert sort_value == 42
        assert row_id == 42

    def test_a_malformed_cursor_raises_rather_than_silently_misbehaving(self):
        with pytest.raises(Exception):
            decode_cursor("not-a-real-base64-json-cursor")

    def test_the_cursor_is_a_real_opaque_token_not_a_raw_value(self):
        """A real caller should never be able to trivially read or
        guess this cursor's own internal structure by eyeballing it."""
        cursor = encode_cursor(datetime.utcnow(), "secret-agent-id")
        assert "secret-agent-id" not in cursor


class TestRealPaginateHelper:
    """Direct tests of the shared paginate() function itself, against
    a real, controlled set of Run rows."""

    def _make_runs(self, prefix: str, count: int):
        base = datetime.utcnow() - timedelta(days=1)
        with Session(engine) as s:
            agent_id = s.exec(text("SELECT id FROM agent LIMIT 1")).first()[0]
            for i in range(count):
                s.add(Run(id=f"{prefix}{i}", agent_id=agent_id, agent_name="x", status="success",
                           duration="1.0s", cost="$0.00", ts="00:00:00",
                           created_at=base + timedelta(seconds=i)))
            s.commit()
        return agent_id

    def _cleanup_runs(self, prefix: str):
        with Session(engine) as s:
            s.exec(text("DELETE FROM run WHERE id LIKE :p"), params={"p": f"{prefix}%"})
            s.commit()

    def test_a_full_real_page_walk_visits_every_row_exactly_once(self):
        prefix = f"pgtest-{uuid.uuid4().hex[:8]}-"
        self._make_runs(prefix, 25)
        try:
            with Session(engine) as s:
                from sqlmodel import select
                seen_ids = set()
                cursor = None
                for _ in range(100):
                    q = select(Run).where(Run.id.like(f"{prefix}%"))
                    page = paginate(s, Run, q, limit=10, cursor=cursor, sort_col=Run.created_at, id_col=Run.id)
                    if not page:
                        break
                    for row in page:
                        assert row.id not in seen_ids, f"row {row.id} was returned on more than one real page"
                        seen_ids.add(row.id)
                    if len(page) < 10:
                        break
                    last = page[-1]
                    cursor = encode_cursor(last.created_at, last.id)
                assert len(seen_ids) == 25
        finally:
            self._cleanup_runs(prefix)

    def test_has_more_is_honest_at_the_real_boundary(self):
        prefix = f"pgtest2-{uuid.uuid4().hex[:8]}-"
        self._make_runs(prefix, 5)
        try:
            with Session(engine) as s:
                from sqlmodel import select
                q = select(Run).where(Run.id.like(f"{prefix}%"))

                class _FakeResponse:
                    headers = {}
                r1 = _FakeResponse()
                page1 = paginate(s, Run, q, limit=5, cursor=None, sort_col=Run.created_at, id_col=Run.id, response=r1)
                assert len(page1) == 5
                assert r1.headers["X-Has-More"] == "false"

                r2 = _FakeResponse()
                page2 = paginate(s, Run, q, limit=3, cursor=None, sort_col=Run.created_at, id_col=Run.id, response=r2)
                assert len(page2) == 3
                assert r2.headers["X-Has-More"] == "true"
                assert "X-Next-Cursor" in r2.headers
        finally:
            self._cleanup_runs(prefix)

    def test_a_row_with_a_null_sort_value_does_not_get_pushed_to_the_front(self):
        """§0.113, GAP_REPORT — the real, deliberate NULL-handling fix:
        a real, existing row predating created_at's own introduction
        (a genuine NULL) must not sort as "newest" ahead of real,
        non-NULL, genuinely-newer rows.

        A real, separate bug in this TEST's own setup was found and
        fixed while investigating why this failed: Run.created_at has
        a real `default_factory=datetime.utcnow` — SQLAlchemy's own
        column-level default fires whenever the Python-side attribute
        is None AT FLUSH TIME, not only when the field is genuinely
        omitted, so `Run(..., created_at=None)` silently got a real,
        non-NULL timestamp on commit regardless — confirmed directly:
        `r.created_at` was `None` immediately after construction, but
        a real, non-NULL datetime immediately after `session.commit()`.
        This is NOT a bug in `paginate` itself — a genuinely NULL row
        can only be produced by writing NULL at the database level
        directly (a raw UPDATE, bypassing the ORM's own default
        entirely), matching how a real, existing legacy row actually
        got its own NULL value in the first place (a raw `ALTER TABLE
        ADD COLUMN` with no default, never a real ORM insert)."""
        prefix = f"pgtest3-{uuid.uuid4().hex[:8]}-"
        with Session(engine) as s:
            agent_id = s.exec(text("SELECT id FROM agent LIMIT 1")).first()[0]
            s.add(Run(id=f"{prefix}null", agent_id=agent_id, agent_name="x", status="success",
                       duration="1.0s", cost="$0.00", ts="00:00:00"))
            s.add(Run(id=f"{prefix}real", agent_id=agent_id, agent_name="x", status="success",
                       duration="1.0s", cost="$0.00", ts="00:00:00", created_at=datetime.utcnow()))
            s.commit()
            # Force a genuinely NULL value at the database level — the
            # ORM's own default_factory would silently override a
            # Python-side `created_at=None` at flush time (see this
            # test's own docstring above for the full real reasoning).
            s.exec(text("UPDATE run SET created_at = NULL WHERE id = :id"), params={"id": f"{prefix}null"})
            s.commit()
        try:
            with Session(engine) as s:
                from sqlmodel import select
                q = select(Run).where(Run.id.like(f"{prefix}%"))
                page = paginate(s, Run, q, limit=1, cursor=None, sort_col=Run.created_at, id_col=Run.id)
                assert page[0].id == f"{prefix}real", "a NULL-created_at row was incorrectly sorted ahead of a real, dated row"
        finally:
            self._cleanup_runs(prefix)

    def test_a_malformed_cursor_from_the_real_api_returns_an_honest_400(self):
        r = client.get("/api/runs", params={"cursor": "not-a-real-cursor-at-all"})
        assert r.status_code == 400

    def test_limit_is_hard_capped_regardless_of_what_a_caller_requests(self):
        r = client.get("/api/runs", params={"limit": 999999})
        assert r.status_code == 422  # FastAPI's own real query validation rejects it outright


class TestRealPaginatedEndpoints:
    """Live, end-to-end proof that every real, converted endpoint
    genuinely paginates through the real, running API — not just that
    the shared helper works in isolation."""

    def test_list_agents_paginates_with_real_headers(self):
        r = client.get("/api/agents", params={"limit": 3})
        assert r.status_code == 200
        assert len(r.json()) <= 3
        assert "x-has-more" in {k.lower() for k in r.headers.keys()}

    def test_list_agents_cursor_advances_to_a_genuinely_different_page(self):
        r1 = client.get("/api/agents", params={"limit": 3})
        page1_ids = {a["id"] for a in r1.json()}
        if r1.headers.get("x-has-more") != "true":
            pytest.skip("not enough real agents in this shared test DB to exercise a second page")
        r2 = client.get("/api/agents", params={"limit": 3, "cursor": r1.headers["x-next-cursor"]})
        page2_ids = {a["id"] for a in r2.json()}
        assert page1_ids.isdisjoint(page2_ids), "the second real page overlapped with the first"

    def test_list_alerts_paginates_with_real_headers(self):
        r = client.get("/api/alerts", params={"limit": 2})
        assert r.status_code == 200
        assert len(r.json()) <= 2

    def test_get_agent_traces_paginates(self):
        agent = create_agent(plain_flow())
        try:
            run_r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert run_r.status_code == 200
            r = client.get(f"/api/agents/{agent['id']}/traces", params={"limit": 100})
            assert r.status_code == 200
            assert any(t["id"] == run_r.json()["trace_id"] for t in r.json())
        finally:
            cleanup_agent(agent["id"])

    def test_get_agent_logs_paginates(self):
        agent = create_agent(plain_flow())
        try:
            client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            r = client.get(f"/api/agents/{agent['id']}/logs", params={"limit": 100})
            assert r.status_code == 200
            assert isinstance(r.json(), list)
        finally:
            cleanup_agent(agent["id"])

    def test_list_documents_paginates_for_a_real_kb(self):
        with Session(engine) as s:
            kb_id = s.exec(text("SELECT id FROM knowledgebase LIMIT 1")).first()
        if not kb_id:
            pytest.skip("no real KB seeded in this environment")
        r = client.get(f"/api/integrations/knowledge-bases/{kb_id[0]}/documents", params={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()) <= 5

    def test_list_eval_runs_paginates_for_a_real_set(self):
        with Session(engine) as s:
            set_id = s.exec(text("SELECT id FROM evalset LIMIT 1")).first()
        if not set_id:
            pytest.skip("no real eval set seeded in this environment")
        r = client.get(f"/api/evals/sets/{set_id[0]}/runs", params={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()) <= 5

    def test_list_knowledge_bases_paginates_with_real_headers(self):
        """§0.113, GAP_REPORT — the same real, keyset-pagination fix
        for the top-level knowledge-bases list."""
        r = client.get("/api/integrations/knowledge-bases", params={"limit": 2})
        assert r.status_code == 200
        assert len(r.json()) <= 2
        assert "x-has-more" in {k.lower() for k in r.headers.keys()}

    def test_list_outbox_paginates_for_a_real_kb(self):
        """§0.113, GAP_REPORT — the same real, keyset-pagination fix
        for a KB's own ingestion outbox listing."""
        with Session(engine) as s:
            kb_id = s.exec(text("SELECT id FROM knowledgebase LIMIT 1")).first()
        if not kb_id:
            pytest.skip("no real KB seeded in this environment")
        r = client.get(f"/api/integrations/knowledge-bases/{kb_id[0]}/outbox", params={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()) <= 5


class TestRealMonitoringSqlAggregateEquivalence:
    """§0.113, GAP_REPORT — direct proof that the real SQL-aggregate
    rewrite of _compute_platform_metrics/get_home_stats produces
    IDENTICAL results to what the old, Python-side loop-and-sum
    computation would have — not merely that it runs without error."""

    def test_compute_platform_metrics_matches_a_real_python_side_recomputation(self):
        from app.routers.monitoring import _compute_platform_metrics
        with Session(engine) as s:
            sql_result = _compute_platform_metrics(s)

            from sqlmodel import select
            cutoff = datetime.utcnow() - timedelta(days=7)
            runs = s.exec(select(Run).where(Run.created_at.is_not(None), Run.created_at >= cutoff)).all()
            total = len(runs)
            if total == 0:
                pytest.skip("no real runs in the current 7-day window to compare against")
            success_count = sum(1 for r in runs if r.status == "success")
            harness_count = sum(1 for r in runs if r.fail_class == "harness")
            moderation_count = sum(1 for r in runs if r.fail_class == "moderation")
            denied_count = sum(1 for r in runs if r.status == "denied")
            avg_cost_usd = sum(r.cost_usd for r in runs) / total

            assert sql_result["sample_size"] == total
            assert sql_result["success"] == round(100 * success_count / total, 1)
            assert sql_result["stepLimit"] == round(100 * harness_count / total, 1)
            assert sql_result["guardrail"] == round(100 * moderation_count / total, 1)
            assert sql_result["entPass"] == round(100 * (total - denied_count) / total, 1)
            assert sql_result["costAvg"] == f"${avg_cost_usd:.4f}"

    def test_get_home_stats_matches_a_real_python_side_recomputation(self):
        from sqlmodel import select
        from app.models import Agent
        r = client.get("/api/monitoring/stats")
        assert r.status_code == 200
        body = r.json()

        with Session(engine) as s:
            agents = s.exec(select(Agent)).all()
            # Same definition as the Alerts page's Open tab and the sidebar badge.
            open_alerts = s.exec(select(Alert).where(
                Alert.state == "open", Alert.acked == False,  # noqa: E712
                Alert.delivered_at.is_not(None))).all()
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            todays_runs = s.exec(select(Run).where(Run.created_at.is_not(None), Run.created_at >= today_start)).all()

        assert body["agents"] == len(agents)
        assert body["openAlerts"] == len(open_alerts)
        assert body["runsToday"] == len(todays_runs)
        if todays_runs:
            expected_rate = round(100 * sum(1 for r in todays_runs if r.status == "success") / len(todays_runs), 1)
            assert body["successRate"] == expected_rate
