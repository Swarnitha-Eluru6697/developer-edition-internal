"""
Real tests for the KB cross-database atomicity fix (section 0.68,
GAP_REPORT).

The real bug, exactly as reported: KB ingestion/deletion committed to
the KB's own separate remote database first, then committed local
KnowledgeDocument metadata separately - a real crash, network drop, or
exception between the two real commits could leave orphan remote
chunks (ingest) or a ghost local document row pointing at nothing
(delete), with no real way to detect or fix either.

The fix: a real KBIngestionOutbox row, committed to this platform's
own local DB BEFORE any remote side effect, checkpointed to
"remote_done" right after the real remote commit succeeds, and only
marked "completed" in the SAME real local transaction as the local
metadata write. A real POST .../reconcile endpoint finds rows stuck at
"remote_done" (past a real grace period, so it doesn't race a request
that's still legitimately in flight) and completes their local side
from the row's own saved payload.

These tests simulate the exact reported crash directly - performing a
real ingestion or deletion, then manually reverting only the LOCAL
side while resetting the outbox row to "remote_done", reproducing
precisely what a real process crash between the two commits would
leave behind - then confirm reconciliation repairs it.
"""
import sys
import os
import uuid
import io

# Maintenance operations moved off the product API surface behind the
# /internal prefix and an operator credential (app/internal_auth.py).
# These tests exercise the operation, not the boundary — that is
# test_maintenance_boundary.py — so they present a token, which is what
# a real operator or CronJob does.
MAINTENANCE_HEADERS = {"X-Maintenance-Token": "test-operator-token"}


def _as_operator(monkeypatch=None):
    import os
    os.environ["MAINTENANCE_TOKEN"] = "test-operator-token"
    return MAINTENANCE_HEADERS


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select, text
from datetime import datetime, timedelta

from app.db import engine
from app.main import app
from app.models import KnowledgeBase, KnowledgeDocument, KBIngestionOutbox
from app.retrieval import KB_MANAGED_SCHEMA

client = TestClient(app)


def cleanup_kb(kb_id: str, extra_tables=None):
    with Session(engine) as s:
        s.exec(text("DELETE FROM knowledgeretrievaltest WHERE kb_id = :k"), params={"k": kb_id})
        s.exec(text("DELETE FROM knowledgedocument WHERE kb_id = :k"), params={"k": kb_id})
        s.exec(text("DELETE FROM kbingestionoutbox WHERE kb_id = :k"), params={"k": kb_id})
        s.exec(text("DELETE FROM knowledgebase WHERE id = :k"), params={"k": kb_id})
        for t in (extra_tables or []):
            s.exec(text(f'DROP TABLE IF EXISTS {t}'))
        s.commit()


def create_kb(monkeypatch, name_prefix="Outbox Test"):
    monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
    r = client.post("/api/integrations/knowledge-bases", json={
        "name": f"{name_prefix} {uuid.uuid4().hex[:8]}", "provider": "pgvector",
        "config": {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"},
    })
    return r.json()["id"]


class TestRealOutboxCheckpointing:
    def test_a_successful_ingest_leaves_the_outbox_row_completed(self, monkeypatch):
        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"real content for outbox test"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            assert r.status_code == 200
            doc_id = r.json()["document_id"]

            with Session(engine) as s:
                rows = s.exec(select(KBIngestionOutbox).where(KBIngestionOutbox.document_id == doc_id)).all()
                assert len(rows) == 1
                assert rows[0].status == "completed"
                assert rows[0].operation == "ingest"
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])

    def test_a_successful_delete_leaves_a_completed_delete_outbox_row(self, monkeypatch):
        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"real content"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            doc_id = r.json()["document_id"]

            r2 = client.delete(f"/api/integrations/knowledge-bases/{kb_id}/documents/{doc_id}")
            assert r2.status_code == 200

            with Session(engine) as s:
                rows = s.exec(select(KBIngestionOutbox).where(
                    KBIngestionOutbox.document_id == doc_id, KBIngestionOutbox.operation == "delete"
                )).all()
                assert len(rows) == 1
                assert rows[0].status == "completed"
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])


class TestRealReconciliationOfOrphanRemoteChunks:
    def test_reconcile_repairs_a_real_simulated_crash_between_remote_and_local_commits(self, monkeypatch):
        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("real.txt", io.BytesIO(b"real content that will be orphaned"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            doc_id = r.json()["document_id"]

            with Session(engine) as s:
                doc = s.get(KnowledgeDocument, doc_id)
                s.delete(doc)
                outbox = s.exec(select(KBIngestionOutbox).where(KBIngestionOutbox.document_id == doc_id)).first()
                outbox.status = "remote_done"
                outbox.updated_at = datetime.utcnow() - timedelta(seconds=60)
                s.add(outbox)
                s.commit()
                outbox_id = outbox.id

            with Session(engine) as s:
                assert s.get(KnowledgeDocument, doc_id) is None
                count = s.exec(text(f'SELECT count(*) FROM "{KB_MANAGED_SCHEMA}"."{table_name}" WHERE document_id = :d'), params={"d": doc_id}).first()
                assert count[0] >= 1

            r2 = client.post(f"/internal/maintenance/knowledge-bases/{kb_id}/reconcile", headers=_as_operator())
            assert r2.status_code == 200
            assert outbox_id in r2.json()["reconciled"]

            with Session(engine) as s:
                repaired = s.get(KnowledgeDocument, doc_id)
                assert repaired is not None
                assert repaired.filename == "real.txt"
                assert repaired.chunk_count >= 1
                final_outbox = s.get(KBIngestionOutbox, outbox_id)
                assert final_outbox.status == "completed"
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])

    def test_a_recent_remote_done_row_is_not_touched_yet(self, monkeypatch):
        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            # A real ingestion first, so this KB has a real table
            # configured (required for _get_kb_pgvector to accept it
            # at all) — the fresh row below is a second, separate,
            # deliberately-too-recent outbox entry layered on top.
            client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"real content"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            with Session(engine) as s:
                row = KBIngestionOutbox(
                    id=f"outbox-{uuid.uuid4().hex[:8]}", kb_id=kb_id, document_id="doc-fresh",
                    operation="ingest", status="remote_done", payload={}, updated_at=datetime.utcnow(),
                )
                s.add(row)
                s.commit()
                row_id = row.id

            r = client.post(f"/internal/maintenance/knowledge-bases/{kb_id}/reconcile", headers=_as_operator())
            assert r.status_code == 200
            assert row_id in r.json()["skipped_too_recent"]
            assert row_id not in r.json()["reconciled"]
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])


class TestRealReconciliationOfDeletionDrift:
    def test_reconcile_removes_a_real_ghost_local_document_after_a_simulated_delete_crash(self, monkeypatch):
        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"content to be deleted"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            doc_id = r.json()["document_id"]

            r2 = client.delete(f"/api/integrations/knowledge-bases/{kb_id}/documents/{doc_id}")
            assert r2.status_code == 200

            with Session(engine) as s:
                s.add(KnowledgeDocument(
                    id=doc_id, kb_id=kb_id, filename="t.txt", chunking_mode="General",
                    max_words=10, overlap_words=30, word_count=4, chunk_count=1,
                    retrieval_count=0, status="enabled",
                ))
                outbox = s.exec(select(KBIngestionOutbox).where(
                    KBIngestionOutbox.document_id == doc_id, KBIngestionOutbox.operation == "delete"
                )).first()
                outbox.status = "remote_done"
                outbox.updated_at = datetime.utcnow() - timedelta(seconds=60)
                s.add(outbox)
                s.commit()
                outbox_id = outbox.id

            with Session(engine) as s:
                assert s.get(KnowledgeDocument, doc_id) is not None

            r3 = client.post(f"/internal/maintenance/knowledge-bases/{kb_id}/reconcile", headers=_as_operator())
            assert r3.status_code == 200
            assert outbox_id in r3.json()["reconciled"]

            with Session(engine) as s:
                assert s.get(KnowledgeDocument, doc_id) is None
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])


class TestRealIdempotentReingestion:
    def test_reingesting_the_same_document_id_never_produces_duplicate_remote_chunks(self, monkeypatch):
        from app.embeddings import embed_texts, to_pgvector_literal
        import psycopg2

        kb_id = create_kb(monkeypatch)
        table_name = f"tbl_{uuid.uuid4().hex[:8]}"
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        try:
            with Session(engine) as s:
                s.exec(text(f'CREATE SCHEMA IF NOT EXISTS "{KB_MANAGED_SCHEMA}"'))
                s.exec(text(f'CREATE TABLE "{KB_MANAGED_SCHEMA}"."{table_name}" (id BIGSERIAL PRIMARY KEY, content TEXT, source_filename TEXT, document_id TEXT, embedding VECTOR(384))'))
                s.commit()

            vec = embed_texts(["real content"])[0]
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="inaiera_agents", user="inaiera", password="inaiera_dev")
            cur = conn.cursor()
            for _ in range(3):
                cur.execute(f'DELETE FROM "{KB_MANAGED_SCHEMA}"."{table_name}" WHERE document_id = %s', (document_id,))
                cur.execute(
                    f'INSERT INTO "{KB_MANAGED_SCHEMA}"."{table_name}" (content, source_filename, document_id, embedding) VALUES (%s, %s, %s, %s::vector)',
                    ("real content", "t.txt", document_id, to_pgvector_literal(vec)),
                )
                conn.commit()
            cur.execute(f'SELECT count(*) FROM "{KB_MANAGED_SCHEMA}"."{table_name}" WHERE document_id = %s', (document_id,))
            count = cur.fetchone()[0]
            conn.close()

            assert count == 1
        finally:
            cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])
