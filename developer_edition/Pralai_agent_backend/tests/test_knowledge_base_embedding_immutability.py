"""
Real, end-to-end tests for per-KB immutable embedding configuration
(section 0.92, GAP_REPORT).

The real bug, exactly as reported: ingestion always called embed_texts
with whatever EMBEDDING_PROVIDER/VOYAGE_EMBED_MODEL was CURRENTLY
configured, and so did every later query (retrieval.py's own
embed_query call). The KB stored "embed_model": current_embed_model_
name() at ingestion time, but retrieval never read that stored value
back - so if the real, global config changed between two real
ingestion runs, or between ingestion and a later query, the resulting
vectors could genuinely diverge:

    Monday:   EMBEDDING_PROVIDER=voyage model=A, ingest KB
    Friday:   change model=B
              query same KB

producing a real dimension mismatch at best, or worse, dimension-
compatible but semantically incompatible vectors with silently poor
results and no error at all.

The fix: a KB's first real ingestion persists its own real, immutable
{"embedding_provider", "embedding_model", "embedding_dimension"} onto
its config; every subsequent real ingestion into that SAME KB reuses
that exact, stored configuration (never whatever the current global
env vars say by then); every real query-time embedding call
(handle_retrieval, the retrieval-test endpoint, chunk-content editing)
pins itself to that same stored configuration too. Changing a KB's
embedding configuration requires an explicit, deliberate
force_reindex=true, which truncates existing (now genuinely
incompatible) vectors and re-embeds fresh under the current config.

These tests exercise the real, running API and a real local Postgres
database directly - not mocked, except for the real Voyage HTTP call
itself (no real VOYAGE_API_KEY is available in this environment).
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine

client = TestClient(app)


def create_pgvector_kb(name_prefix: str = "Embed Immutability Test") -> dict:
    r = client.post("/api/integrations/knowledge-bases", json={
        "name": f"{name_prefix} {uuid.uuid4().hex[:8]}", "provider": "pgvector",
        "config": {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"},
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_kb(kb_id: str, table: str):
    client.delete(f"/api/integrations/knowledge-bases/{kb_id}")
    with Session(engine) as s:
        for schema in ("inaiera_kb", "public"):
            try:
                s.exec(text(f'DROP TABLE IF EXISTS {schema}."{table}"'))
                s.commit()
            except Exception:
                s.rollback()


def ingest_text(kb_id: str, table: str, content: str, force_reindex: bool = False) -> dict:
    import io
    r = client.post(
        f"/api/integrations/knowledge-bases/{kb_id}/ingest",
        files={"file": ("doc.txt", io.BytesIO(content.encode()), "text/plain")},
        data={"table": table, "force_reindex": "true" if force_reindex else "false"},
    )
    return r


class TestRealPerKbConfigPersistedAtIngestion:
    def test_a_kbs_first_ingestion_persists_the_real_structured_embedding_config(self):
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r = ingest_text(kb["id"], table, "a real test document about oceans and whales")
            assert r.status_code == 200, r.text

            kbs = client.get("/api/integrations/knowledge-bases").json()
            stored = next(k for k in kbs if k["id"] == kb["id"])
            c = stored["config"]
            assert c["embedding_provider"] == "hashing"
            assert c["embedding_model"] == "local-hashing-v1"
            assert c["embedding_dimension"] == 384
            # The pre-existing embed_model field stays, for backward compat.
            assert c["embed_model"] == "local-hashing-v1"
        finally:
            cleanup_kb(kb["id"], table)


class TestRealSubsequentIngestionReusesStoredConfigNotCurrentGlobal:
    """The exact real scenario from the report: a KB ingested under
    one real configuration must keep using THAT configuration for
    every later ingestion, even if the current global config would
    otherwise say something different by then."""

    def test_reingestion_uses_the_stored_provider_even_if_the_kb_config_claims_a_different_one(self):
        """Directly simulates the report's own "Friday: change model=B"
        scenario: this KB's own stored config is set to claim a
        DIFFERENT provider (voyage) than what actually ran at ingestion
        - re-ingesting must genuinely attempt THAT stored provider
        (and fail honestly, since there's no real API key here), not
        silently fall back to the current, working default."""
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r1 = ingest_text(kb["id"], table, "first real document about rivers")
            assert r1.status_code == 200, r1.text

            with Session(engine) as s:
                s.exec(text(
                    "UPDATE knowledgebase SET config = config || "
                    "'{\"embedding_provider\": \"voyage\", \"embedding_model\": \"voyage-3-lite\"}'::jsonb "
                    "WHERE id = :kb_id"
                ), params={"kb_id": kb["id"]})
                s.commit()

            r2 = ingest_text(kb["id"], table, "second real document about mountains")
            assert r2.status_code == 400
            assert "voyage" in r2.json()["detail"]
            assert "VOYAGE_API_KEY" in r2.json()["detail"]
        finally:
            cleanup_kb(kb["id"], table)

    def test_force_reindex_truncates_existing_rows_and_re_embeds_under_the_new_config(self):
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r1 = ingest_text(kb["id"], table, "the original real document about deserts")
            assert r1.status_code == 200, r1.text

            r2 = ingest_text(kb["id"], table, "a completely different real replacement document", force_reindex=True)
            assert r2.status_code == 200, r2.text

            with Session(engine) as s:
                rows = s.exec(text(f'SELECT content FROM inaiera_kb."{table}"')).all()
            assert len(rows) == 1
            assert "replacement" in rows[0][0]
            assert "deserts" not in rows[0][0]
        finally:
            cleanup_kb(kb["id"], table)


class TestRealQueryTimeEmbeddingPinnedToStoredConfig:
    """The other real half of the report's own request: 'generate
    query embeddings using that exact configuration' - these tests
    prove retrieval (not just ingestion) is genuinely pinned too."""

    def test_retrieval_test_endpoint_correctly_finds_real_results_using_the_stored_config(self):
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r = ingest_text(kb["id"], table, "a real document about penguins in Antarctica")
            assert r.status_code == 200, r.text

            rt = client.post(f"/api/integrations/knowledge-bases/{kb['id']}/retrieval-test",
                              json={"query": "penguins", "top_k": 3})
            assert rt.status_code == 200, rt.text
            results = rt.json()["results"]
            assert len(results) == 1
            assert "penguins" in results[0]["content"]
        finally:
            cleanup_kb(kb["id"], table)

    def test_a_query_against_a_kb_whose_stored_config_points_at_an_unavailable_provider_fails_honestly(self):
        """The exact real scenario the report describes for the QUERY
        side: if this KB's own stored config says its chunks were
        embedded with a provider that's no longer available, the
        query must honestly fail with a clear, actionable error - NOT
        silently compute the query vector with a DIFFERENT provider,
        which would compare it against real chunks in a meaningless,
        semantically-incompatible way with no error at all."""
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r = ingest_text(kb["id"], table, "a real document about volcanoes")
            assert r.status_code == 200, r.text

            with Session(engine) as s:
                s.exec(text(
                    "UPDATE knowledgebase SET config = config || "
                    "'{\"embedding_provider\": \"voyage\", \"embedding_model\": \"voyage-3-lite\"}'::jsonb "
                    "WHERE id = :kb_id"
                ), params={"kb_id": kb["id"]})
                s.commit()

            rt = client.post(f"/api/integrations/knowledge-bases/{kb['id']}/retrieval-test",
                              json={"query": "volcanoes", "top_k": 3})
            assert rt.status_code == 400
            assert "voyage" in rt.json()["detail"]
            assert "VOYAGE_API_KEY" in rt.json()["detail"]
        finally:
            cleanup_kb(kb["id"], table)

    def test_editing_a_chunks_content_recomputes_its_vector_using_the_stored_config_not_the_current_default(self):
        kb = create_pgvector_kb()
        table = f"embed_cfg_{uuid.uuid4().hex[:8]}"
        try:
            r = ingest_text(kb["id"], table, "a real document about coral reefs")
            assert r.status_code == 200, r.text

            with Session(engine) as s:
                s.exec(text(
                    "UPDATE knowledgebase SET config = config || "
                    "'{\"embedding_provider\": \"voyage\", \"embedding_model\": \"voyage-3-lite\"}'::jsonb "
                    "WHERE id = :kb_id"
                ), params={"kb_id": kb["id"]})
                s.commit()
                chunk_id = s.exec(text(f'SELECT id FROM inaiera_kb."{table}" LIMIT 1')).first()[0]

            patch_r = client.patch(
                f"/api/integrations/knowledge-bases/{kb['id']}/chunks/{chunk_id}",
                json={"content": "an edited real chunk about coral reefs"},
            )
            assert patch_r.status_code == 400
            assert "voyage" in str(patch_r.json())
        finally:
            cleanup_kb(kb["id"], table)
