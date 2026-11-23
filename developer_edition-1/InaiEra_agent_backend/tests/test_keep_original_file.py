"""Tests for keeping the uploaded file itself, not only its text.

Ingestion read a file, extracted its text and discarded the bytes — so
after uploading a PDF on the Documents tab, the only copy of that PDF
was still on the uploader's own machine. Meanwhile the Upload API tab's
"Stage on the platform" button had been putting files in S3 all along,
through routers/uploads.py. Same platform, same bucket available, two
different answers to "where did my file go".

Verified live against the real S3 bucket in this environment, not only
here: ingesting with keep_original=true wrote
`anonymous/up-d2f3570796554b50/keep_test.md` (129 bytes, text/plain),
which read back byte-identical through the platform's own download
endpoint, and deleting the document removed the object (a subsequent
head_object answered 404). What is tested below is the logic around
that: what gets stored, what the defaults are, and what happens when
storage refuses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.routers import knowledge_bases as kbm
from app.routers import uploads as uploads_mod
from app.routers.uploads import UploadRejected, store_upload_bytes


class _FakeSession:
    """Just enough Session for the helpers under test: they add a row,
    commit, and later look one up by id."""

    def __init__(self):
        self.rows = {}
        self.commits = 0
        self.deleted = []

    def add(self, row):
        self.rows[getattr(row, "id", id(row))] = row

    def commit(self):
        self.commits += 1

    def get(self, _model, key):
        return self.rows.get(key)

    def delete(self, row):
        self.deleted.append(row)
        self.rows.pop(getattr(row, "id", None), None)

    def exec(self, _statement):
        """Storing a file now asks which upload destination is default.
        Nothing here configures one, so this answers "none", which is
        the state these tests describe: the environment's destination."""
        class _Result:
            def first(self_inner):
                return None

            def all(self_inner):
                return []

        return _Result()


class _FakeBackend:
    def __init__(self, fail_with=None):
        self.objects = {}
        self.deleted = []
        self.fail_with = fail_with

    def put(self, key, data, content_type=""):
        if self.fail_with:
            raise self.fail_with
        self.objects[key] = (data, content_type)

    def delete(self, key):
        if self.fail_with:
            raise self.fail_with
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def backend(monkeypatch):
    """One fake standing in for EVERY lookup, because there are three
    and missing one sends a real request to the real bucket:

      uploads.py resolves the backend it writes through,
      knowledge_bases.py the one it deletes through, and
      storage_destinations.py the environment's own — which it imported
      by name at module load, so patching app.storage does not reach it.
    """
    fake = _FakeBackend()
    monkeypatch.setattr(uploads_mod, "get_storage_backend", lambda name=None: fake)
    monkeypatch.setattr("app.storage.get_storage_backend", lambda name=None: fake)
    monkeypatch.setattr("app.storage_destinations.get_storage_backend", lambda name=None: fake)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    return fake


class TestStoringTheFile:
    def test_the_bytes_reach_storage_under_a_tenant_prefixed_key(self, backend):
        session = _FakeSession()
        row = store_upload_bytes(session, b"# policy\nrefunds in 30 days", "policy.md",
                                 "text/markdown", owner="alice@example.com")
        assert row.object_key == f"alice@example.com/{row.id}/policy.md"
        stored, content_type = backend.objects[row.object_key]
        assert stored == b"# policy\nrefunds in 30 days"
        # The SNIFFED type, not the client's claim — the same rule the
        # staging endpoint follows, because it is the same function.
        assert content_type == "text/plain"
        assert row.storage_backend == "s3"

    def test_the_filename_is_sanitised_before_it_becomes_a_key(self, backend):
        session = _FakeSession()
        row = store_upload_bytes(session, b"data", "../../etc/passwd", "text/plain")
        assert ".." not in row.object_key
        assert row.filename == "passwd"

    def test_an_anonymous_upload_gets_its_own_namespace_not_a_shared_root(self, backend):
        session = _FakeSession()
        row = store_upload_bytes(session, b"data", "notes.txt", "text/plain")
        assert row.object_key.startswith("anonymous/")

    def test_malware_is_refused_on_this_path_too(self, backend):
        """The allowlist is relaxed for KB ingestion (see below), but the
        scan is not: that check is about the bytes, not the format."""
        session = _FakeSession()
        eicar = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")
        with pytest.raises(UploadRejected) as e:
            store_upload_bytes(session, eicar, "harmless.txt", "text/plain",
                               enforce_allowlist=False)
        assert e.value.status == 422
        assert backend.objects == {}

    def test_ingestion_may_keep_a_format_the_staging_allowlist_refuses(self, backend):
        """A format outside the allowlist must not be refused HERE: the
        file has already been parsed and indexed by the time this runs,
        and refusing to keep a copy of a document this platform just
        ingested would make no sense to whoever uploaded it.

        (Genuinely dangerous content is a different question and still
        refused on both paths — see the malware test above, which an
        ELF header trips.)"""
        session = _FakeSession()
        odd = bytes(range(120, 256)) * 4  # sniffs as nothing on the allowlist
        with pytest.raises(UploadRejected) as e:
            store_upload_bytes(session, odd, "reading.dat", "application/x-instrument-log")
        assert e.value.status == 415
        row = store_upload_bytes(session, odd, "reading.dat", "application/x-instrument-log",
                                 enforce_allowlist=False)
        assert row.object_key in backend.objects


class TestWhatIngestionReports:
    def test_a_storage_failure_reports_instead_of_failing_the_ingest(self, monkeypatch):
        """By the time this runs the chunks are written. Raising would
        mean an ingest reported as failed whose content is in the
        pipeline anyway — the worst of both."""
        from app.storage import StorageError
        failing = _FakeBackend(fail_with=StorageError("AccessDenied"))
        monkeypatch.setattr(uploads_mod, "get_storage_backend", lambda name=None: failing)
        monkeypatch.setattr("app.storage_destinations.get_storage_backend", lambda name=None: failing)
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        public, error = kbm._keep_original(_FakeSession(), None, b"data", "x.txt", "text/plain")
        assert public is None
        # Names the backend: "could not write to S3" and "could not
        # write to this server's disk" send someone to different places.
        assert "s3" in error and "AccessDenied" in error

    def test_a_refused_file_reports_its_reason(self, monkeypatch):
        clean = _FakeBackend()
        monkeypatch.setattr(uploads_mod, "get_storage_backend", lambda name=None: clean)
        monkeypatch.setattr("app.storage_destinations.get_storage_backend", lambda name=None: clean)
        eicar = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")
        public, error = kbm._keep_original(_FakeSession(), None, eicar, "x.txt", "text/plain")
        assert public is None and "malware" in error.lower()

    def test_what_is_reported_says_where_the_file_is(self, backend):
        public, error = kbm._keep_original(_FakeSession(), None, b"data", "x.txt", "text/plain")
        assert error is None
        # Backend and key both: "kept in S3" and "kept on this server's
        # disk" are different promises, and a key is what makes the
        # first one checkable.
        assert public["storage_backend"] == "s3"
        assert public["object_key"].endswith("/x.txt")
        assert public["url"].endswith(f"/api/uploads/{public['id']}/download")
        assert public["size"] == 4


class TestDeletingTheDocument:
    def test_deleting_a_document_removes_its_kept_file(self, backend):
        session = _FakeSession()
        row = store_upload_bytes(session, b"data", "x.txt", "text/plain")
        assert kbm._delete_original(session, row.id) is True
        assert backend.deleted == [row.object_key]
        assert session.get(None, row.id) is None

    def test_a_document_with_no_kept_file_has_nothing_to_remove(self, backend):
        assert kbm._delete_original(_FakeSession(), None) is None

    def test_a_storage_failure_still_lets_the_document_be_deleted(self, monkeypatch):
        """Otherwise a storage outage makes documents undeletable."""
        from app.storage import StorageError
        session = _FakeSession()
        good = _FakeBackend()
        monkeypatch.setattr(uploads_mod, "get_storage_backend", lambda name=None: good)
        monkeypatch.setattr("app.storage_destinations.get_storage_backend", lambda name=None: good)
        row = store_upload_bytes(session, b"data", "x.txt", "text/plain")
        broken = _FakeBackend(fail_with=StorageError("boom"))
        monkeypatch.setattr("app.storage.get_storage_backend", lambda name=None: broken)
        monkeypatch.setattr("app.storage_destinations.get_storage_backend", lambda name=None: broken)
        assert kbm._delete_original(session, row.id) is False
        # The row goes regardless — a pointer to an object that may or
        # may not still exist is worse than no pointer.
        assert session.get(None, row.id) is None


class TestTheOptionItself:
    def test_the_api_default_is_off_so_an_existing_caller_changes_nothing(self):
        """The Documents tab ticks the box by default; the API does not.
        An integration that has been POSTing files for months must not
        start filling a bucket — and paying for it — because this
        platform changed under it."""
        import inspect
        for fn in (kbm.ingest_document, kbm.api_upload_document):
            default = inspect.signature(fn).parameters["keep_original"].default
            # A FastAPI Form(...) default, so the value is on .default.
            assert getattr(default, "default", default) is False, fn.__name__

    def test_every_ingest_surface_accepts_it(self):
        """pgvector, Pinecone and the hosted stores all reach the same
        option — a pipeline on Qdrant keeps files the same way one on
        Postgres does."""
        import inspect
        for fn in (kbm._ingest_pinecone, kbm._ingest_vector_store,
                   kbm._ingest_selection_from_source):
            assert "keep_original" in inspect.signature(fn).parameters, fn.__name__
        for model in (kbm.SourceIngestIn, kbm.ApiSourceUploadIn):
            assert model.model_fields["keep_original"].default is False, model.__name__

    def test_a_document_reports_its_kept_file_to_the_listing(self, backend):
        from app.models import KnowledgeDocument
        session = _FakeSession()
        row = store_upload_bytes(session, b"data", "x.txt", "text/plain")
        doc = KnowledgeDocument(id="doc-1", kb_id="kb-1", filename="x.txt")
        doc.source_file_id = row.id
        public = kbm._doc_public(doc, {}, kbm._original_file_public(session, doc.source_file_id))
        assert public["original_file"]["object_key"] == row.object_key

    def test_a_document_with_nothing_kept_reports_null_not_a_broken_link(self, backend):
        from app.models import KnowledgeDocument
        doc = KnowledgeDocument(id="doc-2", kb_id="kb-1", filename="x.txt")
        assert kbm._doc_public(doc, {})["original_file"] is None
        # And a file that the retention sweep has already removed reads
        # as nothing kept, rather than as a link that 404s.
        assert kbm._original_file_public(_FakeSession(), "up-gone") is None


class TestStorageTargetEndpoint:
    """The checkbox names its destination, so this is what it reads."""

    def test_a_configured_bucket_is_named(self, monkeypatch):
        from app.routers.integrations import storage_target
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("S3_BUCKET", "my-inaiera-uploads")
        out = storage_target(_FakeSession())
        assert out["backend"] == "s3" and out["configured"] is True
        assert "my-inaiera-uploads" in out["label"]

    def test_s3_with_no_bucket_is_reported_as_not_configured(self, monkeypatch):
        """Otherwise the tickbox offers a destination that does not
        exist, and the first upload fails on a missing bucket."""
        from app.routers.integrations import storage_target
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.delenv("S3_BUCKET", raising=False)
        out = storage_target(_FakeSession())
        assert out["configured"] is False and out["bucket"] is None

    def test_local_disk_says_so_rather_than_implying_a_cloud(self, monkeypatch):
        from app.routers.integrations import storage_target
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        out = storage_target(_FakeSession())
        assert out["backend"] == "local" and out["configured"] is True
        assert "disk" in out["label"]
