"""X-004 / W-015: a Flink job imported from Git tells the runner exactly where its SQL came from — in typed fields.

Provenance used to ride in a trailing "[flink-git] url=... sha=..." line of the job's free-text description, which spaces, unusual refs,
description edits or formatting could corrupt. Now the import routes send ONLY a structured `source` (canonical repository URL, the ref
actually checked out, the ref that was requested, path, the exact commit, import time) and leave the description as the user wrote it;
the runner freezes the source into each deployment's manifest. Only these routes set it, because only they cloned: a `source` supplied
by a user on the pass-through routes is dropped. Older jobs that still carry the marker are read (never written) and cleaned on resync.
"""
from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import datetime

import pytest

from app.api import flink_proxy

SHA = "a" * 40
JOB = uuid.uuid4()


class _Resp:
    def __init__(self, body: dict, status: int = 200):
        self.content = json.dumps(body).encode()
        self.status_code = status
        self.headers = {"content-type": "application/json"}

    def json(self):
        return json.loads(self.content)


LEGACY_JOB = {"description": "my note\n[flink-git] url=https://github.com/acme/jobs.git ref=main path=a.sql sha=" + "0" * 40}
STRUCTURED_JOB = {"description": "my note", "source": {"git_url": "https://github.com/acme/jobs", "git_ref": "main", "requested_ref": "release",
                                                      "sql_path": "a.sql", "commit_sha": "0" * 40, "provider": "gitlab"}}
job_state = {"job": LEGACY_JOB}


@pytest.fixture
def runner_calls(monkeypatch):
    calls: list[tuple] = []
    job_state["job"] = LEGACY_JOB

    async def fake_call(user, scope, method, path, json_body=None, **kw):
        calls.append((method, path, json_body))
        if method == "GET":
            return _Resp(job_state["job"])
        return _Resp({"id": str(JOB)}, 201 if method == "POST" else 200)

    monkeypatch.setattr(flink_proxy, "_runner_call", fake_call)
    return calls


def _fetch(sha=SHA, path="jobs/orders.sql", branch="main", seen=None):
    async def fetch(**kw):
        if seen is not None:
            seen.append(kw)
        return "INSERT INTO t SELECT 1;", path, sha, branch
    return fetch


USER = types.SimpleNamespace(user_id="u1")


@pytest.fixture(autouse=True)
def workspace_ownership_is_not_the_subject_here(monkeypatch):
    """Workspace ownership has its own tests (tests/test_workspace_authz.py); here every requested workspace is owned."""
    async def owned(db, principal, requested):
        return requested
    monkeypatch.setattr(flink_proxy, "authorize_workspace", owned)


class _BodyRequest:
    def __init__(self, raw: bytes = b"{}"):
        self._raw = raw

    async def body(self):
        return self._raw


def test_importing_a_job_sends_the_structured_source_and_leaves_the_description_alone(monkeypatch, runner_calls):
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", _fetch(branch="release"))
    payload = flink_proxy.FlinkJobFromGit(git_url="https://GitHub.com/acme/jobs.git/", git_ref="release", provider="github",
                                          description="Orders [flink-git] is just words here  ref=x  with   odd   spacing")
    asyncio.run(flink_proxy.create_job_from_git(payload, None, USER, None))
    (method, path, body), = runner_calls
    assert (method, path) == ("POST", "jobs")
    src = body["source"]
    imported_at = src.pop("imported_at")
    assert src == {"git_url": "https://github.com/acme/jobs", "git_ref": "release", "requested_ref": "release",
                   "sql_path": "jobs/orders.sql", "commit_sha": SHA, "provider": "github"}
    assert datetime.fromisoformat(imported_at).tzinfo is not None              # a real, timezone-aware import time
    assert body["description"] == "Orders [flink-git] is just words here  ref=x  with   odd   spacing"   # untouched: nothing is encoded into it


def test_no_ref_asked_means_the_default_branch_is_cloned_and_the_branch_actually_used_is_recorded(monkeypatch, runner_calls):
    seen: list = []
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", _fetch(branch="trunk", seen=seen))
    asyncio.run(flink_proxy.create_job_from_git(flink_proxy.FlinkJobFromGit(git_url="https://github.com/acme/jobs"), None, USER, None))
    assert seen[0]["git_ref"] is None                                           # nothing was asked for: no ref is forced on the clone
    src = runner_calls[0][2]["source"]
    assert src["git_ref"] == "trunk" and src["requested_ref"] is None


@pytest.mark.parametrize("sha", ["", "abc123", "g" * 40, "a" * 39])
def test_an_unknown_or_malformed_commit_fails_the_import_rather_than_being_guessed_or_dropped(monkeypatch, runner_calls, sha):
    from app.core.errors import WorkspaceError
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", _fetch(sha=sha))
    payload = flink_proxy.FlinkJobFromGit(git_url="https://github.com/acme/jobs.git")
    with pytest.raises(WorkspaceError):
        asyncio.run(flink_proxy.create_job_from_git(payload, None, USER, None))
    assert runner_calls == []                                                   # nothing was created without provenance


def test_a_resync_of_a_structured_job_follows_its_requested_ref_and_keeps_the_provider(monkeypatch, runner_calls):
    job_state["job"] = STRUCTURED_JOB
    seen: list = []
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", _fetch(sha="b" * 40, path="a.sql", branch="release", seen=seen))
    asyncio.run(flink_proxy.sync_job_from_git(JOB, flink_proxy.FlinkJobSync(), None, USER))
    assert (seen[0]["git_ref"], seen[0]["provider"], seen[0]["sql_path"]) == ("release", "gitlab", "a.sql")
    patch = next(c for c in runner_calls if c[0] == "PATCH")[2]
    assert patch["source"]["commit_sha"] == "b" * 40 and patch["source"]["requested_ref"] == "release" and patch["sql"]
    assert "description" not in patch                                           # the user's text is not touched


def test_a_resync_of_a_legacy_job_reads_the_marker_then_removes_it(monkeypatch, runner_calls):
    job_state["job"] = LEGACY_JOB
    seen: list = []
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", _fetch(sha="b" * 40, path="a.sql", seen=seen))
    asyncio.run(flink_proxy.sync_job_from_git(JOB, flink_proxy.FlinkJobSync(), None, USER))
    assert seen[0]["git_url"] == "https://github.com/acme/jobs.git" and seen[0]["git_ref"] == "main"
    patch = next(c for c in runner_calls if c[0] == "PATCH")[2]
    assert patch["description"] == "my note"                                    # marker gone, user text kept
    assert patch["source"]["commit_sha"] == "b" * 40 and patch["source"]["git_url"] == "https://github.com/acme/jobs"


def test_a_job_not_created_from_git_cannot_be_synced(monkeypatch, runner_calls):
    from app.core.errors import WorkspaceError
    job_state["job"] = {"description": "plain"}
    with pytest.raises(WorkspaceError):
        asyncio.run(flink_proxy.sync_job_from_git(JOB, flink_proxy.FlinkJobSync(), None, USER))


@pytest.mark.parametrize("raw,expected", [
    ("https://GitHub.com/Acme/Jobs.git", "https://github.com/Acme/Jobs"),
    ("https://github.com/acme/jobs/", "https://github.com/acme/jobs"),
    ("  https://github.com/acme/jobs.git  ", "https://github.com/acme/jobs"),
    ("https://github.com/acme/jobs?x=1#frag", "https://github.com/acme/jobs"),
])
def test_the_repository_url_is_recorded_in_one_canonical_spelling(raw, expected):
    assert flink_proxy._canonical_repo_url(raw) == expected


def test_the_legacy_marker_is_decoded_but_never_written():
    assert flink_proxy._decode_legacy_provenance("x\n[flink-git] url=u ref=r path=p sha=s") == {"url": "u", "ref": "r", "path": "p", "sha": "s"}
    assert not hasattr(flink_proxy, "_encode_provenance")


# ---- a user cannot forge provenance through the pass-through routes ---------
def test_a_client_supplied_source_is_dropped_from_a_pass_through_body():
    body = json.dumps({"name": "j", "sql": "select 1", "source": {"commit_sha": "f" * 40}}).encode()
    assert json.loads(flink_proxy._without_source(body)) == {"name": "j", "sql": "select 1"}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'{"name": "j"}'])
def test_other_bodies_are_forwarded_untouched(raw):
    assert flink_proxy._without_source(raw) == raw


@pytest.mark.parametrize("handler,args", [("create_job", ()), ("update_job", (JOB,))])
def test_the_create_and_update_routes_strip_it(monkeypatch, handler, args):
    seen = {}

    async def fake_proxy(request, path, user, scope, *, strip_source=False, workspace_id=None):
        seen["strip"] = strip_source

    monkeypatch.setattr(flink_proxy, "_proxy", fake_proxy)
    extra = (None,) if handler == "create_job" else ()
    asyncio.run(getattr(flink_proxy, handler)(*args, _BodyRequest(), USER, *extra))
    assert seen["strip"] is True


def test_the_other_proxy_routes_do_not_touch_the_body(monkeypatch):
    seen = []

    async def fake_proxy(request, path, user, scope, *, strip_source=False, workspace_id=None):
        seen.append(strip_source)

    monkeypatch.setattr(flink_proxy, "_proxy", fake_proxy)
    for handler, args in (("get_job", (JOB,)), ("validate_job", (JOB,)), ("deploy_job", (JOB,))):
        asyncio.run(getattr(flink_proxy, handler)(*args, None, USER))
    assert seen == [False, False, False]


def _proxied_body(monkeypatch, raw: bytes, *, strip_source: bool) -> bytes:
    """What _proxy actually puts on the wire to the runner."""
    sent = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *, method, url, content, headers, params):
            sent["content"] = content
            return _Resp({"id": str(JOB)})

    class FakeRequest:
        def __init__(self):
            self.method = "POST"
            self.headers = {"content-type": "application/json"}
            self.client = None
            self.query_params = {}

        async def body(self):
            return raw

    monkeypatch.setattr(flink_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(flink_proxy, "_auth", lambda user, scope, workspace_id=None: "Bearer t")
    asyncio.run(flink_proxy._proxy(FakeRequest(), "jobs", USER, "flink.jobs.edit", strip_source=strip_source))
    return sent["content"]


def test_the_proxy_really_removes_a_forged_source_from_what_it_forwards(monkeypatch):
    raw = json.dumps({"name": "j", "sql": "select 1", "source": {"commit_sha": "f" * 40}}).encode()
    assert json.loads(_proxied_body(monkeypatch, raw, strip_source=True)) == {"name": "j", "sql": "select 1"}


def test_without_the_flag_the_proxy_forwards_the_body_as_is(monkeypatch):
    raw = json.dumps({"name": "j", "source": {"commit_sha": "f" * 40}}).encode()
    assert _proxied_body(monkeypatch, raw, strip_source=False) == raw
