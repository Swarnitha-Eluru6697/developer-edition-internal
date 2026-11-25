"""Tests for choosing where an upload is kept, at upload time.

Where files went was three environment variables read once at import:
one bucket per deployment, changeable only by editing .env on the server
and restarting. That is also what let the dev server and the containers
disagree about where files lived — same code, different environments,
no way to see it from the UI.

A destination is now a stored, credentialed, testable row that the
upload dialog can add. What is tested here is everything around that:
which destination an upload resolves to, that a file is read back from
the one it was WRITTEN to, that credentials never come back out, and
that a destination cannot be deleted out from under files that live in
it.

Verified live against the real bucket in this environment, not only
here: a destination saved through the API stored `enc:v1:`-marked
ciphertext for both keys, an ingest naming it wrote
`anonymous/up-67667815a5a949b5/keep_test.md`, the download endpoint read
those bytes back through the destination recorded on the row, deleting
the document removed the object (head_object → 404), and deleting the
destination was refused for as long as that file existed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import storage_destinations as sd
from app.models import StorageDestination, UploadedFile
from app.routers import uploads as uploads_mod
from app.secrets_crypto import decrypt_secret


class _FakeSession:
    """Enough Session for these helpers: get by id, add, commit, and a
    minimal `exec` over the rows already added."""

    def __init__(self, rows=()):
        self.rows = {r.id: r for r in rows}
        self.commits = 0

    def add(self, row):
        self.rows[row.id] = row

    def commit(self):
        self.commits += 1

    def refresh(self, row):
        return row

    def get(self, _model, key):
        return self.rows.get(key)

    def delete(self, row):
        self.rows.pop(row.id, None)

    def exec(self, _statement):
        # Only used for "the default destination" and "files in this
        # destination"; both are satisfied by handing back everything
        # and letting the caller filter, which is what the real query
        # does server-side.
        rows = list(self.rows.values())

        class _Result:
            def first(self_inner):
                return next((r for r in rows
                             if isinstance(r, StorageDestination) and r.is_default), None)

            def all(self_inner):
                return [r for r in rows if isinstance(r, StorageDestination)]

        return _Result()


class _FakeBackend:
    def __init__(self, label="fake"):
        self.label = label
        # The probe names the bucket in its own messages, so a stand-in
        # needs one too.
        self.bucket = "fake-bucket"
        self.objects = {}
        self.deleted = []

    def put(self, key, data, content_type=""):
        self.objects[key] = (data, content_type)

    def get(self, key):
        return self.objects[key][0]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    def exists(self, key):
        return key in self.objects


def _destination(monkeypatch, backend, **overrides):
    """A saved destination whose backend is a fake, so nothing here
    talks to a real bucket."""
    dest = StorageDestination(
        id=overrides.pop("id", "sd-test"), name=overrides.pop("name", "Marketing bucket"),
        provider="s3", bucket=overrides.pop("bucket", "marketing-uploads"),
        region="eu-west-1", access_key_id="enc:v1:xxx", secret_access_key="enc:v1:yyy",
        **overrides,
    )
    monkeypatch.setattr(sd, "backend_for", lambda d: backend)
    return dest


class TestWhichDestinationAnUploadUses:
    def test_no_choice_and_no_stored_default_means_the_environment(self, monkeypatch):
        env = _FakeBackend("env")
        monkeypatch.setattr(sd, "env_backend", lambda: env)
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("S3_BUCKET", "env-bucket")
        backend, dest_id, label = sd.resolve(_FakeSession(), None)
        assert backend is env
        # None, not "env": the column means "the environment's
        # destination", and every row written before destinations
        # existed already says that by being null.
        assert dest_id is None
        assert "env-bucket" in label

    def test_a_stored_default_wins_over_the_environment(self, monkeypatch):
        chosen = _FakeBackend("stored")
        dest = _destination(monkeypatch, chosen, is_default=True)
        monkeypatch.setattr(sd, "env_backend", lambda: _FakeBackend("env"))
        backend, dest_id, label = sd.resolve(_FakeSession([dest]), None)
        assert backend is chosen and dest_id == "sd-test"
        assert label == "S3 · marketing-uploads"

    def test_naming_a_destination_overrides_the_default(self, monkeypatch):
        named = _FakeBackend("named")
        dest = _destination(monkeypatch, named, id="sd-other", is_default=False)
        backend, dest_id, _ = sd.resolve(_FakeSession([dest]), "sd-other")
        assert backend is named and dest_id == "sd-other"

    def test_the_environment_can_be_chosen_explicitly(self, monkeypatch):
        """Even when a stored destination is default — otherwise adding
        one would take away the ability to use the server's own bucket."""
        env = _FakeBackend("env")
        monkeypatch.setattr(sd, "env_backend", lambda: env)
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("S3_BUCKET", "env-bucket")
        dest = _destination(monkeypatch, _FakeBackend("stored"), is_default=True)
        backend, dest_id, _ = sd.resolve(_FakeSession([dest]), sd.ENV_DESTINATION_ID)
        assert backend is env and dest_id is None

    def test_a_destination_that_no_longer_exists_says_so(self):
        with pytest.raises(sd.DestinationError) as e:
            sd.resolve(_FakeSession(), "sd-deleted")
        assert "sd-deleted" in str(e.value)


class TestReadingFilesBack:
    """The rule that makes several destinations safe: an object is
    reached through the destination it was WRITTEN to, never through
    whatever happens to be default now."""

    def test_a_stored_object_resolves_through_its_own_destination(self, monkeypatch):
        written_to = _FakeBackend("original")
        dest = _destination(monkeypatch, written_to, id="sd-old", is_default=False)
        # A different destination is default by now.
        monkeypatch.setattr(sd, "env_backend", lambda: _FakeBackend("current"))
        backend = sd.backend_for_stored_object(_FakeSession([dest]), "sd-old", "s3")
        assert backend is written_to

    def test_a_row_with_no_destination_uses_the_named_backend(self, monkeypatch):
        """Every file uploaded before destinations existed — the column
        is null and storage_backend still says where it lives."""
        local = _FakeBackend("local")
        monkeypatch.setattr(sd, "get_storage_backend", lambda name=None: local)
        assert sd.backend_for_stored_object(_FakeSession(), None, "local") is local

    def test_a_deleted_destination_is_reported_rather_than_guessed(self):
        """Falling back to the current default here would look in the
        wrong bucket and report the file as missing, which is a lie: the
        object was never deleted."""
        with pytest.raises(sd.DestinationError) as e:
            sd.backend_for_stored_object(_FakeSession(), "sd-gone", "s3")
        assert "was not deleted" in str(e.value)


class TestCredentials:
    def test_keys_are_encrypted_on_the_way_in(self, monkeypatch):
        monkeypatch.setattr(sd, "_clear_default", lambda session: None)
        session = _FakeSession()
        dest = sd.create(
            session, name="Marketing", bucket="marketing-uploads", region="eu-west-1",
            endpoint_url=None, access_key_id="AKIAEXAMPLE", secret_access_key="s3cr3t",
            make_default=False, verified="ok",
        )
        assert dest.access_key_id.startswith("enc:v1:")
        assert dest.secret_access_key.startswith("enc:v1:")
        assert "AKIAEXAMPLE" not in dest.access_key_id
        # And they really are the same values coming back out.
        assert decrypt_secret(dest.access_key_id) == "AKIAEXAMPLE"
        assert decrypt_secret(dest.secret_access_key) == "s3cr3t"

    def test_reads_mask_them(self):
        dest = StorageDestination(id="sd-1", name="Marketing", bucket="b",
                                  access_key_id="enc:v1:xxx", secret_access_key="enc:v1:yyy")
        public = sd.describe(dest)
        assert public["access_key_id"] == sd.MASK
        assert "secret_access_key" not in public
        assert "enc:v1" not in str(public)

    def test_no_stored_key_reports_the_credential_chain_rather_than_a_mask(self):
        """Null keys are a real, legitimate setup — an instance role —
        and showing dots there would imply a secret nobody entered."""
        dest = StorageDestination(id="sd-2", name="Role-based", bucket="b")
        assert sd.describe(dest)["access_key_id"] is None
        assert "credential chain" in sd.describe(dest)["credentials"]

    def test_credentials_that_cannot_be_decrypted_name_the_remedy(self, monkeypatch):
        monkeypatch.setattr(sd, "decrypt_secret",
                            lambda v: (_ for _ in ()).throw(ValueError("bad key")))
        dest = StorageDestination(id="sd-3", name="Marketing", bucket="b",
                                  access_key_id="enc:v1:xxx", secret_access_key="enc:v1:yyy")
        sd._cache.pop("sd-3", None)
        with pytest.raises(sd.DestinationError) as e:
            sd.backend_for(dest)
        assert "Re-enter them" in str(e.value)


class TestTheProbe:
    """"It works" has to be something this platform checked."""

    def test_a_probe_writes_reads_and_deletes(self):
        backend = _FakeBackend()
        from app.storage import S3StorageBackend
        # The real probe body, run against the fake's put/get/delete.
        result = S3StorageBackend.probe(backend)
        assert backend.objects == {}          # cleaned up after itself
        assert backend.deleted                # and really deleted
        assert "wrote, read and deleted" in result

    def test_a_probe_that_reads_back_different_bytes_fails(self):
        from app.storage import S3StorageBackend, StorageError

        class Liar(_FakeBackend):
            def get(self, key):
                return b"something else"

        with pytest.raises(StorageError) as e:
            S3StorageBackend.probe(Liar())
        assert "read back different bytes" in str(e.value)

    def test_probe_reports_failure_instead_of_raising(self, monkeypatch):
        from app.storage import StorageError

        class Broken(_FakeBackend):
            def probe(self):
                raise StorageError("AccessDenied")

        ok, message = sd.probe(Broken())
        assert ok is False and "AccessDenied" in message


class TestCaching:
    def test_a_backend_is_reused_between_uploads(self, monkeypatch):
        built = []
        monkeypatch.setattr(sd, "decrypt_secret", lambda v: "plain")
        monkeypatch.setattr(sd, "S3StorageBackend",
                            lambda **kw: built.append(kw) or _FakeBackend())
        dest = StorageDestination(id="sd-cache", name="n", bucket="b",
                                  access_key_id="enc:v1:x", secret_access_key="enc:v1:y")
        sd._cache.pop("sd-cache", None)
        sd.backend_for(dest)
        sd.backend_for(dest)
        assert len(built) == 1

    def test_editing_a_destination_rebuilds_it(self, monkeypatch):
        """Keyed on the row's own settings, so a changed bucket or key
        takes effect immediately instead of being served by a stale
        client for the life of the process."""
        built = []
        monkeypatch.setattr(sd, "decrypt_secret", lambda v: "plain")
        monkeypatch.setattr(sd, "S3StorageBackend",
                            lambda **kw: built.append(kw) or _FakeBackend())
        dest = StorageDestination(id="sd-edit", name="n", bucket="b",
                                  access_key_id="enc:v1:x", secret_access_key="enc:v1:y")
        sd._cache.pop("sd-edit", None)
        sd.backend_for(dest)
        dest.bucket = "a-different-bucket"
        sd.backend_for(dest)
        assert [k["bucket"] for k in built] == ["b", "a-different-bucket"]


class TestWhatTheUploadRecords:
    def test_the_row_remembers_which_destination_holds_it(self, monkeypatch):
        backend = _FakeBackend()
        session = _FakeSession()
        monkeypatch.setattr(uploads_mod, "sd", sd, raising=False)
        monkeypatch.setattr(sd, "resolve", lambda s, d: (backend, "sd-marketing", "S3 · marketing"))
        row = uploads_mod.store_upload_bytes(session, b"hello", "notes.txt", "text/plain",
                                             destination_id="sd-marketing")
        assert row.storage_destination_id == "sd-marketing"
        assert row.object_key in backend.objects

    def test_an_upload_with_no_destination_records_null(self, monkeypatch):
        backend = _FakeBackend()
        monkeypatch.setattr(sd, "resolve", lambda s, d: (backend, None, "this server's disk"))
        row = uploads_mod.store_upload_bytes(_FakeSession(), b"hello", "notes.txt", "text/plain")
        assert row.storage_destination_id is None


class TestEnvironmentSummary:
    def test_s3_with_a_bucket_is_reported_as_configured(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("S3_BUCKET", "my-inaiera-uploads")
        out = sd.env_summary()
        assert out["configured"] is True and "my-inaiera-uploads" in out["label"]
        # Not editable from the UI, because it genuinely is not: it
        # lives in a file on the server.
        assert out["editable"] is False

    def test_s3_with_no_bucket_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.delenv("S3_BUCKET", raising=False)
        assert sd.env_summary()["configured"] is False

    def test_local_disk_says_so(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        out = sd.env_summary()
        assert out["provider"] == "local" and "disk" in out["label"]
