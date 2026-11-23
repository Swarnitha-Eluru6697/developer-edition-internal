"""Retention and reconciliation must be a scheduled control loop.

The reported gap: cleanup and reconciliation existed only as functions
someone had to remember to invoke. Nothing in the shipped deployment ran
them, so expired uploads, stale HITL claims, completed outbox rows and
idempotency keys accumulated indefinitely — measured on this project's
own development database before this was written, 124 completed
KBIngestionOutbox rows going back nine days that no code path would ever
have removed.

What makes this a control loop rather than a cron line, and what these
tests pin:

  * every run is RECORDED, so "did retention run?" has an answer;
  * a MISSED run raises an alert, because a loop that dies quietly is
    indistinguishable from one with nothing to do;
  * it CATCHES UP from the last success rather than skipping the window
    a restart was down for;
  * a failing job does not stop the others.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, delete, select

from app import maintenance_scheduler as sched
from app.db import engine
from app.maintenance_jobs import Job, job_by_name, jobs
from app.models import ActivityIdempotency, KBIngestionOutbox, MaintenanceRun


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def clean_records():
    """Each test reasons about freshness, so it must start with no
    history of its own."""
    def _clear():
        with Session(engine) as s:
            s.exec(delete(MaintenanceRun))
            s.commit()
    _clear()
    yield
    _clear()


def _job(name="test-job", interval=300, fn=None):
    return Job(name, fn or (lambda session: {"did": "nothing"}), interval, "a test job")


class TestEveryRunIsRecorded:
    def test_a_successful_run_is_recorded_with_its_outcome(self, session):
        record = sched.run_job(session, _job(fn=lambda s: {"pruned": 7}))
        assert record.status == "ok"
        assert record.detail == {"pruned": 7}
        assert record.finished_at is not None
        assert record.duration_ms is not None

    def test_a_failing_job_is_recorded_as_an_error_not_lost(self, session):
        def boom(_s):
            raise RuntimeError("the database went away")

        record = sched.run_job(session, _job(fn=boom))
        assert record.status == "error"
        assert "went away" in record.error

    def test_a_failing_job_does_not_stop_the_others(self, session, monkeypatch):
        """One broken job must not silently disable retention for
        everything else."""
        def boom(_s):
            raise RuntimeError("nope")

        monkeypatch.setattr(sched, "jobs", lambda: [
            _job("breaks", 300, boom), _job("works", 300, lambda s: {"ok": True})])
        outcome = sched.tick(session)
        assert outcome == {"breaks": "error", "works": "ok"}

    def test_the_attempt_is_recorded_before_the_work_starts(self, session):
        """So a job that hangs, or a process killed mid-run, leaves
        evidence at "running" rather than no evidence at all."""
        seen = {}

        def slow(s):
            # The STATUS VALUE, not the ORM object: holding the object
            # would show "ok" by assertion time, since run_job mutates
            # that same instance when the job finishes.
            rows = s.exec(select(MaintenanceRun).where(
                MaintenanceRun.job == "slow-job")).all()
            seen["statuses"] = [r.status for r in rows]
            return {}

        sched.run_job(session, _job("slow-job", 300, slow))
        assert seen["statuses"] == ["running"]


class TestDueness:
    def test_a_job_that_has_never_run_is_due_immediately(self, session):
        assert sched.is_due(session, _job("never-run")) is True

    def test_a_job_that_just_succeeded_is_not_due(self, session):
        job = _job("just-ran", interval=3600)
        sched.run_job(session, job)
        assert sched.is_due(session, job) is False

    def test_it_catches_up_from_the_last_success_rather_than_skipping(self, session):
        """A restart must not silently drop the window it was down for."""
        job = _job("stale", interval=300)
        old = datetime.utcnow() - timedelta(seconds=900)
        session.add(MaintenanceRun(job="stale", started_at=old, finished_at=old, status="ok"))
        session.commit()
        assert sched.is_due(session, job) is True

    def test_a_failed_run_does_not_count_as_a_success(self, session):
        """Otherwise a job failing every interval would look fresh and
        never alert."""
        job = _job("failing", interval=300)
        now = datetime.utcnow()
        session.add(MaintenanceRun(job="failing", started_at=now, finished_at=now,
                                    status="error", error="boom"))
        session.commit()
        assert sched.is_due(session, job) is True


class TestMissedRunsAlert:
    """A loop that dies quietly is indistinguishable from one with
    nothing to do. Reported through metrics and logs rather than the
    Alert table, because Alert.agent_id is NOT NULL with a foreign key
    to agent.id — that table is per-agent by construction, and writing a
    platform-level row into it raised a NotNullViolation. That would have
    crashed the alerting path the first time a job was actually overdue,
    which is precisely when it is needed."""

    @staticmethod
    def _gauge(name, job):
        from app.metrics import REGISTRY
        return REGISTRY.get_sample_value(name, {"job": job})

    def test_an_overdue_job_is_reported_as_overdue(self, session, monkeypatch):
        job = _job("overdue-job", interval=300)
        old = datetime.utcnow() - timedelta(seconds=300 * sched.OVERDUE_FACTOR + 600)
        session.add(MaintenanceRun(job="overdue-job", started_at=old, finished_at=old, status="ok"))
        session.commit()

        assert sched.overdue_by(session, job) is not None
        monkeypatch.setattr(sched, "jobs", lambda: [job])
        monkeypatch.setattr(sched, "is_due", lambda s, j, now=None: False)
        assert sched.tick(session) == {"overdue-job": "overdue"}

        assert self._gauge("inaiera_maintenance_job_overdue", "overdue-job") == 1

    def test_the_overdue_gauge_clears_once_the_job_succeeds(self, session):
        job = _job("recovers", interval=60)
        old = datetime.utcnow() - timedelta(seconds=60 * sched.OVERDUE_FACTOR + 600)
        session.add(MaintenanceRun(job="recovers", started_at=old, finished_at=old, status="ok"))
        session.commit()
        sched._report_missed(session, job, timedelta(seconds=600))
        assert self._gauge("inaiera_maintenance_job_overdue", "recovers") == 1

        sched.run_job(session, job)
        assert self._gauge("inaiera_maintenance_job_overdue", "recovers") == 0

    def test_the_age_of_the_last_success_is_published(self, session):
        """So an operator can alert on their own threshold rather than
        this platform's tolerance."""
        job = _job("aged", interval=300)
        sched.run_job(session, job)
        age = self._gauge("inaiera_maintenance_last_success_age_seconds", "aged")
        assert age is not None and age < 60

    def test_every_run_is_counted_by_outcome(self, session):
        def boom(_s):
            raise RuntimeError("nope")

        sched.run_job(session, _job("counted", 300, boom))
        assert self._gauge_total("counted", "error") >= 1

    @staticmethod
    def _gauge_total(job, status):
        from app.metrics import REGISTRY
        return REGISTRY.get_sample_value(
            "inaiera_maintenance_job_runs_total", {"job": job, "status": status})

    def test_a_job_merely_late_within_tolerance_is_not_reported_missed(self, session):
        """An alert that fires on ordinary jitter is one people learn to
        ignore. Late is not the same as missed."""
        job = _job("slightly-late", interval=300)
        recent = datetime.utcnow() - timedelta(seconds=400)
        session.add(MaintenanceRun(job="slightly-late", started_at=recent,
                                    finished_at=recent, status="ok"))
        session.commit()
        assert sched.overdue_by(session, job) is None

    def test_a_fresh_deployment_does_not_alert_before_anything_has_run(self, session):
        """With no history at all, a daily job must not be reported as
        missed in the first minute."""
        assert sched.overdue_by(session, _job("brand-new", interval=86400)) is None


class TestTheJobsActuallyClean:
    def test_completed_outbox_rows_past_the_window_are_pruned(self, session, monkeypatch):
        monkeypatch.setenv("OUTBOX_RETENTION_DAYS", "7")
        from app.models import KnowledgeBase

        old = datetime.utcnow() - timedelta(days=30)
        fresh = datetime.utcnow()
        kb_id = "mt-outbox-kb"
        # kb_id is NOT NULL with a foreign key, so the test needs a real
        # KB rather than a placeholder.
        if not session.get(KnowledgeBase, kb_id):
            session.add(KnowledgeBase(id=kb_id, name="Maintenance test KB",
                                      provider="pgvector", config={}, status="connected"))
            session.commit()
        rows = [
            KBIngestionOutbox(id="mt-old-done", kb_id=kb_id, document_id="d1", operation="ingest",
                              status="completed", created_at=old, updated_at=old),
            KBIngestionOutbox(id="mt-old-failed", kb_id=kb_id, document_id="d2", operation="ingest",
                              status="failed", created_at=old, updated_at=old),
            KBIngestionOutbox(id="mt-new-done", kb_id=kb_id, document_id="d3", operation="ingest",
                              status="completed", created_at=fresh, updated_at=fresh),
        ]
        for r in rows:
            session.add(r)
        session.commit()
        try:
            result = job_by_name("prune-completed-outbox").run(session)
            assert result["pruned"] >= 1
            assert session.get(KBIngestionOutbox, "mt-old-done") is None
            # The record of a FAILURE is evidence, not clutter.
            assert session.get(KBIngestionOutbox, "mt-old-failed") is not None
            # Inside the window, still needed.
            assert session.get(KBIngestionOutbox, "mt-new-done") is not None
        finally:
            for r in ("mt-old-done", "mt-old-failed", "mt-new-done"):
                row = session.get(KBIngestionOutbox, r)
                if row:
                    session.delete(row)
            session.commit()
            from app.models import KnowledgeBase as _KB
            kb = session.get(_KB, kb_id)
            if kb:
                session.delete(kb)
                session.commit()

    def test_execution_history_pruning_is_off_unless_a_window_is_set(self, session, monkeypatch):
        """The one job that deletes a record of something a person did.
        An upgrade must not silently destroy execution history."""
        monkeypatch.delenv("RUN_RETENTION_DAYS", raising=False)
        result = job_by_name("prune-execution-history").run(session)
        assert "skipped" in result

        monkeypatch.setenv("RUN_RETENTION_DAYS", "0")
        assert "skipped" in job_by_name("prune-execution-history").run(session)

    def test_reconcile_outbox_skips_providers_it_cannot_reconcile(self, session):
        """Not applicable is not a failure — recording it as one every
        15 minutes buries a real error in noise."""
        result = job_by_name("reconcile-outbox").run(session)
        assert "skipped_unsupported_provider" in result
        for detail in result["repaired"].values():
            assert "only wired for provider" not in str(detail)


class TestTheLoopIsDeployable:
    def test_every_job_has_a_sane_interval(self):
        for job in jobs():
            assert job.interval_seconds >= 60, f"{job.name} would busy-loop"
            assert job.description

    def test_a_single_pass_can_be_run_for_a_cronjob(self):
        """--once makes the same loop usable as a Kubernetes CronJob,
        for a deployment that would rather schedule externally."""
        assert sched.main(["--once"]) == 0

    def test_the_loop_records_are_themselves_pruned(self, session):
        """The record of runs is one more thing that accumulates."""
        old = datetime.utcnow() - timedelta(days=sched.RUN_RECORD_RETENTION_DAYS + 5)
        session.add(MaintenanceRun(job="ancient", started_at=old, finished_at=old, status="ok"))
        session.commit()
        assert sched.prune_own_records(session) >= 1
        assert session.exec(select(MaintenanceRun).where(MaintenanceRun.job == "ancient")).all() == []

    def test_compose_ships_the_scheduler_as_a_service(self):
        """Deployable infrastructure, not a runbook step."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docker-compose.yml")
        if not os.path.exists(path):
            pytest.skip("compose file not available in this environment")
        with open(path) as f:
            text = f.read()
        assert "maintenance:" in text
        assert "app.maintenance_scheduler" in text
