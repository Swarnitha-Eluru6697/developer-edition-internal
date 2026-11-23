"""Tests for what a Notion source can see, and what it says when that
is nothing.

Reported symptom: "Notion account is linked but there are no files."
Both halves were true and neither was a fault — the token is valid
(`users/me` returns the bot `inaiEra` in the `Aadhya Analytics and Info
Logics Pvt` workspace) and `search` genuinely returns zero results,
because a Notion integration sees nothing until each page or database is
connected to it. That is Notion's design, not a permission someone can
grant once centrally.

So the listing was correct and the UI was uninformative: "no files" with
nothing to say why reads as a broken connection. It now names the
integration and its workspace, which is what someone needs to find it in
the page's own Connections menu.

The second fix here is pagination: both calls asked for one page of 100
and ignored `has_more`, so a workspace with more connected content
showed a silent subset.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import data_sources


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeNotion:
    """Serves `search`, `blocks/{id}/children` and `users/me`, honouring
    cursors so the paging can be tested."""

    def __init__(self, *, search_pages=(), children_pages=(), me=None):
        self.search_pages = list(search_pages) or [[]]
        self.children_pages = list(children_pages) or [[]]
        self.me = me
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def _page(pages, cursor):
        index = int(cursor or 0)
        results = pages[index] if index < len(pages) else []
        more = index + 1 < len(pages)
        return {"results": results, "has_more": more,
                "next_cursor": str(index + 1) if more else None}

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, (json or {}).get("start_cursor")))
        return _Response(self._page(self.search_pages, (json or {}).get("start_cursor")))

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, (params or {}).get("start_cursor")))
        if url.endswith("/users/me"):
            if self.me is None:
                return _Response({}, status_code=401)
            return _Response(self.me)
        return _Response(self._page(self.children_pages, (params or {}).get("start_cursor")))


BOT = {"object": "user", "id": "u1", "name": "inaiEra", "type": "bot",
       "bot": {"workspace_name": "Aadhya Analytics and Info Logics Pvt"}}

CFG = {"credentials": "secret_token"}


@pytest.fixture(autouse=True)
def _clean_notice():
    data_sources.LAST_NOTICE.pop("Notion", None)
    yield
    data_sources.LAST_NOTICE.pop("Notion", None)


def _patch(monkeypatch, client):
    monkeypatch.setattr(data_sources.httpx, "Client", lambda **kw: client)
    return client


class TestTheEmptyWorkspace:
    def test_it_names_the_integration_and_the_workspace(self, monkeypatch):
        """Both are needed to act on the advice: the Connections menu
        lists integrations by name, and someone with several workspaces
        has to know which one this token belongs to."""
        _patch(monkeypatch, _FakeNotion(me=BOT))
        entries = data_sources._notion_list(CFG, "")
        assert entries == []
        notice = data_sources.take_notice("Notion")
        assert "inaiEra" in notice
        assert "Aadhya Analytics and Info Logics Pvt" in notice
        assert "Connections" in notice

    def test_it_says_the_token_works_rather_than_implying_a_fault(self, monkeypatch):
        """The failure mode being fixed: an empty list reads as a broken
        connection, and people re-paste a token that was never wrong."""
        _patch(monkeypatch, _FakeNotion(me=BOT))
        data_sources._notion_list(CFG, "")
        assert "token works" in data_sources.take_notice("Notion")

    def test_the_identity_is_only_fetched_when_there_is_nothing_to_show(self, monkeypatch):
        client = _patch(monkeypatch, _FakeNotion(
            search_pages=[[{"id": "p1", "object": "page"}]], me=BOT))
        data_sources._notion_list(CFG, "")
        assert not any(url.endswith("/users/me") for _m, url, _c in client.calls)

    def test_an_unreadable_identity_still_gives_usable_advice(self, monkeypatch):
        """If users/me fails, the instruction is still worth giving —
        just without the name in it."""
        _patch(monkeypatch, _FakeNotion(me=None))
        data_sources._notion_list(CFG, "")
        notice = data_sources.take_notice("Notion")
        assert "Connections" in notice and "this integration" in notice


class TestPaging:
    def test_every_connected_page_is_listed_not_just_the_first_hundred(self, monkeypatch):
        _patch(monkeypatch, _FakeNotion(search_pages=[
            [{"id": f"p{i}", "object": "page"} for i in range(100)],
            [{"id": "p100", "object": "page"}],
        ]))
        entries = data_sources._notion_list(CFG, "")
        assert len(entries) == 101
        assert data_sources.take_notice("Notion") is None

    def test_a_workspace_beyond_the_cap_says_so(self, monkeypatch):
        """Bounded on purpose — someone is waiting for this listing —
        but a truncated list has to admit it rather than look complete."""
        page = [{"id": f"p{i}", "object": "page"} for i in range(100)]
        _patch(monkeypatch, _FakeNotion(search_pages=[page] * 8))
        entries = data_sources._notion_list(CFG, "")
        assert len(entries) == 100 * data_sources._NOTION_MAX_PAGES
        assert "has more" in data_sources.take_notice("Notion")

    def test_child_pages_page_too(self, monkeypatch):
        _patch(monkeypatch, _FakeNotion(children_pages=[
            [{"id": "c1", "type": "child_page", "child_page": {"title": "One"}}],
            [{"id": "c2", "type": "child_page", "child_page": {"title": "Two"}}],
        ]))
        entries = data_sources._notion_list(CFG, "parent-id")
        assert [e.name for e in entries] == ["One", "Two"]


class TestWhatIsListed:
    def test_pages_and_databases_are_distinguished(self, monkeypatch):
        _patch(monkeypatch, _FakeNotion(search_pages=[[
            {"id": "p1", "object": "page"},
            {"id": "d1", "object": "database"},
        ]]))
        notes = {e.id: e.note for e in data_sources._notion_list(CFG, "")}
        assert notes == {"p1": "page", "d1": "database"}

    def test_everything_is_browsable_and_selectable(self, monkeypatch):
        """A Notion page is both a container and a document, so it has
        to be openable AND tickable — unlike a folder anywhere else."""
        _patch(monkeypatch, _FakeNotion(search_pages=[[{"id": "p1", "object": "page"}]]))
        entry = data_sources._notion_list(CFG, "")[0]
        assert entry.is_dir is True and entry.path == "p1"
        assert entry.ingestible is True

    def test_blocks_that_are_not_child_pages_are_ignored(self, monkeypatch):
        """A paragraph is content of the page being browsed, not an item
        inside it; listing one as a file would offer a fragment."""
        _patch(monkeypatch, _FakeNotion(children_pages=[[
            {"id": "b1", "type": "paragraph"},
            {"id": "c1", "type": "child_page", "child_page": {"title": "Real page"}},
        ]]))
        entries = data_sources._notion_list(CFG, "parent-id")
        assert [e.name for e in entries] == ["Real page"]
