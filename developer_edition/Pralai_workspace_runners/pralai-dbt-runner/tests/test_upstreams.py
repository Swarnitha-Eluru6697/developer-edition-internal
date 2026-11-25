"""Upstream jobs: the other end of a Dependents link.

"B is a Dependent of A" and "A is upstream of B" are the SAME stored link. A job can now be registered (or edited) with the jobs ABOVE it
(`upstream_project_ids`: it fails, and is not started, when any of them fails) as well as the jobs BELOW it (`dependent_project_ids`), so
a job d can say: a, b, c are upstream, e, f, g are downstream. The failure rules are the ones in test_dependents.py; these tests cover what
is new: writing the links from the other end, returning them, and refusing a loop however the two lists are combined.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import ProjectDependent
from tests.test_dependents import _get, _project, _register, _run, api  # noqa: F401  (api: fixture)
from tests.test_dependents import _reset  # noqa: F401  (autouse fixture)


async def _ids(api, headers, *names):
    return [(await _register(api, headers, n)).json()["id"] for n in names]


async def _get_project(api, headers, pid):
    return (await api.get(f"/api/v1/projects/{pid}", headers=headers)).json()


# ── writing and returning the links ──────────────────────────────────────────────────────────────────────────────────────
async def test_upstream_jobs_are_set_at_registration_and_returned_by_name(api, auth_headers):
    a, b, c = await _ids(api, auth_headers, "alpha", "beta", "gamma")
    r = await _register(api, auth_headers, "delta", upstream_project_ids=[c, a, b])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["upstream_project_ids"] == [a, b, c]                      # by name: alpha, beta, gamma
    assert body["dependent_project_ids"] == []
    for up in (a, b, c):                                                   # the same links, seen from the other end
        assert (await _get_project(api, auth_headers, up))["dependent_project_ids"] == [body["id"]]


async def test_a_job_can_have_upstream_and_downstream_jobs_at_once(api, auth_headers):
    a, b, c, e, f, g = await _ids(api, auth_headers, "a", "b", "c", "e", "f", "g")
    d = (await _register(api, auth_headers, "d", upstream_project_ids=[a, b, c], dependent_project_ids=[e, f, g])).json()
    assert d["upstream_project_ids"] == [a, b, c] and d["dependent_project_ids"] == [e, f, g]
    rows = {p["name"]: p for p in (await api.get("/api/v1/projects", headers=auth_headers)).json()}
    assert rows["a"]["dependent_project_ids"] == [d["id"]] and rows["a"]["upstream_project_ids"] == []
    assert rows["e"]["upstream_project_ids"] == [d["id"]] and rows["e"]["dependent_project_ids"] == []


async def test_upstream_can_be_changed_cleared_or_left_alone_and_never_touches_the_downstream_list(api, auth_headers):
    a, b, c, x = await _ids(api, auth_headers, "alpha", "beta", "gamma", "xray")
    d = (await _register(api, auth_headers, "delta", dependent_project_ids=[x])).json()["id"]
    url = f"/api/v1/projects/{d}"
    assert (await api.patch(url, headers=auth_headers, json={"upstream_project_ids": [c, a]})).json()["upstream_project_ids"] == [a, c]
    assert (await api.patch(url, headers=auth_headers, json={"schedule_cron": "0 * * * *"})).json()["upstream_project_ids"] == [a, c]
    assert (await api.patch(url, headers=auth_headers, json={"upstream_project_ids": [b]})).json()["upstream_project_ids"] == [b]
    cleared = (await api.patch(url, headers=auth_headers, json={"upstream_project_ids": []})).json()
    assert cleared["upstream_project_ids"] == [] and cleared["dependent_project_ids"] == [x]          # downstream untouched
    # the old upstream jobs lost their link to d, and only that one
    assert (await _get_project(api, auth_headers, a))["dependent_project_ids"] == []
    assert (await _get_project(api, auth_headers, b))["dependent_project_ids"] == []


async def test_editing_the_downstream_list_never_touches_the_upstream_list(api, auth_headers):
    a, x = await _ids(api, auth_headers, "alpha", "xray")
    d = (await _register(api, auth_headers, "delta", upstream_project_ids=[a])).json()["id"]
    got = (await api.patch(f"/api/v1/projects/{d}", headers=auth_headers, json={"dependent_project_ids": [x]})).json()
    assert got["upstream_project_ids"] == [a] and got["dependent_project_ids"] == [x]


async def test_replacing_one_jobs_upstream_leaves_every_other_jobs_links_alone(api, auth_headers):
    a, b, other = await _ids(api, auth_headers, "alpha", "beta", "other")
    d = (await _register(api, auth_headers, "delta", upstream_project_ids=[a])).json()["id"]
    kept = (await _register(api, auth_headers, "kept", upstream_project_ids=[a, other])).json()["id"]
    await api.patch(f"/api/v1/projects/{d}", headers=auth_headers, json={"upstream_project_ids": [b]})
    assert (await _get_project(api, auth_headers, kept))["upstream_project_ids"] == [a, other]
    assert (await _get_project(api, auth_headers, a))["dependent_project_ids"] == [kept]


# ── refusing bad links ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["self", "unknown", "other_org"])
async def test_an_upstream_job_must_be_another_existing_project_of_this_org(api, auth_headers, run_db, bad):
    a = (await _register(api, auth_headers, "customers")).json()["id"]
    target = {"self": a, "unknown": str(uuid.uuid4()), "other_org": str(await _project(run_db, "theirs", org_id="another-org"))}[bad]
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [target]})
    assert r.status_code == 400 and r.json()["detail"]["details"]["field"] == "upstream_project_ids", r.text
    assert (await _get_project(api, auth_headers, a))["upstream_project_ids"] == []


async def test_the_same_job_cannot_be_both_upstream_and_downstream(api, auth_headers):
    (x,) = await _ids(api, auth_headers, "xray")
    r = await _register(api, auth_headers, "delta", upstream_project_ids=[x], dependent_project_ids=[x])
    assert r.status_code == 400 and "would make a loop" in r.json()["detail"]["message"], r.text
    assert [p["name"] for p in (await api.get("/api/v1/projects", headers=auth_headers)).json()] == ["xray"]      # nothing half-saved


async def test_an_upstream_that_is_already_below_the_job_would_make_a_loop(api, auth_headers):
    a, b = await _ids(api, auth_headers, "a", "b")
    await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"dependent_project_ids": [b]})           # a -> b
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [b]})        # b -> a closes it
    assert r.status_code == 400 and r.json()["detail"]["message"] == "These links would make a loop: b → a → b."


async def test_a_longer_loop_through_other_jobs_is_refused_and_named(api, auth_headers):
    a, b, c = await _ids(api, auth_headers, "a", "b", "c")
    await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"dependent_project_ids": [b]})           # a -> b
    await api.patch(f"/api/v1/projects/{b}", headers=auth_headers, json={"dependent_project_ids": [c]})           # b -> c
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [c]})        # c -> a
    assert r.status_code == 400
    assert r.json()["detail"]["message"] == "These links would make a loop: c → a → b → c."


async def test_replacing_the_upstream_list_does_not_count_the_links_it_is_replacing(api, auth_headers):
    """Moving a job from "below" to "above" in one step is fine: the old link goes away as the new one comes."""
    a, b = await _ids(api, auth_headers, "a", "b")
    await api.patch(f"/api/v1/projects/{b}", headers=auth_headers, json={"upstream_project_ids": [a]})            # a -> b
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"dependent_project_ids": [], "upstream_project_ids": [b]})
    assert r.status_code == 200, r.text
    assert r.json()["upstream_project_ids"] == [b] and r.json()["dependent_project_ids"] == []


async def test_a_refused_change_leaves_the_existing_links_as_they_were(api, auth_headers):
    a, b, c = await _ids(api, auth_headers, "a", "b", "c")
    d = (await _register(api, auth_headers, "d", upstream_project_ids=[a], dependent_project_ids=[b])).json()["id"]
    r = await api.patch(f"/api/v1/projects/{d}", headers=auth_headers, json={"upstream_project_ids": [b, c]})   # b is below d
    assert r.status_code == 400
    got = await _get_project(api, auth_headers, d)
    assert got["upstream_project_ids"] == [a] and got["dependent_project_ids"] == [b]


async def test_deleting_a_job_removes_its_upstream_links_too(api, auth_headers, run_db):
    a, = await _ids(api, auth_headers, "a")
    d = (await _register(api, auth_headers, "d", upstream_project_ids=[a])).json()["id"]
    assert (await api.delete(f"/api/v1/projects/{d}", headers=auth_headers)).status_code == 204
    assert (await _get_project(api, auth_headers, a))["dependent_project_ids"] == []
    async with run_db() as db:
        assert (await db.execute(select(ProjectDependent))).all() == []


async def test_moving_a_job_from_above_to_below_in_one_step_is_allowed_too(api, auth_headers):
    a, b = await _ids(api, auth_headers, "a", "b")
    await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [b]})            # b -> a
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [], "dependent_project_ids": [b]})
    assert r.status_code == 200, r.text
    assert r.json()["upstream_project_ids"] == [] and r.json()["dependent_project_ids"] == [b]


async def test_both_lists_together_still_refuse_a_loop_through_other_jobs(api, auth_headers):
    a, b, c = await _ids(api, auth_headers, "a", "b", "c")
    await api.patch(f"/api/v1/projects/{b}", headers=auth_headers, json={"dependent_project_ids": [c]})           # b -> c
    d = (await _register(api, auth_headers, "d")).json()["id"]
    r = await api.patch(f"/api/v1/projects/{d}", headers=auth_headers, json={"upstream_project_ids": [c], "dependent_project_ids": [b]})
    assert r.status_code == 400 and "would make a loop" in r.json()["detail"]["message"], r.text        # c -> d -> b -> c
    assert (await _get_project(api, auth_headers, d))["upstream_project_ids"] == []


async def test_both_lists_together_refuse_the_job_itself_and_unknown_jobs(api, auth_headers):
    a, = await _ids(api, auth_headers, "a")
    for body in ({"upstream_project_ids": [a], "dependent_project_ids": [a]},
                 {"upstream_project_ids": [], "dependent_project_ids": [str(uuid.uuid4())]},
                 {"upstream_project_ids": [str(uuid.uuid4())], "dependent_project_ids": []}):
        d = (await _register(api, auth_headers, f"d{uuid.uuid4().hex[:4]}")).json()["id"]
        if body["upstream_project_ids"] == [a] and body["dependent_project_ids"] == [a]:
            body = {"upstream_project_ids": [d], "dependent_project_ids": [d]}              # itself
        r = await api.patch(f"/api/v1/projects/{d}", headers=auth_headers, json=body)
        assert r.status_code == 400, (body, r.text)


async def test_a_job_cannot_be_its_own_upstream_and_the_message_says_so(api, auth_headers):
    a, = await _ids(api, auth_headers, "a")
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"upstream_project_ids": [a]})
    assert r.status_code == 400 and r.json()["detail"]["message"] == "A project cannot be its own upstream."



# ── what Dagster reads (the dependency rule is enforced there, from these links) ───────────────────────────────────────────
import time  # noqa: E402

from jose import jwt  # noqa: E402

from app.core.config import get_settings  # noqa: E402

SECRET = "dagster-scheduler-signing-secret"


@pytest.fixture
def _scheduler_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])


def _scheduler_headers() -> dict:
    now = int(time.time())
    token = jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": "dbt.projects.read", "org_id": get_settings().org_id,
                        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}, SECRET, algorithm="HS256")
    return {"X-Internal-Service-Token": token}


async def test_the_internal_routes_give_dagster_each_jobs_upstream_and_downstream_links(api, auth_headers, _scheduler_identity):
    a, b = await _ids(api, auth_headers, "a", "b")
    d = (await _register(api, auth_headers, "d", upstream_project_ids=[a, b], dependent_project_ids=[])).json()["id"]
    one = (await api.get(f"/api/v1/internal/projects/{d}", headers=_scheduler_headers())).json()
    assert one["upstream_project_ids"] == [a, b] and one["dependent_project_ids"] == []
    top = (await api.get(f"/api/v1/internal/projects/{a}", headers=_scheduler_headers())).json()
    assert top["dependent_project_ids"] == [d] and top["upstream_project_ids"] == []
    rows = {p["id"]: p for p in (await api.get("/api/v1/internal/projects", headers=_scheduler_headers())).json()}
    assert rows[d]["upstream_project_ids"] == [a, b]
    assert rows[a]["dependent_project_ids"] == [d] and rows[a]["upstream_project_ids"] == []
    assert "blocked_by" not in one                                          # the Runner no longer decides who is held back


async def test_the_runner_no_longer_holds_a_job_back_for_a_failing_upstream(api, auth_headers, run_db, monkeypatch, tmp_path):
    """The rule is Dagster's now: a run that reaches the Runner is executed, whatever the jobs above it did."""
    from app.services.run_service import dispatch_run
    from tests.test_dependents import _fail_running_run
    from tests.test_run_reconciler import _patch_dispatch

    a, = await _ids(api, auth_headers, "a")
    b = (await _register(api, auth_headers, "b", upstream_project_ids=[a])).json()["id"]
    await _fail_running_run(run_db, uuid.UUID(a))
    builds = _patch_dispatch(monkeypatch, tmp_path)
    queued = await _run(run_db, uuid.UUID(b), status="queued", trigger="scheduled")
    await dispatch_run(queued)
    assert (await _get(run_db, queued)).status == "succeeded" and builds == [queued]
