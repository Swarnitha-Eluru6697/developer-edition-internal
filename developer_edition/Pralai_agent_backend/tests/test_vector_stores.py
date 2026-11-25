"""Tests for the Qdrant / Weaviate / Milvus adapters.

Ingestion was wired for pgvector and Pinecone only; a knowledge base on
any other provider was a catalog entry with a reachability check and no
way to put a document into it. These adapters add that, over each
provider's own REST API rather than three more SDKs.

The HTTP calls themselves were verified against real Qdrant 1.x and
Weaviate 1.27 containers — ingest, vector retrieval, and force-reindex
scoping. What is tested here is the logic around them: the addressing
rules, the guards, and the point shape, none of which needs a server.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import vector_stores
from app.vector_stores import VectorStoreError, Qdrant, Weaviate, Milvus


class TestConsoleUrlGuard:
    """Every one of these providers serves its console on a different
    host from its API, and pasting the console URL is the obvious
    mistake — it is the page you are on when you go to copy something.
    Unguarded, the failure is an HTML login page parsed as JSON.
    """

    @pytest.mark.parametrize("url,label", [
        ("https://cloud.qdrant.io/accounts/", "Qdrant"),
        ("https://console.weaviate.cloud/", "Weaviate"),
        ("https://cloud.zilliz.com/orgs", "Zilliz/Milvus"),
        ("https://app.pinecone.io/organizations/", "Pinecone"),
    ])
    def test_a_console_url_is_refused_by_name(self, url, label):
        with pytest.raises(VectorStoreError) as excinfo:
            vector_stores._base_url({"url": url}, "Qdrant")
        message = str(excinfo.value)
        assert label in message
        assert "web console" in message
        # Must show the shape of the real endpoint, or the rejection is
        # as unhelpful as the failure was.
        assert "looks like" in message

    def test_a_real_endpoint_passes_through(self):
        url = "https://abc-123.eu-central.aws.cloud.qdrant.io:6333"
        assert vector_stores._base_url({"url": url}, "Qdrant") == url

    def test_a_trailing_slash_is_trimmed(self):
        assert vector_stores._base_url({"url": "https://x.example/"}, "Qdrant") == "https://x.example"

    def test_a_scheme_is_assumed_when_omitted(self):
        assert vector_stores._base_url({"url": "x.example"}, "Qdrant") == "https://x.example"

    def test_a_missing_url_is_named_as_such(self):
        with pytest.raises(VectorStoreError) as excinfo:
            vector_stores._base_url({}, "Qdrant")
        assert "no URL configured" in str(excinfo.value)


class TestCollectionNaming:
    def test_the_configured_index_wins(self):
        assert vector_stores._collection({"index": "support"}, "kb-1") == "support"

    def test_the_kb_id_is_the_fallback(self):
        """A pipeline should work before anyone names a collection,
        rather than refusing."""
        assert vector_stores._collection({}, "support-docs") == "support_docs"

    def test_weaviate_class_names_are_made_graphql_safe(self):
        """Weaviate classes must start with an uppercase letter and
        carry no punctuation, so a kb id cannot be used verbatim."""
        assert Weaviate._class_name({"index": "support-docs"}, "x") == "Support_docs"
        assert Weaviate._class_name({"index": "9lives"}, "x") == "9lives"
        assert Weaviate._class_name({}, "kb-7") == "Kb_7"

    def test_an_unusable_name_falls_back_rather_than_producing_an_empty_class(self):
        assert Weaviate._class_name({"index": "---"}, "x") == "Documents"


class TestPointShape:
    def test_every_point_carries_the_kb_id(self):
        """A hosted collection routinely holds unrelated vectors, and
        kb_id is what keeps this platform's queries and its
        force-reindex delete from touching them. Verified against a real
        Qdrant: a neighbouring pipeline's 23 points survived the other's
        force-reindex."""
        points = vector_stores.build_points(
            "kb1", "doc-1", "a.md", ["one", "two"], [[0.1] * 3, [0.2] * 3])
        assert len(points) == 2
        for i, p in enumerate(points):
            assert p["payload"]["kb_id"] == "kb1"
            assert p["payload"]["document_id"] == "doc-1"
            assert p["payload"]["chunk_index"] == i
            assert p["payload"]["filename"] == "a.md"
        assert points[0]["payload"]["content"] == "one"

    def test_user_metadata_is_merged_but_cannot_overwrite_bookkeeping(self):
        """Otherwise a metadata field named kb_id would detach the chunk
        from its own pipeline."""
        points = vector_stores.build_points(
            "kb1", "doc-1", "a.md", ["one"], [[0.1]],
            user_meta={"tier": "premium", "kb_id": "somewhere-else"})
        assert points[0]["payload"]["tier"] == "premium"
        assert points[0]["payload"]["kb_id"] == "kb1"


class TestAdapterRegistry:
    def test_the_three_new_providers_resolve(self):
        for name, cls in (("qdrant", Qdrant), ("weaviate", Weaviate), ("milvus", Milvus)):
            assert vector_stores.adapter_for(name) is cls

    def test_a_provider_without_an_adapter_resolves_to_none(self):
        """pgvector and pinecone have their own dedicated paths, and
        'other' is a catalog entry with no storage semantics at all."""
        for name in ("pgvector", "pinecone", "other", "nonsense"):
            assert vector_stores.adapter_for(name) is None

    def test_every_adapter_implements_the_whole_interface(self):
        """The ingest path picks a module and calls these four, so a
        half-implemented adapter would fail at run time rather than
        here."""
        for cls in vector_stores.ADAPTERS.values():
            for op in ("ensure_collection", "upsert", "query", "delete_by_document"):
                assert callable(getattr(cls, op, None)), f"{cls.name}.{op}"


class TestSaveTimeValidation:
    """The console-URL check runs when the connection is SAVED, not only
    on first use. Discovering it at ingest time meant the message arrived
    once per file, in the middle of a batch, nowhere near the field that
    caused it.
    """

    def test_the_rule_has_one_definition(self):
        """_base_url delegates to check_endpoint_url, so the connection
        editor and the ingest path cannot disagree about what counts as
        a console URL."""
        with pytest.raises(VectorStoreError):
            vector_stores.check_endpoint_url("https://console.weaviate.cloud", "weaviate")
        with pytest.raises(VectorStoreError):
            vector_stores._base_url({"url": "https://console.weaviate.cloud"}, "Weaviate")

    def test_the_message_says_where_to_find_the_endpoint(self):
        """Naming the shape was not enough — someone still has to know
        which page of the provider's console to look on."""
        for url, expected in [
            ("https://console.weaviate.cloud", "REST Endpoint"),
            ("https://cloud.qdrant.io", "Clusters"),
            ("https://cloud.zilliz.com", "Public Endpoint"),
            ("https://app.pinecone.io", "Host"),
        ]:
            with pytest.raises(VectorStoreError) as excinfo:
                vector_stores.check_endpoint_url(url)
            assert expected in str(excinfo.value), url

    def test_a_real_endpoint_and_a_blank_both_pass(self):
        """Blank is not an error here: a pipeline can be created before
        its endpoint is known, and the ingest path reports the missing
        URL separately."""
        vector_stores.check_endpoint_url("https://abc.eu.aws.cloud.qdrant.io:6333")
        vector_stores.check_endpoint_url("")
        vector_stores.check_endpoint_url(None)  # type: ignore[arg-type]


class TestExternalApiSurfaces:
    """The Retrieval and Upload APIs were pgvector-only, and the UI hid
    the API tab for every other provider as a result. Hiding a control
    is not the same as the feature working: the endpoints now dispatch
    per provider, verified against a real Qdrant with real bearer keys
    (upload 23 chunks, retrieve top-3, 401 without a key).
    """

    def test_the_retrieval_test_helpers_can_label_their_records_row(self):
        """An external call is distinguishable from a builder test in
        the Records history, without altering the text that gets
        embedded — a tag inside the query would shift the very results
        the row describes."""
        import inspect
        from app.routers import knowledge_bases as kbm
        for fn in (kbm._retrieval_test_vector_store, kbm._retrieval_test_pinecone):
            params = inspect.signature(fn).parameters
            assert "record_prefix" in params, fn.__name__
            assert params["record_prefix"].default == "", fn.__name__

    def test_upload_is_offered_for_every_provider_with_an_adapter(self):
        """_ingest_supported is what the UI keys off, so it has to agree
        with the real dispatch rather than naming providers by hand."""
        from app.routers.knowledge_bases import _ingest_supported
        for provider in ("pgvector", "pinecone", "qdrant", "weaviate", "milvus"):
            assert _ingest_supported(provider) is True, provider
        for provider in ("other", "nonsense"):
            assert _ingest_supported(provider) is False, provider


class TestDeletingAHostedDocument:
    """Removing one document's chunks — what deleting a document and
    force-reindex both do. Both were broken on a real hosted store, and
    in two different ways, each caught by actually calling them:

      Qdrant answered `400 Index required but not found for "kb_id" of
      one of the following types: [keyword]`. Qdrant Cloud runs
      collections in strict mode, where filtering on an unindexed
      payload field is refused rather than merely slow — and every
      delete here filters on kb_id.

      Weaviate raised `Client.delete() got an unexpected keyword
      argument 'json'` before any request left the process: httpx's
      delete() takes no body, and Weaviate's batch-delete genuinely is
      a DELETE with one.
    """

    def _recorder(self, monkeypatch, get_status=200, body=None):
        """Captures the HTTP calls an adapter makes, without a server.

        `get_status` covers only the GET that asks whether a collection
        exists — 404 is how a brand-new one answers — while writes
        succeed, which is the shape every one of these flows actually
        meets.
        """
        calls = []

        class FakeResponse:
            text = ""
            content = b"{}"

            def __init__(self, status_code=200):
                self.status_code = status_code

            def json(self):
                return body or {}

        class FakeClient:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def _record(self_inner, method, url, status=200, **kwargs):
                calls.append((method, url, kwargs.get("json")))
                return FakeResponse(status)

            def get(self_inner, url, **kw):
                return self_inner._record("GET", url, status=get_status, **kw)

            def put(self_inner, url, **kw):
                return self_inner._record("PUT", url, **kw)

            def post(self_inner, url, **kw):
                return self_inner._record("POST", url, **kw)

            def request(self_inner, method, url, **kw):
                return self_inner._record(method, url, **kw)

            def delete(self_inner, url, **kw):
                raise TypeError("Client.delete() got an unexpected keyword argument 'json'")

        monkeypatch.setattr(vector_stores.httpx, "Client", lambda **kw: FakeClient())
        return calls

    def test_qdrant_indexes_the_fields_it_filters_on_before_deleting(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        Qdrant.delete_by_document({"url": "https://x.cloud.qdrant.io:6333", "index": "docs"}, "kb1", "doc-9")
        indexed = [c[2]["field_name"] for c in calls
                   if c[0] == "PUT" and c[1].endswith("/index?wait=true")]
        assert indexed == ["kb_id", "document_id"]
        assert all(c[2]["field_schema"] == "keyword" for c in calls
                   if c[0] == "PUT" and "/index" in c[1])

    def test_qdrant_scopes_the_delete_to_this_pipeline_and_document(self, monkeypatch):
        calls = self._recorder(monkeypatch)
        Qdrant.delete_by_document({"url": "https://x.cloud.qdrant.io:6333", "index": "docs"}, "kb1", "doc-9")
        deletes = [c for c in calls if c[0] == "POST" and "/points/delete" in c[1]]
        assert len(deletes) == 1
        must = deletes[0][2]["filter"]["must"]
        assert {"key": "kb_id", "match": {"value": "kb1"}} in must
        assert {"key": "document_id", "match": {"value": "doc-9"}} in must

    def test_a_force_reindex_still_never_deletes_another_pipelines_data(self, monkeypatch):
        """No document_id means "everything this platform wrote", which
        must stay scoped by kb_id — a shared collection is a real
        deployment, and an unscoped delete there is unrecoverable."""
        calls = self._recorder(monkeypatch)
        Qdrant.delete_by_document({"url": "https://x.cloud.qdrant.io:6333", "index": "shared"}, "kb1", None)
        must = [c for c in calls if "/points/delete" in c[1]][0][2]["filter"]["must"]
        assert must == [{"key": "kb_id", "match": {"value": "kb1"}}]

    def test_a_new_qdrant_collection_gets_those_indexes_at_creation(self, monkeypatch):
        calls = self._recorder(monkeypatch, get_status=404)
        Qdrant.ensure_collection({"url": "https://x.cloud.qdrant.io:6333", "index": "fresh"}, "kb1", 384)
        assert [c[2]["field_name"] for c in calls if "/index" in c[1]] == ["kb_id", "document_id"]

    def test_weaviate_sends_a_delete_with_a_body(self, monkeypatch):
        """The FakeClient's own delete() raises the exact TypeError the
        real httpx raised, so this test fails if the adapter ever goes
        back to calling it."""
        calls = self._recorder(monkeypatch)
        Weaviate.delete_by_document({"url": "https://x.weaviate.cloud", "index": "Docs"}, "kb1", "doc-9")
        assert len(calls) == 1
        method, url, body = calls[0]
        assert method == "DELETE" and url.endswith("/v1/batch/objects")
        operands = body["match"]["where"]["operands"]
        assert {"path": ["kb_id"], "operator": "Equal", "valueText": "kb1"} in operands
        assert {"path": ["document_id"], "operator": "Equal", "valueText": "doc-9"} in operands
