"""DG-011: a repository credential never reaches a log line or a stored error message, whoever logged it.

Registration refuses user info in a git URL (DBT-009), but a credential can still arrive in text the runner did not write: a git error that
echoes the URL it was given, an exception message, a legacy row. Every log event and every stored run error goes through one scrubber.
"""
from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.core import log_scrub
from app.core.log_scrub import scrub_urls
from app.services.run_service import dispatch_run

TOKEN = "ghp_SUPERSECRET123"
URL = f"https://ci:{TOKEN}@github.com/acme/private.git"


@pytest.mark.parametrize("text,expected", [
    (f"clone of {URL} failed", "clone of https://***@github.com/acme/private.git failed"),
    (f"https://{TOKEN}@github.com/a/b.git", "https://***@github.com/a/b.git"),
    (f"a {URL} and b http://u:p@h:8080/x.git#subdirectory=dbt c", "a https://***@github.com/acme/private.git and b http://***@h:8080/x.git#subdirectory=dbt c"),
    ("ssh://git:pw@host/x.git", "ssh://***@host/x.git"),
    ("fatal: could not read from 'https://u:p@h/x.git'", "fatal: could not read from 'https://***@h/x.git'"),
    ("https://github.com/acme/clean.git", "https://github.com/acme/clean.git"),                 # nothing to hide: unchanged
    ("git@github.com:acme/repo.git", "git@github.com:acme/repo.git"),                           # scp-style: not a URL with a scheme
    ("mail me at a@b.c", "mail me at a@b.c"),
    ("", ""),
])
def test_scrub_urls(text, expected):
    assert scrub_urls(text) == expected


def test_structlog_events_are_scrubbed_in_every_value_nested_too(capsys):
    from app.main import _configure_logging
    _configure_logging("info")
    structlog.get_logger().warning("clone_failed", git_url=URL, detail={"cmd": [f"git clone {URL}"], "n": 3}, error=f"boom {URL}")
    out = capsys.readouterr().out
    assert TOKEN not in out and "ci:" not in out
    event = json.loads(out.strip().splitlines()[-1])
    assert event["git_url"] == "https://***@github.com/acme/private.git" and event["detail"]["n"] == 3


def test_a_logged_exception_is_scrubbed_too(capsys):
    from app.main import _configure_logging
    _configure_logging("info")
    try:
        raise RuntimeError(f"cannot read {URL}")
    except RuntimeError:
        structlog.get_logger().exception("clone_crashed")
    out = capsys.readouterr().out
    assert "cannot read https://***@github.com/acme/private.git" in out and TOKEN not in out


def test_standard_library_records_are_scrubbed_message_arguments_and_traceback(caplog):
    log_scrub.install_stdlib()
    with caplog.at_level(logging.INFO):
        logging.getLogger("some.library").warning("fetching %s for %s", URL, {"url": URL})
        try:
            raise ValueError(f"bad {URL}")
        except ValueError:
            logging.getLogger("some.library").exception("failed %s", URL)
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert TOKEN not in text and "https://***@github.com/acme/private.git" in text


def test_installing_twice_does_not_stack_processors():
    log_scrub.add_to_structlog()
    log_scrub.add_to_structlog()
    assert structlog.get_config()["processors"].count(log_scrub.scrub_event) == 1


# ── stored error text ───────────────────────────────────────────────────────
async def test_a_failed_runs_stored_error_never_contains_a_credential(run_db, monkeypatch, tmp_path):
    from tests.test_run_service import _make_project_and_run, _patch_dispatch_deps
    _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": False, "errors": [f"fatal: unable to access '{URL}': 403"], "nodes": []})
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    await dispatch_run(run_id)
    async with run_db() as db:
        from app.models import Run
        run = await db.get(Run, run_id)
    assert run.status == "failed" and TOKEN not in run.error_message and "https://***@github.com" in run.error_message


async def test_a_clone_that_raises_is_stored_without_the_credential(run_db, monkeypatch, tmp_path):
    from tests.test_run_service import _make_project_and_run, _patch_dispatch_deps

    _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": True, "errors": [], "nodes": []})

    class _Boom:
        def clone_for_run(self, **kw):
            raise RuntimeError(f"Cmd('git') failed: clone {URL}")

        def cleanup(self, run_id):
            pass
    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: _Boom())
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    await dispatch_run(run_id)
    async with run_db() as db:
        from app.models import Run
        run = await db.get(Run, run_id)
    assert run.status == "failed" and TOKEN not in (run.error_message or "")


async def test_a_run_that_could_not_be_started_is_failed_without_the_credential(run_db):
    from app.services.run_service import _fail_queued
    from tests.test_run_service import _make_project_and_run
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    await _fail_queued(run_id, f"cannot resolve {URL}")
    async with run_db() as db:
        from app.models import Run
        assert TOKEN not in (await db.get(Run, run_id)).error_message


def test_registration_still_refuses_a_credentialed_url():
    """The other half of DG-011 (DBT-009): the credential is not accepted in the first place."""
    from app.schemas.project import ProjectRegister
    with pytest.raises(ValueError, match="credential_ref"):
        ProjectRegister(git_url=URL, git_ref="main")
