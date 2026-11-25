"""Reading files out of a cloned repository without being led out of it (W-012).

A repository is untrusted input. It can commit `secret.sql -> /etc/passwd`, or a symlink to a mounted secret
directory; a plain read follows it and copies a file from the Workspace pod into a workspace (or a Flink
job's SQL). Every read of a file in a clone goes through read_repo_file():

  1. lstat the path: a symbolic link is rejected outright, and so is anything that is not a regular file
     (a FIFO or device would otherwise block or misbehave on read);
  2. resolve the path and require it to stay inside the clone root (catches a symlinked PARENT directory and
     `..` escapes), using is_relative_to so `/repo-evil` is not mistaken for `/repo`;
  3. open with O_NOFOLLOW | O_NONBLOCK (defence in depth against the path being swapped after the check) and
     fstat the descriptor: it must be the same regular file lstat saw;
  4. read at most `max_bytes + 1` bytes, so an oversized file is refused without being loaded into memory.

The rejection reasons never include the link's target.

This module is the ONLY place Workspace reads files from, or walks, a cloned repository (W-013): the workspace
Git import and Flink "create job from Git" both build on it, so they cannot drift into different rules.

  walk_repo_files()   what is a candidate file and what is rejected (symlinks, FIFOs); never descends a link
  ensure_safe_path()  the check for an explicit path a caller supplies (a file or a directory)
  read_repo_file()    the read itself

tests/test_repo_file_rules.py fails if any other module reads files or walks directories on its own.
"""
from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SYMLINK_REASON = "symbolic link (not followed)"
NOT_REGULAR_REASON = "not a regular file"


class UnsafeRepoPath(Exception):
    """A path in a cloned repo that must not be read. `reason` is safe to show a user (no link target)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FileTooLarge(Exception):
    """The file is larger than the caller's limit (not read)."""


def ensure_inside(repo_root: Path | str, path: Path | str) -> Path:
    """The fully resolved `path`, provided it is `repo_root` or lies inside it; otherwise UnsafeRepoPath."""
    root = Path(os.path.realpath(repo_root))
    resolved = Path(os.path.realpath(path))
    if not resolved.is_relative_to(root):
        raise UnsafeRepoPath("resolves outside the repository")
    return resolved


def _lstat_not_a_link(path: Path | str) -> os.stat_result:
    """lstat (never stat: do not follow) and refuse a symbolic link."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeRepoPath(SYMLINK_REASON)
    return st


def ensure_safe_path(repo_root: Path | str, path: Path | str) -> Path:
    """An explicit path a caller supplies (a file OR a directory): it must exist, must not be a symbolic link
    itself, and must resolve inside `repo_root` (so a symlinked parent directory is caught too).
    Returns the resolved path. Raises FileNotFoundError if it does not exist, UnsafeRepoPath otherwise."""
    try:
        _lstat_not_a_link(path)
    except FileNotFoundError:
        ensure_inside(repo_root, path)                    # an escape is reported as an escape, whether or not it exists
        raise
    return ensure_inside(repo_root, path)


@dataclass(frozen=True)
class RepoEntry:
    """One entry of a repository walk. `rejected` is why it must not be read (None = a regular file)."""
    path: Path
    rel: str
    rejected: str | None


def walk_repo_files(repo_root: Path | str, *, skip_dirs: tuple[str, ...] = (".git",)) -> Iterator[RepoEntry]:
    """Every file-like entry under `repo_root`, in a stable (sorted) order, classified once for every caller.

    A symbolic link (to a file OR a directory) and any non-regular file (FIFO, socket, device) is yielded with a
    `rejected` reason and is never descended into or read; a directory symlink's contents never appear. `.git`
    is skipped. A symlinked `repo_root` itself is refused. A missing root yields nothing."""
    root = Path(repo_root)
    if root.is_symlink():
        raise UnsafeRepoPath(SYMLINK_REASON)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for d in list(dirs):
            link = Path(dirpath) / d
            if link.is_symlink():                      # os.walk lists it with the directories but will not follow it
                dirs.remove(d)
                yield RepoEntry(link, str(link.relative_to(root)), SYMLINK_REASON)
        for name in sorted(files):
            path = Path(dirpath) / name
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue                               # vanished between listing and lstat
            rejected = SYMLINK_REASON if stat.S_ISLNK(mode) else None if stat.S_ISREG(mode) else NOT_REGULAR_REASON
            yield RepoEntry(path, str(path.relative_to(root)), rejected)


def read_repo_file(repo_root: Path | str, path: Path | str, *, max_bytes: int) -> bytes:
    """The bytes of a regular file inside `repo_root`; never through a symbolic link."""
    st = _lstat_not_a_link(path)                          # not stat(): do not follow
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeRepoPath(NOT_REGULAR_REASON)
    ensure_inside(repo_root, path)

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):        # O_NOFOLLOW refused a symlink swapped in after lstat
            raise UnsafeRepoPath(SYMLINK_REASON) from None
        raise
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise UnsafeRepoPath("changed while being read")
        with os.fdopen(fd, "rb") as f:
            fd = -1                                       # the file object owns it now
            data = f.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise FileTooLarge()
    return data
