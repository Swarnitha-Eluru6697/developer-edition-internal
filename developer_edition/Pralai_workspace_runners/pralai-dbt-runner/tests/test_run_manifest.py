"""DBT-006/007: the execution manifest and the Git side of freezing a run.

The ref resolver and the exact-commit checkout are exercised against REAL local Git repositories (no mocks of git),
because "what does `git ls-remote` / `git fetch <sha>` actually do" is the whole point. The database side — submit,
dispatch, retries — is in tests/test_run_freeze.py.
"""
from __future__ import annotations

import copy
import subprocess
import uuid
from pathlib import Path

import pytest

import app.services.run_manifest as rm
from app.services.git_service import GitService

SHA_A = "a" * 40


# ---------------------------------------------------------------------
# A local "remote" with a branch, a nested branch, a lightweight and an annotated tag
# ---------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """origin.git (bare) + a working clone to push more commits from. `.c1` is the first commit, `.c2` the tip of
    main; `v1` points at c1 (lightweight), `v1a` is an annotated tag on c1, `feature/x` is a nested branch."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "dbt_project.yml").write_text("name: demo\n")
    (work / "f.txt").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    c1 = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "v1")
    _git(work, "tag", "-a", "v1a", "-m", "annotated")
    (work / "f.txt").write_text("two\n")
    _git(work, "commit", "-qam", "two")
    c2 = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-b", "feature/x")
    (work / "f.txt").write_text("three\n")
    _git(work, "commit", "-qam", "three")
    _git(work, "checkout", "-q", "main")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))

    class Remote:
        url = f"file://{bare}"
        path = bare
        c1_sha, c2_sha = c1, c2

        @staticmethod
        def advance(branch: str = "main") -> str:
            """Push one more commit to `branch` and return its sha."""
            _git(work, "checkout", "-q", branch)
            (work / "f.txt").write_text(f"advance {uuid.uuid4()}\n")
            _git(work, "commit", "-qam", "advance")
            _git(work, "push", "-q", "origin", branch)
            return _git(work, "rev-parse", "HEAD")

    return Remote


# ---------------------------------------------------------------------
# Resolving a ref to a commit
# ---------------------------------------------------------------------
def test_a_branch_resolves_to_its_current_tip(remote):
    assert rm.resolve_commit_blocking(remote.url, "main") == remote.c2_sha


def test_the_tip_it_resolves_to_moves_with_the_branch(remote):
    before = rm.resolve_commit_blocking(remote.url, "main")
    new = remote.advance()
    assert new != before
    assert rm.resolve_commit_blocking(remote.url, "main") == new


def test_a_nested_branch_name_resolves(remote):
    assert rm.resolve_commit_blocking(remote.url, "feature/x") not in (remote.c1_sha, remote.c2_sha)


def test_tags_resolve_and_an_annotated_tag_is_peeled_to_its_commit(remote):
    assert rm.resolve_commit_blocking(remote.url, "v1") == remote.c1_sha
    assert rm.resolve_commit_blocking(remote.url, "v1a") == remote.c1_sha      # not the tag object's own sha


def test_a_fully_qualified_ref_resolves(remote):
    assert rm.resolve_commit_blocking(remote.url, "refs/heads/main") == remote.c2_sha


def test_a_branch_wins_over_a_tag_of_the_same_name(remote, tmp_path):
    work = tmp_path / "work"
    _git(work, "tag", "main", remote.c1_sha)               # a tag called `main` on the OLD commit
    _git(work, "push", "-q", "origin", "refs/tags/main")
    assert rm.resolve_commit_blocking(remote.url, "main") == remote.c2_sha


def test_a_full_sha_is_used_as_is_and_lowercased(remote):
    assert rm.resolve_commit_blocking(remote.url, remote.c1_sha) == remote.c1_sha
    assert rm.resolve_commit_blocking(remote.url, remote.c1_sha.upper()) == remote.c1_sha


@pytest.mark.parametrize("ref", ["nope", "feature", "ma", "refs/heads/nope", "", "  "])
def test_a_ref_that_does_not_exist_is_refused(remote, ref):
    with pytest.raises(rm.GitRefError) as e:
        rm.resolve_commit_blocking(remote.url, ref)
    assert e.value.code == "GIT_REF_NOT_FOUND"


def test_a_short_sha_cannot_be_resolved_remotely_and_is_refused(remote):
    with pytest.raises(rm.GitRefError) as e:
        rm.resolve_commit_blocking(remote.url, remote.c1_sha[:8])
    assert e.value.code == "GIT_REF_NOT_FOUND"


def test_an_unreachable_repository_is_a_different_error_from_a_missing_ref(tmp_path):
    with pytest.raises(rm.GitRefError) as e:
        rm.resolve_commit_blocking(f"file://{tmp_path}/does-not-exist.git", "main")
    assert e.value.code == "GIT_UNREACHABLE"


def test_a_git_timeout_is_reported_as_unreachable(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)
    monkeypatch.setattr(rm.subprocess, "run", boom)
    with pytest.raises(rm.GitRefError) as e:
        rm.resolve_commit_blocking("https://user:s3cret@git.example.test/a/b.git", "main")
    assert e.value.code == "GIT_UNREACHABLE" and "timed out" in e.value.message
    assert "s3cret" not in e.value.message


def test_errors_never_contain_the_clone_credential():
    with pytest.raises(rm.GitRefError) as e:
        rm.resolve_commit_blocking("http://deploy:tok-12345@127.0.0.1:9/x.git", "main")     # refused at once
    assert "tok-12345" not in e.value.message
    assert "tok-12345" not in str(e.value)


def test_redact_url_strips_user_info_only():
    assert rm.redact_url("https://user:tok@github.com/a/b.git") == "https://github.com/a/b.git"
    assert rm.redact_url("https://tok@github.com:8443/a/b.git?x=1") == "https://github.com:8443/a/b.git?x=1"
    assert rm.redact_url("https://github.com/a/b.git") == "https://github.com/a/b.git"
    assert rm.redact_url("git@github.com:a/b.git") == "git@github.com:a/b.git"      # scp style: no password
    assert rm.redact_url("file:///srv/repo.git") == "file:///srv/repo.git"


# ---------------------------------------------------------------------
# Checking out exactly the frozen commit
# ---------------------------------------------------------------------
@pytest.fixture
def git_service(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("app.services.git_service.get_settings",
                        lambda: SimpleNamespace(git_workdir=str(tmp_path / "clones")))
    return GitService()


def test_the_clone_is_the_frozen_commit_even_after_the_branch_moved(remote, git_service):
    frozen = remote.c2_sha
    remote.advance()                                    # the branch moves on while the run is queued
    assert rm.resolve_commit_blocking(remote.url, "main") != frozen

    run_id = uuid.uuid4()
    clone = git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=run_id, sha=frozen)
    assert git_service.head_sha(clone) == frozen
    assert (clone / "f.txt").read_text() == "two\n"     # c2's content, not the new tip's


def test_without_a_sha_the_clone_is_the_current_tip_as_before(remote, git_service):
    new = remote.advance()
    clone = git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=uuid.uuid4())
    assert git_service.head_sha(clone) == new


def test_a_frozen_old_commit_that_is_not_the_tip_can_be_checked_out(remote, git_service):
    clone = git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=uuid.uuid4(), sha=remote.c1_sha)
    assert git_service.head_sha(clone) == remote.c1_sha
    assert (clone / "f.txt").read_text() == "one\n"


def test_a_commit_that_no_longer_exists_fails_instead_of_using_the_branch(remote, git_service):
    with pytest.raises(RuntimeError, match="frozen when the run was submitted"):
        git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=uuid.uuid4(), sha="1" * 40)


def test_a_failed_checkout_message_has_no_credential(git_service):
    with pytest.raises(RuntimeError) as e:
        git_service.clone_for_run(git_url="http://deploy:tok-98765@127.0.0.1:9/x.git", git_ref="main",
                                  run_id=uuid.uuid4(), sha="2" * 40)
    assert "tok-98765" not in str(e.value)


def test_a_server_that_refuses_fetch_by_sha_falls_back_to_the_refs_history(remote, git_service, monkeypatch):
    """Some Git servers do not allow `fetch <sha>`. Simulated by making the first (by-sha) fetch fail."""
    import git as gitpython
    real = gitpython.cmd.Git.execute
    state = {"calls": 0}

    def flaky(self, command, *a, **kw):
        if "fetch" in command and "--depth" in command and SHA_A not in command:
            state["calls"] += 1
            raise gitpython.GitCommandError(command, 128, stderr="fatal: server does not allow request for unadvertised object")
        return real(self, command, *a, **kw)

    monkeypatch.setattr(gitpython.cmd.Git, "execute", flaky)
    clone = git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=uuid.uuid4(), sha=remote.c1_sha)
    assert state["calls"] == 1
    assert git_service.head_sha(clone) == remote.c1_sha


# ---------------------------------------------------------------------
# The manifest document
# ---------------------------------------------------------------------
def _manifest(**kw):
    args = {
        "git_url": "https://deploy:tok-1@github.com/acme/a.git", "git_ref": "main", "git_sha": SHA_A,
        "project_path": "dbt/sales", "engine": "trino", "select": "tag:nightly",
        "dq_blocks": {"stg_orders": "models:\n- name: stg_orders\n", "dim_x": "models: []\n"},
        "dq_meta": {"stg_orders": {"artifact_version_id": "av-7", "binding_id": "b-1", "content_hash": "ch-9",
                                   "renderer_version": "2.1", "idempotency_key": "k-1",
                                   "applied_at": "2026-10-14T00:00:00+00:00"}},
    }
    args.update(kw)
    return rm.build_manifest(**args)


def test_the_manifest_records_the_commit_the_run_settings_and_the_dq_artifacts():
    m = _manifest()
    assert m["manifest_version"] == rm.MANIFEST_VERSION == "1"
    assert m["git"] == {"url": "https://github.com/acme/a.git", "ref": "main", "sha": SHA_A, "source": "ref"}
    assert (m["project_path"], m["engine"], m["select"]) == ("dbt/sales", "trino", "tag:nightly")
    block = m["dq"]["blocks"]["stg_orders"]
    assert block["artifact_version_id"] == "av-7" and block["binding_id"] == "b-1"
    assert block["content_hash"] == "ch-9" and block["renderer_version"] == "2.1"
    assert block["yaml"] == "models:\n- name: stg_orders\n"
    assert block["yaml_sha256"] == rm.sha256_text(block["yaml"])


def test_a_block_pushed_without_artifact_metadata_is_kept_and_visibly_unattributed():
    block = _manifest()["dq"]["blocks"]["dim_x"]
    assert block["yaml"] == "models: []\n" and block["yaml_sha256"]
    assert block["artifact_version_id"] is None and block["content_hash"] is None


def test_the_manifest_never_contains_the_clone_credential():
    assert "tok-1" not in repr(_manifest())


def test_no_dq_blocks_is_an_empty_snapshot():
    assert _manifest(dq_blocks=None, dq_meta=None)["dq"] == {"blocks": {}}
    assert rm.dq_blocks(_manifest(dq_blocks={}, dq_meta={})) == {}


def test_dq_blocks_are_returned_in_the_shape_the_merger_takes():
    assert rm.dq_blocks(_manifest()) == {"stg_orders": "models:\n- name: stg_orders\n", "dim_x": "models: []\n"}


def test_the_hash_is_stable_and_independent_of_key_order():
    m = _manifest()
    reordered = {k: m[k] for k in reversed(list(m))}
    assert rm.manifest_sha256(m) == rm.manifest_sha256(reordered) == rm.manifest_sha256(copy.deepcopy(m))


def test_any_change_changes_the_hash():
    base = rm.manifest_sha256(_manifest())
    assert rm.manifest_sha256(_manifest(git_sha="b" * 40)) != base
    assert rm.manifest_sha256(_manifest(engine="pg_lake")) != base
    assert rm.manifest_sha256(_manifest(dq_blocks={"stg_orders": "models: []\n"})) != base
    assert rm.manifest_sha256(_manifest(dq_meta={})) != base


def test_verify_accepts_an_untouched_manifest():
    m = _manifest()
    assert rm.verify(m, rm.manifest_sha256(m)) is m


def test_verify_refuses_a_changed_commit_a_changed_block_and_missing_data():
    m = _manifest()
    good = rm.manifest_sha256(m)
    changed = copy.deepcopy(m)
    changed["git"]["sha"] = "b" * 40
    with pytest.raises(rm.ManifestIntegrityError, match="does not match the hash"):
        rm.verify(changed, good)
    with pytest.raises(rm.ManifestIntegrityError):
        rm.verify(None, good)
    with pytest.raises(rm.ManifestIntegrityError):
        rm.verify(m, None)


def test_verify_catches_a_dq_block_edited_along_with_a_recomputed_manifest_hash():
    """Someone who also fixes up manifest_sha256 still cannot change a block without its own hash."""
    m = _manifest()
    m["dq"]["blocks"]["stg_orders"]["yaml"] = "models: []\n"
    with pytest.raises(rm.ManifestIntegrityError, match="stg_orders"):
        rm.verify(m, rm.manifest_sha256(m))


def test_verify_refuses_an_unknown_manifest_version():
    m = _manifest()
    m["manifest_version"] = "99"
    with pytest.raises(rm.ManifestIntegrityError, match="version"):
        rm.verify(m, rm.manifest_sha256(m))


# ---------------------------------------------------------------------
# X-004: the manifest carries who/where/what-runs-it, and the build leaves evidence
# ---------------------------------------------------------------------
def test_the_manifest_records_org_workspace_subject_runtime_and_idempotency_key():
    m = _manifest(org_id="org-1", workspace_id="ws-9", subject_id="user-7", trigger="manual", idempotency_key="k-1")
    assert (m["org_id"], m["workspace_id"], m["subject_id"], m["trigger"]) == ("org-1", "ws-9", "user-7", "manual")
    assert m["idempotency_key"] == "k-1"
    assert m["runtime_version"]["dbt_core"] and m["runtime_version"]["runner"]
    assert m["target_resource_id"] is None                       # known only after the build
    assert m["manifest_version"] == "1"


def test_the_dq_artifact_summary_names_each_artifact_version_and_hash():
    (a, b) = _manifest()["dq_artifacts"]
    assert a["model"] == "dim_x" and a["version"] is None and a["sha256"]       # unattributed (legacy push)
    assert (b["model"], b["id"], b["version"], b["content_hash"]) == ("stg_orders", "b-1", "av-7", "ch-9")
    assert b["sha256"] == rm.sha256_text("models:\n- name: stg_orders\n")


def test_the_request_fingerprint_binds_a_key_to_what_was_asked():
    base = rm.request_fingerprint("tag:a", SHA_A)
    assert rm.request_fingerprint("tag:a", SHA_A.upper()) == base
    assert rm.request_fingerprint("tag:b", SHA_A) != base
    assert rm.request_fingerprint("tag:a", "b" * 40) != base
    assert rm.request_fingerprint("tag:a", None) != base
    assert rm.request_fingerprint(None, None) == rm.request_fingerprint(None, "")


def test_a_manifest_frozen_with_the_first_integer_version_is_still_accepted():
    m = _manifest()
    del m["manifest_version"]
    m["version"] = 1
    assert rm.verify(m, rm.manifest_sha256(m)) is m


def test_tree_sha_is_gits_hash_of_the_committed_project_folder(remote, git_service):
    clone = git_service.clone_for_run(git_url=remote.url, git_ref="main", run_id=uuid.uuid4(), sha=remote.c2_sha)
    expect = _git(clone, "rev-parse", "HEAD^{tree}")
    assert git_service.tree_sha(clone, clone) == expect
    (clone / "f.txt").write_text("changed in the working tree, not committed\n")      # what DQ injection does
    assert git_service.tree_sha(clone, clone) == expect


def test_tree_sha_of_a_subfolder_changes_only_when_that_folder_changes(tmp_path, git_service):
    work = tmp_path / "w"
    (work / "a").mkdir(parents=True)
    (work / "b").mkdir()
    _git(work.parent, "init", "-q", "-b", "main", str(work))
    (work / "a" / "m.sql").write_text("select 1\n")
    (work / "b" / "m.sql").write_text("select 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    first = git_service.tree_sha(work, work / "a")
    assert first == _git(work, "rev-parse", "HEAD:a")
    (work / "b" / "m.sql").write_text("select 3\n")
    _git(work, "commit", "-qam", "touch b only")
    assert git_service.tree_sha(work, work / "a") == first                        # a/ is byte-identical
    (work / "a" / "m.sql").write_text("select 99\n")
    _git(work, "commit", "-qam", "touch a")
    assert git_service.tree_sha(work, work / "a") != first


def test_tree_sha_is_none_when_it_cannot_be_determined(tmp_path, git_service):
    assert git_service.tree_sha(tmp_path, tmp_path) is None            # not a repository
