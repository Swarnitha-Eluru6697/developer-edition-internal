"""DBT-005: what the cluster says about a Job being cancelled (RunnerIsolation.terminate_job).

`cancelled` may only be written once the cluster confirms the attempt's Job is gone or already finished. A
Foreground delete returns at once while the Job (and its pods) linger, terminating, until the pods are gone, so
the answer to "is it gone?" has to come from reading the cluster again, not from the delete call succeeding.
"""
from __future__ import annotations

import uuid

import pytest

from app.executors import isolation
from tests.test_isolation import FakeK8s, _k8s_settings, _use, no_real_k8s  # noqa: F401  (autouse fixture: no real kube config)


class LingeringK8s(FakeK8s):
    """Foreground deletion: delete_namespaced_job marks the Job terminating; it disappears only after
    `linger_reads` further reads. Can also fail the delete (API error) a number of times."""

    def __init__(self, *, linger_reads=1, fail_deletes=0, fail_reads=0, **kw):
        super().__init__(**kw)
        self.linger_reads = linger_reads
        self.fail_deletes = fail_deletes
        self.fail_reads = fail_reads
        self.delete_calls = 0
        self._reads_since_delete: dict[str, int] = {}

    def read_namespaced_job(self, name, namespace):
        if self.fail_reads:
            self.fail_reads -= 1
            raise isolation.ApiException(status=500)
        if name in self._reads_since_delete:
            self._reads_since_delete[name] += 1
            if self._reads_since_delete[name] > self.linger_reads:
                self.jobs.pop(name, None)
                self._reads_since_delete.pop(name)
        return super().read_namespaced_job(name, namespace)

    def delete_namespaced_job(self, name, namespace, body=None, **kw):
        self.delete_calls += 1
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise isolation.ApiException(status=500)
        job = self.jobs.get(name)
        if job is None:
            raise isolation.ApiException(status=404)
        if body is not None and body.preconditions and body.preconditions.uid != job.metadata.uid:
            raise isolation.ApiException(status=409)
        self.deleted_jobs.append(name)
        job.metadata.deletion_timestamp = "2026-10-14T00:00:00Z"
        self._reads_since_delete[name] = 0


def _setup(monkeypatch, fake, *, attempt=1, labels_of=None):
    _use(monkeypatch, fake, _k8s_settings())
    run_id = uuid.uuid4()
    job = isolation._build_job(run_id=labels_of or run_id, attempt=attempt, engine="pg_lake", select=None, settings=_k8s_settings())
    job.metadata.name = isolation._job_name(run_id, attempt)
    fake.create_namespaced_job("inaiera", job)
    return run_id, job.metadata.name


async def terminate(run_id, name, attempt=1):
    return await isolation.RunnerIsolation().terminate_job(run_id=run_id, attempt=attempt, job_name=name)


async def test_a_running_job_is_deleted_and_reported_terminating_not_gone(monkeypatch):
    fake = LingeringK8s(linger_reads=3)
    run_id, name = _setup(monkeypatch, fake)
    assert await terminate(run_id, name) == "terminating"            # the delete returned, the Job still exists
    assert fake.deleted_jobs == [name] and name in fake.jobs


async def test_it_is_reported_gone_only_after_the_cluster_no_longer_has_it(monkeypatch):
    fake = LingeringK8s(linger_reads=3)
    run_id, name = _setup(monkeypatch, fake)
    states = [await terminate(run_id, name) for _ in range(4)]
    assert states == ["terminating", "terminating", "terminating", "gone"]
    assert fake.deleted_jobs == [name]                                # deleted once, then only watched


async def test_an_immediately_deleted_job_is_gone(monkeypatch):
    fake = FakeK8s()                                                   # background-style: removed by the delete call
    run_id, name = _setup(monkeypatch, fake)
    assert await terminate(run_id, name) == "gone"


async def test_a_job_that_does_not_exist_is_gone(monkeypatch):
    _use(monkeypatch, FakeK8s(), _k8s_settings())
    assert await terminate(uuid.uuid4(), "dbtrun-x-a1") == "gone"


async def test_a_finished_job_is_terminal_and_is_not_waited_for(monkeypatch):
    fake = LingeringK8s()
    run_id, name = _setup(monkeypatch, fake)
    fake.jobs[name].status.succeeded = 1
    assert await terminate(run_id, name) == "terminal"


async def test_a_failed_job_is_terminal_too(monkeypatch):
    fake = LingeringK8s()
    run_id, name = _setup(monkeypatch, fake)
    fake.jobs[name].status.failed = 1
    assert await terminate(run_id, name) == "terminal"


async def test_a_suspended_job_that_never_started_is_deleted(monkeypatch):
    fake = FakeK8s()
    run_id, name = _setup(monkeypatch, fake)
    assert fake.jobs[name].spec.suspend is True
    assert await terminate(run_id, name) == "gone"


async def test_a_job_that_is_already_terminating_is_not_deleted_again(monkeypatch):
    fake = LingeringK8s(linger_reads=5)
    run_id, name = _setup(monkeypatch, fake)
    await terminate(run_id, name)
    await terminate(run_id, name)
    assert fake.delete_calls == 1


async def test_a_delete_api_error_is_raised_so_the_caller_retries(monkeypatch):
    fake = LingeringK8s(fail_deletes=1)
    run_id, name = _setup(monkeypatch, fake)
    with pytest.raises(isolation.ApiException):
        await terminate(run_id, name)
    assert name in fake.jobs and not fake.deleted_jobs                # nothing was claimed to be deleted
    assert await terminate(run_id, name) == "terminating"             # the retry works


async def test_a_read_api_error_is_raised_not_mistaken_for_gone(monkeypatch):
    fake = LingeringK8s(fail_reads=1)
    run_id, name = _setup(monkeypatch, fake)
    with pytest.raises(isolation.ApiException):
        await terminate(run_id, name)


async def test_a_name_collision_with_someone_elses_job_is_never_deleted(monkeypatch):
    fake = LingeringK8s()
    run_id, name = _setup(monkeypatch, fake, labels_of=uuid.uuid4())    # labelled for another run
    assert await terminate(run_id, name) == "foreign"
    assert fake.deleted_jobs == [] and name in fake.jobs


async def test_the_wrong_attempt_is_not_this_attempts_job(monkeypatch):
    fake = LingeringK8s()
    run_id, name = _setup(monkeypatch, fake, attempt=2)
    assert await terminate(run_id, name, attempt=1) == "foreign"
    assert fake.deleted_jobs == []


async def test_a_replacement_job_created_between_read_and_delete_is_protected_by_the_uid_precondition(monkeypatch):
    import copy
    fake = LingeringK8s()
    run_id, name = _setup(monkeypatch, fake)
    real_read = fake.read_namespaced_job
    swapped = []

    def read_then_replace(n, ns):
        job = copy.deepcopy(real_read(n, ns))                          # what we read: the original Job (its UID)
        if not swapped:
            swapped.append(1)
            replacement = copy.deepcopy(fake.jobs[n])
            replacement.metadata.uid = "someone-elses-uid"             # a same-named Job takes its place
            fake.jobs[n] = replacement
        return job
    fake.read_namespaced_job = read_then_replace
    state = await terminate(run_id, name)
    assert not fake.deleted_jobs                                       # the UID precondition refused the delete (409)
    assert state != "gone"                                             # and the run is never told it is gone
