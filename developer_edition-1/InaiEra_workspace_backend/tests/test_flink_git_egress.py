"""Flink job registration from Git applies the same egress rules as every
other clone (WS-GIT-009): no server-side fetch of an arbitrary URL, and a
provider's token never goes to a host that isn't that provider's."""
from __future__ import annotations

import asyncio

import pytest

from app.api import flink_proxy
from app.core.errors import WorkspaceError


@pytest.fixture
def no_clone(monkeypatch):
    calls = []
    import app.api.git as git_api

    def _clone(*a, **k):
        calls.append(a)
        raise AssertionError("must not clone")
    monkeypatch.setattr(git_api, "_do_clone", _clone)

    async def _token(_u, _p):
        return "ghp_token"
    monkeypatch.setattr(git_api, "load_git_token", _token)
    return calls


@pytest.mark.parametrize("url,provider", [
    ("http://github.com/acme/jobs.git", "github"),               # not https
    ("https://169.254.169.254/latest/meta-data", "github"),       # metadata endpoint
    ("https://internal.acme.local/jobs.git", "github"),           # not allowlisted
    ("https://gitlab.com/acme/jobs.git", "github"),               # github token → gitlab host
    ("file:///etc/passwd", "github"),
])
def test_disallowed_urls_are_refused_before_cloning(no_clone, url, provider):
    with pytest.raises(WorkspaceError) as ei:
        asyncio.run(flink_proxy._fetch_sql_from_git(
            git_url=url, git_ref="main", sql_path=None, provider=provider, user_id="u",
        ))
    assert ei.value.status_code == 400
    assert "not permitted" in ei.value.detail["message"]
    assert no_clone == []
