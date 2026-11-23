"""Tests for KB host aliasing and the refuse-to-store-a-doomed-secret
guard.

Both exist because of the same real, two-process setup: this project's
compose stack runs an api container alongside a Postgres reachable as
`db:5432` on that network, while a developer often also runs a backend
directly on the host, where the same database is `127.0.0.1:5435`.

1. Every knowledge base created through the container stores
   host: "db". Served by a host-side backend, all of them failed with
   "DNS resolution failed for 'db'" — and editing the rows only moves
   the breakage to the other caller.

2. A host-side backend started without SECRETS_ENCRYPTION_KEY exported
   generates a throwaway Fernet key per process. Secrets saved through
   it looked fine, showed as configured, and were unreadable forever
   after. That cost real credentials twice here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.kb_hosts import resolve_kb_host, alias_hint
from app.secrets_crypto import using_ephemeral_key, require_durable_key


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("KB_HOST_ALIASES", raising=False)
    yield


class TestHostAliases:
    def test_without_the_variable_nothing_is_rewritten(self):
        """The container, and every real deployment, must behave exactly
        as before — aliasing is opt-in, never inferred."""
        assert resolve_kb_host("db", 5432) == ("db", 5432)
        assert resolve_kb_host("10.0.0.5", 6543) == ("10.0.0.5", 6543)

    def test_an_alias_redirects_host_and_port(self, monkeypatch):
        monkeypatch.setenv("KB_HOST_ALIASES", "db=127.0.0.1:5435")
        assert resolve_kb_host("db", 5432) == ("127.0.0.1", 5435)

    def test_an_alias_without_a_port_keeps_the_stored_port(self, monkeypatch):
        monkeypatch.setenv("KB_HOST_ALIASES", "db=10.0.0.9")
        assert resolve_kb_host("db", 5432) == ("10.0.0.9", 5432)

    def test_only_the_named_host_is_affected(self, monkeypatch):
        monkeypatch.setenv("KB_HOST_ALIASES", "db=127.0.0.1:5435")
        assert resolve_kb_host("warehouse.internal", 5432) == ("warehouse.internal", 5432)

    def test_several_aliases_are_supported(self, monkeypatch):
        monkeypatch.setenv("KB_HOST_ALIASES", "db=127.0.0.1:5435,warehouse=10.0.0.4:6000")
        assert resolve_kb_host("db", 5432) == ("127.0.0.1", 5435)
        assert resolve_kb_host("warehouse", 5432) == ("10.0.0.4", 6000)

    @pytest.mark.parametrize("raw", ["garbage", "=x", "db=", ",,", "db"])
    def test_a_malformed_entry_is_skipped_not_raised(self, raw, monkeypatch):
        """This runs on the connection path — a typo in an operator
        convenience must not take every knowledge base offline."""
        monkeypatch.setenv("KB_HOST_ALIASES", raw)
        assert resolve_kb_host("db", 5432) == ("db", 5432)

    def test_a_non_numeric_stored_port_falls_back_rather_than_raising(self):
        assert resolve_kb_host("db", "not-a-port") == ("db", 5432)
        assert resolve_kb_host("db", None) == ("db", 5432)

    def test_the_hint_fires_only_for_a_bare_hostname(self):
        """A failure on an IP or a dotted domain means something else,
        and this advice would be a red herring."""
        assert "KB_HOST_ALIASES" in alias_hint("db")
        assert alias_hint("10.1.2.3") == ""
        assert alias_hint("warehouse.internal") == ""
        assert alias_hint("") == ""

    def test_the_hint_is_silent_once_aliases_are_configured(self, monkeypatch):
        monkeypatch.setenv("KB_HOST_ALIASES", "db=127.0.0.1:5435")
        assert alias_hint("db") == ""


class TestDurableKeyGuard:
    def test_an_unset_key_is_reported_as_ephemeral(self, monkeypatch):
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        assert using_ephemeral_key() is True

    def test_a_configured_key_is_durable(self, monkeypatch):
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "x" * 44)
        assert using_ephemeral_key() is False

    def test_storing_a_secret_is_refused_without_a_durable_key(self, monkeypatch):
        """Accepting the credential and reporting success, having
        encrypted it under a key that dies with this process, is worse
        than declining."""
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError) as excinfo:
            require_durable_key("the API key for Cohere")
        message = str(excinfo.value)
        assert "the API key for Cohere" in message
        assert "SECRETS_ENCRYPTION_KEY" in message
        # Must state plainly that nothing was written.
        assert "Nothing has been stored" in message

    def test_storing_is_allowed_with_a_durable_key(self, monkeypatch):
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "x" * 44)
        require_durable_key("anything")  # must not raise


class TestSharePointSubdomain:
    """The subdomain becomes a hostname, so an unusable value has to be
    caught at the field rather than surfacing later as an opaque Graph
    error about something else. A real configuration here had
    "aadhyaanalytics com" — with a space — which builds
    "aadhyaanalytics com.sharepoint.com" and can never resolve.
    """

    def test_a_bare_label_passes_through(self):
        from app.routers.integrations import _normalized_subdomain
        assert _normalized_subdomain("contoso") == "contoso"
        assert _normalized_subdomain("  contoso  ") == "contoso"
        assert _normalized_subdomain("netorgft20692442-my") == "netorgft20692442-my"

    def test_what_people_actually_paste_is_trimmed_to_the_label(self):
        from app.routers.integrations import _normalized_subdomain
        assert _normalized_subdomain("contoso.sharepoint.com") == "contoso"
        assert _normalized_subdomain("https://contoso.sharepoint.com") == "contoso"
        assert _normalized_subdomain("https://contoso.sharepoint.com/sites/team") == "contoso"

    def test_blank_is_absent_rather_than_an_error(self):
        from app.routers.integrations import _normalized_subdomain
        assert _normalized_subdomain("") is None
        assert _normalized_subdomain(None) is None
        assert _normalized_subdomain("   ") is None

    def test_a_value_that_cannot_be_a_hostname_is_refused(self):
        from fastapi import HTTPException
        from app.routers.integrations import _normalized_subdomain
        for bad in ("aadhyaanalytics com", "a.b", "has space", "under_score", "!!"):
            with pytest.raises(HTTPException) as excinfo:
                _normalized_subdomain(bad)
            assert excinfo.value.status_code == 400
            # The message has to show the shape it becomes, or the
            # rejection is just as mysterious as the failure was.
            assert "sharepoint.com" in excinfo.value.detail


class TestGraphAuthorizationExplanation:
    """A 401 with an spException on /sites/{hostname} means the app
    registration has no consented application permission — not that the
    client secret is wrong. Saying which is the whole point.
    """

    @staticmethod
    def _token(claims):
        import base64
        import json
        head = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"{head}.{body}.signature"

    def test_an_empty_roles_claim_is_reported_as_nothing_consented(self):
        from app.data_sources import _graph_authz_error
        token = self._token({"app_displayname": "My Connector", "tid": "contoso"})
        msg = str(_graph_authz_error("SharePoint", token, 401, "{}"))
        assert "NO application permissions" in msg
        # Must exonerate the credentials, which are demonstrably fine —
        # a token was issued.
        assert "fine" in msg
        assert "Sites.Read.All" in msg and "Files.Read.All" in msg

    def test_a_partial_grant_names_only_what_is_missing(self):
        from app.data_sources import _graph_authz_error
        token = self._token({"roles": ["Files.Read.All"], "app_displayname": "My Connector"})
        msg = str(_graph_authz_error("SharePoint", token, 403, "{}"))
        assert "Granted application permissions: Files.Read.All" in msg
        assert "Sites.Read.All" in msg
        assert "needs Files.Read.All" not in msg

    def test_delegated_permissions_are_called_out_as_inapplicable(self):
        from app.data_sources import _graph_authz_error
        token = self._token({"roles": []})
        msg = str(_graph_authz_error("OneDrive", token, 401, "{}"))
        assert "not delegated" in msg or "Delegated permissions do not apply" in msg

    def test_an_unparseable_token_still_produces_advice(self):
        from app.data_sources import _graph_authz_error, _jwt_claims
        assert _jwt_claims("not-a-jwt") == {}
        msg = str(_graph_authz_error("SharePoint", "not-a-jwt", 401, "{}"))
        assert "application permissions" in msg


class TestGraphPathEncoding:
    """OneDrive and SharePoint resolve to the same (driveId, itemId)
    addressing, because /drives/{id}/items/{id} reaches a personal
    OneDrive and a SharePoint document library identically.

    The bug this replaced: OneDrive was addressed as a SITE drive, which
    returned HTTP 200 with zero items — a team site's "Documents"
    library is not where anyone's personal files live, so empty and
    broken looked the same.
    """

    def test_the_root_listing_is_recognised(self):
        from app.data_sources import _graph_split
        assert _graph_split("") == ("root", None, None)

    def test_a_user_and_a_personal_site_both_resolve_to_a_drive_owner(self):
        from app.data_sources import _graph_split
        assert _graph_split("user:abc-123") == ("user", "abc-123", None)
        assert _graph_split("site:host,guid,guid") == ("site", "host,guid,guid", None)

    def test_a_drive_root_and_a_folder_inside_it_are_distinguished(self):
        from app.data_sources import _graph_split
        assert _graph_split("drive:D1") == ("drive", "D1", None)
        assert _graph_split("drive:D1|ITEM9") == ("drive", "D1", "ITEM9")

    def test_a_bare_id_is_tolerated_as_a_drive(self):
        """Older saved paths predate the prefixes."""
        from app.data_sources import _graph_split
        assert _graph_split("D1") == ("drive", "D1", None)

    def test_onedrive_no_longer_demands_directory_wide_access(self):
        """Personal OneDrives are enumerated as sites, so Sites.Read.All
        is enough — User.Read.All reads the entire directory and must not
        be presented as a requirement."""
        from app.data_sources import _GRAPH_REQUIRED_ROLES
        assert "User.Read.All" not in _GRAPH_REQUIRED_ROLES["OneDrive"]
        assert "Sites.Read.All" in _GRAPH_REQUIRED_ROLES["OneDrive"]


class TestIngestibleFilter:
    """The browser used an ALLOWLIST of text extensions, which was
    narrower than the pipeline itself: a repository of .sql files showed
    every file greyed out as unusable and a selected folder expanded to
    zero files — yet parse_file decodes any unrecognised extension as
    text and rejects real binaries itself, naming the file when it does.
    """

    def test_source_code_and_config_text_is_offered(self):
        from app.data_sources import _is_ingestible
        for name in ("codegen.flink.sql", "main.py", "app.ts", "Chart.yaml",
                     "Makefile.txt", "schema.graphql", "main.tf", "notes.org"):
            assert _is_ingestible(name), name

    def test_documents_the_extractors_handle_are_offered(self):
        from app.data_sources import _is_ingestible
        # Legacy Office binaries included: the container carries
        # antiword/catdoc/xls2csv/catppt.
        for name in ("a.pdf", "a.docx", "a.doc", "a.xls", "a.pptx", "a.epub", "a.msg"):
            assert _is_ingestible(name), name

    def test_formats_with_no_text_layer_are_flagged(self):
        from app.data_sources import _is_ingestible
        for name in ("photo.png", "clip.mp4", "audio.wav", "V9_New_Codebase.zip",
                     "lib.dylib", "font.woff2", "model.safetensors", "data.parquet"):
            assert not _is_ingestible(name), name

    def test_an_unknown_extension_is_offered_rather_than_hidden(self):
        """Erring towards offering: the pipeline's binary check is
        accurate and explains itself, while a hidden file explains
        nothing."""
        from app.data_sources import _is_ingestible
        assert _is_ingestible("weird.unknownext")
        assert _is_ingestible("Dockerfile")

    def test_os_metadata_and_secret_files_are_excluded_by_name(self):
        """A .env is plain text, so the extension denylist would offer
        it — and ingesting one copies live credentials into a vector
        store, from which retrieval would hand them back."""
        from app.data_sources import _is_ingestible
        for name in (".DS_Store", "Thumbs.db", "desktop.ini",
                     ".env", ".env.production", "credentials.json", "id_rsa", ".pgpass"):
            assert not _is_ingestible(name), name

    def test_a_similarly_named_but_harmless_file_is_still_offered(self):
        from app.data_sources import _is_ingestible
        assert _is_ingestible("my.env.example")
        assert _is_ingestible(".gitignore")
        assert _is_ingestible("environment-setup.md")
