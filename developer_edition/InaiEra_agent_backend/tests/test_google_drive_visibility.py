"""Tests for what a Google Drive source can actually see.

The reported symptom: a Google Drive source configured with a valid
service-account key browsed as completely empty, while its owner could
see plenty of files in their browser. Nothing was broken — the source
authenticates as the SERVICE ACCOUNT, a separate identity with its own,
empty Drive, and the listing only ever asked for `'root' in parents`.
Files shared with that identity are not children of its root, so the one
route a personal Google account actually has was invisible.

Confirmed live against the real key in this environment before the fix:
`'root' in parents` → 0 items, `sharedWithMe` → 0 items, `drives.list`
→ 0 drives, all HTTP 200. So the credentials were fine and the account
genuinely had access to nothing, which is exactly what the listing now
says, naming the address to share with.

The three queries are covered here with a fake transport, because
proving the shared-with-me path needs a folder shared with that service
account and nothing in this environment can create one.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import data_sources

SA_KEY = json.dumps({
    "type": "service_account",
    "client_email": "inaiera-drive-connector@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-used-by-these-tests\n-----END PRIVATE KEY-----\n",
})


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Answers the three Drive calls the listing makes, and records the
    queries so a test can assert WHICH question was asked."""

    def __init__(self, *, own=(), shared=(), drives=(), children=()):
        self.own, self.shared, self.drives, self.children = own, shared, drives, children
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        params = params or {}
        if url.endswith("/drives"):
            self.queries.append("drives.list")
            return _Response({"drives": list(self.drives)})
        q = params.get("q", "")
        self.queries.append(q)
        if "sharedWithMe" in q:
            return _Response({"files": list(self.shared)})
        if "'root' in parents" in q:
            return _Response({"files": list(self.own)})
        return _Response({"files": list(self.children)})


@pytest.fixture(autouse=True)
def _no_real_token(monkeypatch):
    """Signing a JWT and exchanging it with Google is a separate
    concern, already exercised live; these tests are about the listing."""
    monkeypatch.setattr(data_sources, "_google_token", lambda cfg, scope: "fake-token")
    data_sources.LAST_NOTICE.pop("Google Drive", None)
    yield
    data_sources.LAST_NOTICE.pop("Google Drive", None)


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(data_sources.httpx, "Client", lambda **kw: client)
    return client


class TestWhatTheRootListingAsksFor:
    def test_it_asks_all_three_questions_not_just_the_root(self, monkeypatch):
        """The bug in one assertion: only the third question was ever
        asked, and it is the one a service account always answers with
        nothing."""
        client = _patch_client(monkeypatch, _FakeClient())
        data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert "drives.list" in client.queries
        assert any("sharedWithMe" in q for q in client.queries)
        assert any("'root' in parents" in q for q in client.queries)

    def test_a_folder_shared_with_the_service_account_is_listed(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(shared=[
            {"id": "f1", "name": "Board minutes",
             "mimeType": "application/vnd.google-apps.folder",
             "owners": [{"emailAddress": "someone@example.com"}]},
        ]))
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert [(e.name, e.is_dir) for e in entries] == [("Board minutes", True)]
        # A folder is descended into by its id.
        assert entries[0].path == "f1"
        # And it says where it came from: Google keeps "Shared with me"
        # separate from My Drive, and with impersonation both are listed
        # here together.
        assert entries[0].note == "shared with this account"

    def test_a_shared_drive_is_listed_as_a_folder(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(drives=[{"id": "d1", "name": "Marketing"}]))
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert entries[0].name == "Marketing"
        assert entries[0].is_dir and entries[0].path == "d1"
        assert entries[0].note == "shared drive"

    def test_all_three_sources_appear_together_without_duplicates(self, monkeypatch):
        """A file can be both shared with the account and in its own
        root; listing it twice would be a confusing way to be correct."""
        both = {"id": "same", "name": "Policy.pdf", "mimeType": "application/pdf"}
        _patch_client(monkeypatch, _FakeClient(
            drives=[{"id": "d1", "name": "Marketing"}],
            shared=[both, {"id": "s2", "name": "Notes.md", "mimeType": "text/markdown"}],
            own=[both],
        ))
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert [e.id for e in entries].count("same") == 1
        assert {e.name for e in entries} == {"Marketing", "Policy.pdf", "Notes.md"}
        # Folders first, then files, each alphabetically.
        assert entries[0].name == "Marketing"

    def test_descending_into_a_folder_asks_only_for_its_children(self, monkeypatch):
        client = _patch_client(monkeypatch, _FakeClient(children=[
            {"id": "c1", "name": "Q3.pdf", "mimeType": "application/pdf", "size": "1024"},
        ]))
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "f1")
        assert [e.name for e in entries] == ["Q3.pdf"]
        assert entries[0].size == 1024
        # No shared-drive or shared-with-me query inside a folder.
        assert client.queries == ["'f1' in parents and trashed=false"]

    def test_a_shared_drive_is_browsed_by_its_own_id(self, monkeypatch):
        """A shared drive's id IS its root folder id, so descending into
        one needs no special case."""
        client = _patch_client(monkeypatch, _FakeClient(children=[
            {"id": "c9", "name": "Brand.docx",
             "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ]))
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "d1")
        assert [e.name for e in entries] == ["Brand.docx"]
        assert client.queries == ["'d1' in parents and trashed=false"]


class TestTheEmptyCase:
    def test_an_empty_drive_explains_itself_and_names_the_address(self, monkeypatch):
        """The actual fix for the reported problem. An empty list with no
        explanation reads as "this integration is broken"; the truth is
        that nothing has been shared with this identity yet, and the
        address to share with was only ever inside the masked key."""
        _patch_client(monkeypatch, _FakeClient())
        entries = data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert entries == []
        notice = data_sources.take_notice("Google Drive")
        assert "inaiera-drive-connector@example.iam.gserviceaccount.com" in notice
        assert "Share" in notice
        # And it points at the Workspace alternative rather than
        # implying sharing is the only way.
        assert "impersonate_subject" in notice

    def test_an_empty_result_while_impersonating_says_something_different(self, monkeypatch):
        """Domain-wide delegation is a different configuration with a
        different remedy — telling someone to share a folder with the
        service account would be wrong advice here."""
        _patch_client(monkeypatch, _FakeClient())
        data_sources._gdrive_list(
            {"credentials": SA_KEY, "impersonate_subject": "ceo@company.com"}, "")
        notice = data_sources.take_notice("Google Drive")
        assert "ceo@company.com" in notice
        assert "delegation" in notice.lower()

    def test_no_notice_when_the_listing_speaks_for_itself(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(shared=[
            {"id": "f1", "name": "Docs", "mimeType": "application/vnd.google-apps.folder"},
        ]))
        data_sources._gdrive_list({"credentials": SA_KEY}, "")
        assert data_sources.take_notice("Google Drive") is None


class TestFileKinds:
    def test_a_google_doc_is_offered_and_marked_as_exported(self, monkeypatch):
        """Native Google formats have no bytes to download; they are
        exported at fetch time, which is why they are ingestible despite
        having no file extension to judge."""
        _patch_client(monkeypatch, _FakeClient(shared=[
            {"id": "g1", "name": "Strategy", "mimeType": "application/vnd.google-apps.document"},
        ]))
        entry = data_sources._gdrive_list({"credentials": SA_KEY}, "")[0]
        assert entry.ingestible is True and entry.note == "exported as text"
        assert entry.is_dir is False

    def test_an_image_is_listed_but_not_offered(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(shared=[
            {"id": "i1", "name": "logo.png", "mimeType": "image/png"},
        ]))
        entry = data_sources._gdrive_list({"credentials": SA_KEY}, "")[0]
        assert entry.ingestible is False
        assert "no text to extract" in entry.note


class TestTheAddressIsReachable:
    """The address has to reach the UI, or the advice in the notice is
    something nobody can act on: the key it lives in comes back masked."""

    def test_the_service_account_email_is_read_from_a_stored_key(self):
        from app.models import DataSource
        from app.routers.integrations import _service_account_email
        ds = DataSource(name="Google Drive", version="1", status="connected", auth="oauth",
                        config={"credentials": SA_KEY})
        assert _service_account_email(ds) == "inaiera-drive-connector@example.iam.gserviceaccount.com"

    def test_a_non_json_credential_is_not_an_error(self):
        """Most sources store a token, not a key — GitHub and Notion go
        through this same serializer."""
        from app.models import DataSource
        from app.routers.integrations import _service_account_email
        ds = DataSource(name="GitHub", version="1", status="connected", auth="apikey",
                        config={"credentials": "ghp_sometoken"})
        assert _service_account_email(ds) is None

    def test_the_oauth_client_id_is_exposed_for_delegation(self):
        """Workspace domains block sharing with an outside address, and
        a service account is always outside the domain — so delegation
        is often the only route, and its Admin console screen asks for
        the numeric client id, not the email."""
        from app.models import DataSource
        from app.routers.integrations import _ds_masked
        key = json.loads(SA_KEY)
        key["client_id"] = "109519407387034599118"
        ds = DataSource(name="Google Drive", version="1", status="connected", auth="oauth",
                        config={"credentials": json.dumps(key)})
        assert _ds_masked(ds)["service_account_client_id"] == "109519407387034599118"

    def test_a_delegation_failure_names_the_real_cause(self, monkeypatch):
        """Google answers `invalid_grant: Invalid email or User ID`,
        which sends people to check an address that is almost always
        correct. The real cause is that delegation was never authorised
        for this client id."""
        class _TokenResponse:
            status_code = 400
            content = b"x"
            text = '{"error": "invalid_grant", "error_description": "Invalid email or User ID"}'

        class _TokenClient:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None):
                return _TokenResponse()

        monkeypatch.setattr(data_sources.httpx, "Client", lambda **kw: _TokenClient())
        key = json.loads(SA_KEY)
        key["client_id"] = "109519407387034599118"
        # A real RSA key, because the JWT is genuinely signed before the
        # exchange is attempted.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        key["private_key"] = pem
        monkeypatch.undo()  # the autouse fixture stubs _google_token; this test needs the real one
        monkeypatch.setattr(data_sources.httpx, "Client", lambda **kw: _TokenClient())
        with pytest.raises(data_sources.DataSourceError) as e:
            data_sources._google_token(
                {"credentials": json.dumps(key), "impersonate_subject": "user@company.com"},
                "https://www.googleapis.com/auth/drive.readonly")
        message = str(e.value)
        assert "Domain-wide delegation is not authorised" in message
        assert "109519407387034599118" in message
        assert "drive.readonly" in message
        # Google's own words are kept, rather than hidden.
        assert "Invalid email or User ID" in message

    def test_the_masked_read_carries_the_address_but_not_the_key(self):
        from app.models import DataSource
        from app.routers.integrations import _ds_masked
        ds = DataSource(name="Google Drive", version="1", status="connected", auth="oauth",
                        config={"credentials": SA_KEY})
        public = _ds_masked(ds)
        assert public["service_account_email"].endswith(".iam.gserviceaccount.com")
        assert public["config"]["credentials"] != SA_KEY
        assert "private_key" not in json.dumps(public)
