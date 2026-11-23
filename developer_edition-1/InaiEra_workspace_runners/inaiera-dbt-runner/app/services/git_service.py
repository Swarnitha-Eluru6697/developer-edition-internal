"""Git working-copy helper.

dbt Runner clones into a per-run ephemeral directory (never a shared
working copy — concurrent runs of different projects, or even the same
project, must not interleave file state). Mirrors the isolation principle
already established for Workspace's notebook kernels and codegen runs.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import git
import structlog

from app.core.config import get_settings
from app.core.git_credentials import git_env
from app.services.run_manifest import redact_url, scrub

log = structlog.get_logger()


class GitService:
    def __init__(self) -> None:
        settings = get_settings()
        self._root = Path(settings.git_workdir)
        self._root.mkdir(parents=True, exist_ok=True)

    def clone_for_run(self, *, git_url: str, git_ref: str, run_id: uuid.UUID,
                      sha: str | None = None, token: str | None = None) -> Path:
        """Shallow-clone into a fresh directory scoped to this run. With `sha` (DBT-006: the commit frozen in the
        run's manifest) exactly that commit is checked out, whatever `git_ref` points to now; without it the
        ref's tip is cloned. `token` (from the project's credential_ref) authenticates through git's environment, never the URL.
        Caller is responsible for cleanup via cleanup(run_id)."""
        target = self._root / str(run_id)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)

        log.info("git_clone_start", git_url=redact_url(git_url), git_ref=git_ref, sha=sha, run_id=str(run_id))
        if sha:
            self._checkout_commit(target, git_url=git_url, git_ref=git_ref, sha=sha, token=token)
            log.info("git_clone_done", run_id=str(run_id), sha=sha)
            return target
        repo = git.Repo.clone_from(
            git_url, target,
            branch=git_ref,
            depth=1,
            env=git_env(git_url, token),
        )
        log.info("git_clone_done", run_id=str(run_id), sha=repo.head.commit.hexsha)
        return target

    @staticmethod
    def _checkout_commit(target: Path, *, git_url: str, git_ref: str, sha: str, token: str | None = None) -> None:
        """Fetch exactly `sha` and check it out detached. Most servers allow a depth-1 fetch of one commit; for one
        that does not, fetch the ref's history and require the commit to be in it. A commit that is gone from the
        remote (force-pushed away) FAILS the run — it never falls back to whatever the ref points to now."""
        repo = git.Repo.init(target)
        repo.create_remote("origin", git_url)
        try:
            with repo.git.custom_environment(**git_env(git_url, token)):
                try:
                    repo.git.fetch("--depth", "1", "origin", sha)
                except git.GitCommandError:
                    repo.git.fetch("origin", git_ref)
                repo.git.checkout("--detach", sha)
        except git.GitCommandError as e:
            raise RuntimeError(
                f"commit {sha} (frozen when the run was submitted) cannot be fetched from "
                f"{redact_url(git_url)}: {scrub(str(e.stderr or e), git_url)}") from None
        actual = repo.head.commit.hexsha
        if actual != sha:
            raise RuntimeError(f"checked out {actual} instead of the run's commit {sha}")

    def list_dbt_projects(self, *, git_url: str, git_ref: str, token: str | None = None) -> list[str]:
        """Folders (repo-relative, '' = repo root) that hold a dbt_project.yml at
        git_ref. Fetches only the commit's file tree (no checkout, no file
        contents where the server supports it), so registration can reject a
        wrong project name up front instead of failing on the first run.
        Raises git.GitCommandError if the repo or ref cannot be read."""
        target = self._root / f"check-{uuid.uuid4()}"
        target.mkdir(parents=True)
        try:
            repo = git.Repo.clone_from(
                git_url, target, branch=git_ref, depth=1,
                multi_options=["--no-checkout", "--filter=blob:none"],
                env=git_env(git_url, token),
            )
            names = repo.git.ls_tree("-r", "--name-only", "HEAD").splitlines()
        finally:
            shutil.rmtree(target, ignore_errors=True)
        folders = []
        for name in names:
            parts = name.split("/")
            if parts[-1] != "dbt_project.yml" or {"dbt_packages", "target", "node_modules"} & set(parts):
                continue
            folders.append("/".join(parts[:-1]))
        return sorted(folders, key=lambda f: (len(f.split("/")), f))

    def file_exists(self, *, git_url: str, git_ref: str, path: str, token: str | None = None) -> bool:
        """Whether `path` is a file of the repo at git_ref (same tree-only fetch as list_dbt_projects).
        Raises git.GitCommandError if the repo or ref cannot be read."""
        target = self._root / f"check-{uuid.uuid4()}"
        target.mkdir(parents=True)
        try:
            repo = git.Repo.clone_from(
                git_url, target, branch=git_ref, depth=1,
                multi_options=["--no-checkout", "--filter=blob:none"],
                env=git_env(git_url, token),
            )
            return path in repo.git.ls_tree("-r", "--name-only", "HEAD").splitlines()
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def head_sha(self, path: Path) -> str:
        return git.Repo(path).head.commit.hexsha

    def tree_sha(self, clone_dir: Path, project_dir: Path) -> str | None:
        """Git's own hash of the committed project folder (X-004 evidence of what was built, with the commit):
        the tree id of `project_dir` at HEAD. None if it cannot be determined."""
        try:
            rel = project_dir.resolve().relative_to(clone_dir.resolve()).as_posix()
            repo = git.Repo(clone_dir)
            return repo.git.rev_parse("HEAD^{tree}" if rel in ("", ".") else f"HEAD:{rel}")
        except Exception:  # noqa: BLE001 — evidence, never a reason to fail a build
            return None

    def cleanup(self, run_id: uuid.UUID) -> None:
        target = self._root / str(run_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


_service: GitService | None = None


def get_git_service() -> GitService:
    global _service
    if _service is None:
        _service = GitService()
    return _service
