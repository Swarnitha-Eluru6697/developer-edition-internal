"""DG-010 / DG-011 in the Dagster code server and location manager.

DG-010: nothing here builds a REPLACEMENT environment for a subprocess (the old git sensor did, with just GIT_TERMINAL_PROMPT=0); Dagster no longer
runs git at all (DG-009), and the one subprocess it starts (the per-repo code server) inherits the whole environment.
DG-011: a repository credential that reaches Dagster in any string (a project record from an older runner, an exception message) never reaches a
log line.
"""
from __future__ import annotations

import ast
import json
import logging
import subprocess
from pathlib import Path

import structlog
from pralai_dbt_runner_dags import location_manager, log_scrub
from pralai_dbt_runner_dags.log_scrub import scrub_urls

PKG = Path(__file__).resolve().parents[1] / "pralai_dbt_runner_dags"
TOKEN = "ghp_SUPERSECRET123"
URL = f"https://ci:{TOKEN}@github.com/acme/private.git"


# ── DG-010 ────────────────────────────────────────────────────────────────────
def test_the_code_server_subprocess_inherits_the_whole_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
    monkeypatch.setenv("HOME", "/enterprise/home")
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw["env"]))
    location_manager.Manager.__new__(location_manager.Manager)._spawn("some.module", 4001, {"DBT_LOCATION": "x-12345678"})
    assert seen["HTTPS_PROXY"] == "http://proxy.corp:3128" and seen["SSL_CERT_FILE"] == "/etc/corp/ca.pem" and seen["HOME"] == "/enterprise/home"
    assert seen["DBT_LOCATION"] == "x-12345678"                        # what it adds


def test_no_module_passes_a_hand_built_environment_to_a_subprocess():
    """Every `env=` given to subprocess.run/Popen must start from os.environ. (Dagster runs no git now; this keeps a future change honest.)"""
    offenders = []
    for path in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("run", "Popen", "check_output", "call"):
                for kw in node.keywords:
                    if kw.arg == "env" and "os.environ" not in ast.unparse(kw.value):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


def test_dagster_runs_no_git():
    """No subprocess call whose command starts with `git` (DG-009: the dbt Runner resolves refs and holds the credentials)."""
    for path in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List) and node.args[0].elts:
                first = node.args[0].elts[0]
                assert not (isinstance(first, ast.Constant) and first.value == "git"), f"{path.name}:{node.lineno} starts git"


# ── DG-011 ────────────────────────────────────────────────────────────────────
def test_scrub_urls():
    assert scrub_urls(f"cannot read {URL}") == "cannot read https://***@github.com/acme/private.git"
    assert scrub_urls("https://github.com/acme/clean.git") == "https://github.com/acme/clean.git"
    assert scrub_urls("git@github.com:acme/repo.git") == "git@github.com:acme/repo.git"


def test_structlog_events_are_scrubbed_in_every_value(capsys):
    structlog.get_logger().warning("location_defs.built", project={"git_url": URL, "id": "1"}, error=f"boom {URL}")
    out = capsys.readouterr().out
    assert TOKEN not in out and "ci:" not in out and "https://***@github.com/acme/private.git" in out


def test_a_logged_exception_is_scrubbed(capsys):
    try:
        raise RuntimeError(f"cannot read {URL}")
    except RuntimeError:
        structlog.get_logger().exception("fetch_failed")
    assert TOKEN not in capsys.readouterr().out


def test_standard_library_records_are_scrubbed(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("a.library").warning("fetching %s", URL)
        try:
            raise ValueError(URL)
        except ValueError:
            logging.getLogger("a.library").exception("failed")
    assert TOKEN not in "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)


def test_importing_the_package_installs_the_scrubber():
    """Both the code server and the location manager import the package, so neither needs its own wiring."""
    assert log_scrub.scrub_event in structlog.get_config()["processors"]


def test_a_project_record_with_a_credential_never_puts_it_in_a_location_name():
    from pralai_dbt_runner_dags.naming import location_name
    name = location_name({"git_url": URL})
    assert TOKEN not in name and json.dumps(name).count("ci:") == 0
