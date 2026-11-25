"""DG-010: every git subprocess the runner starts INHERITS the process environment and only adds to it.

The finding: a Git call given an environment of just `GIT_TERMINAL_PROMPT=0` loses HOME, proxy variables, CA settings, SSH_AUTH_SOCK,
GIT_ASKPASS and the rest of an enterprise's Git configuration. The Dagster sensor that did this no longer runs git (DG-009); the runner is
now the one place that does, so this pins its behaviour with a fake `git` executable that records the environment each invocation receives.

Supported authentication (see app/core/git_credentials.py): HTTPS with a per-project `credential_ref` token (an Authorization header scoped to
the repository's host); ssh and anything else authenticate through the pod's own environment (SSH_AUTH_SOCK, GIT_SSH_COMMAND, ~/.ssh, a
credential helper or GIT_ASKPASS the operator provides) — the runner passes all of it through untouched.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import git
import pytest

from app.core.git_credentials import git_env
from app.services import run_manifest
from app.services.git_service import GitService

INHERITED = {
    "HOME": "/enterprise/home", "HTTPS_PROXY": "http://proxy.corp:3128", "NO_PROXY": ".svc,.corp", "SSL_CERT_FILE": "/etc/corp/ca.pem",
    "GIT_SSL_CAINFO": "/etc/corp/ca.pem", "SSH_AUTH_SOCK": "/run/agent.sock", "GIT_ASKPASS": "/opt/corp/askpass",
    "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=yes -i /keys/id",
}


@pytest.fixture
def enterprise_env(monkeypatch):
    for k, v in INHERITED.items():
        monkeypatch.setenv(k, v)


def _assert_inherited(env: dict, *, except_keys=()):
    for k, v in INHERITED.items():
        if k not in except_keys:
            assert env.get(k) == v, f"{k} was lost: {env.get(k)!r}"
    assert env.get("GIT_TERMINAL_PROMPT") == "0"                      # what the runner ADDS


# ── git as a plain subprocess (ls-remote) ───────────────────────────────────────────
def test_ls_remote_inherits_the_process_environment(enterprise_env, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return type("R", (), {"returncode": 0, "stdout": "a" * 40 + "\trefs/heads/main\n", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_manifest.resolve_commit_blocking("https://github.com/acme/x.git", "main") == "a" * 40
    _assert_inherited(seen)


def test_ls_remote_with_a_token_adds_the_header_and_still_inherits(enterprise_env, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return type("R", (), {"returncode": 0, "stdout": "a" * 40 + "\trefs/heads/main\n", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_manifest.resolve_commit_blocking("https://github.com/acme/x.git", "main", "tok")
    _assert_inherited(seen)
    assert seen["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader" and "tok" not in " ".join(seen.get("GIT_CONFIG_KEY_0", ""))


# ── git through GitPython (clone / fetch / checkout) ───────────────────────────────────
@pytest.fixture
def recording_git(tmp_path, monkeypatch):
    """A fake `git` that appends its environment to a file and fails, so every code path is observed without a network."""
    import shutil
    real = shutil.which("git")
    dump = tmp_path / "env.dump"
    script = tmp_path / "fakegit"
    # local plumbing (init, remote add, version...) runs for real; the commands that would touch the network record their environment and fail
    script.write_text(f'#!/bin/sh\ncase "$1" in\n  fetch|clone|checkout|ls-tree)\n    printf "=== %s\\n" "$*" >> "{dump}"; env >> "{dump}"; exit 128;;\n'
                      f'  *) exec "{real}" "$@";;\nesac\n')
    script.chmod(0o755)
    monkeypatch.setenv("GIT_PYTHON_GIT_EXECUTABLE", str(script))
    git.refresh()
    yield dump
    monkeypatch.delenv("GIT_PYTHON_GIT_EXECUTABLE", raising=False)
    git.refresh()


def _invocations(dump: Path) -> list[dict]:
    blocks, current = [], None
    for line in dump.read_text().splitlines():
        if line.startswith("=== "):
            current = {"__args__": line[4:]}
            blocks.append(current)
        elif current is not None and "=" in line:
            k, _, v = line.partition("=")
            current[k] = v
    return blocks


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(GitService, "__init__", lambda self: setattr(self, "_root", tmp_path / "work") or self._root.mkdir())
    return GitService()


def test_a_clone_inherits_the_process_environment(enterprise_env, recording_git, service):
    with pytest.raises((git.GitCommandError, RuntimeError)):
        service.clone_for_run(git_url="https://github.com/acme/x.git", git_ref="main", run_id=uuid.uuid4())
    (clone,) = [b for b in _invocations(recording_git) if "clone" in b["__args__"]]
    _assert_inherited(clone)


def test_a_commit_fetch_and_checkout_inherit_the_process_environment(enterprise_env, recording_git, service):
    with pytest.raises(RuntimeError):
        service.clone_for_run(git_url="https://github.com/acme/x.git", git_ref="main", run_id=uuid.uuid4(), sha="b" * 40, token="tok")
    fetches = [b for b in _invocations(recording_git) if "fetch" in b["__args__"]]
    assert fetches
    for f in fetches:
        _assert_inherited(f)
        assert f["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"           # the token's header, added on top


def test_listing_dbt_projects_inherits_the_process_environment(enterprise_env, recording_git, service):
    with pytest.raises((git.GitCommandError, RuntimeError)):
        service.list_dbt_projects(git_url="https://github.com/acme/x.git", git_ref="main")
    (clone,) = [b for b in _invocations(recording_git) if "clone" in b["__args__"]]
    _assert_inherited(clone)


def test_an_ssh_remote_gets_nothing_added_but_the_prompt_switch_so_the_pods_own_ssh_setup_applies(enterprise_env):
    assert git_env("ssh://git@github.com/acme/x.git", "ignored") == {"GIT_TERMINAL_PROMPT": "0"}
    assert git_env("git@github.com:acme/x.git", None) == {"GIT_TERMINAL_PROMPT": "0"}


def test_git_env_itself_is_only_additions_never_a_replacement():
    """It is a dict of what to ADD; callers merge it over the inherited environment. It must never pretend to be a whole environment."""
    for url, token in (("https://h/x.git", "t"), ("https://h/x.git", None), ("ssh://git@h/x.git", None)):
        assert set(git_env(url, token)) <= {"GIT_TERMINAL_PROMPT", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                                            "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"}
