"""
Real tests for the KB schema isolation fix (section 0.67, GAP_REPORT).

The real bug, exactly as reported: ingest_document's own `table` Form
parameter (an operator-supplied string) flowed straight into
sql.Identifier(target_table) with no schema qualification anywhere -
an operator naming the table "users" would genuinely run CREATE TABLE
IF NOT EXISTS + ALTER TABLE ADD COLUMN against THEIR OWN real
"public.users" (or whatever table already existed under that name),
not a separate object.

The fix: every KB table this service creates now lives inside one,
dedicated schema (KB_MANAGED_SCHEMA = "inaiera_kb") - CREATE SCHEMA IF
NOT EXISTS, never CREATE/ALTER anything outside it. A real, deliberate
security decision made while building this: kb.config itself is
caller-supplied at KB creation time (POST /knowledge-bases accepts a
real, arbitrary config object), so kb_schema_of() never trusts an
arbitrary config["schema"] string - only an EXACT match against the
one hardcoded constant is ever used for a real DDL/query target.

The single most direct, load-bearing test in this file is
test_ingesting_into_a_colliding_table_name_never_touches_the_real_pre_existing_table
- it reproduces the exact reported attack scenario against a real
database.
"""
import sys
import os
import uuid
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app
from app.retrieval import KB_MANAGED_SCHEMA, kb_schema_of
from app.routers.knowledge_bases import _kb_table_identifier
from app.models import KnowledgeBase

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


class TestRealSchemaTrustBoundary:
    def test_the_exact_managed_constant_is_trusted(self):
        assert kb_schema_of({"schema": KB_MANAGED_SCHEMA}) == KB_MANAGED_SCHEMA

    def test_an_arbitrary_caller_supplied_schema_string_is_never_trusted(self):
        assert kb_schema_of({"schema": "public"}) is None
        assert kb_schema_of({"schema": "pg_catalog"}) is None
        assert kb_schema_of({"schema": "some_other_schema"}) is None
        assert kb_schema_of({"schema": "inaiera_kb; DROP SCHEMA public CASCADE;"}) is None

    def test_no_schema_key_at_all_is_the_real_pre_fix_default(self):
        assert kb_schema_of({}) is None
        assert kb_schema_of({"table": "mytable"}) is None


class TestRealTableIdentifierConstruction:
    def test_schema_qualified_when_managed_schema_is_genuinely_set(self):
        kb = KnowledgeBase(id="x", name="x", provider="pgvector", config={"table": "mytable", "schema": KB_MANAGED_SCHEMA})
        ident = _kb_table_identifier(kb)
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="inaiera_agents", user="inaiera", password="inaiera_dev")
        cur = conn.cursor()
        assert ident.as_string(cur) == '"inaiera_kb"."mytable"'
        conn.close()

    def test_unqualified_for_a_pre_fix_kb_with_no_schema(self):
        kb = KnowledgeBase(id="x", name="x", provider="pgvector", config={"table": "mytable"})
        ident = _kb_table_identifier(kb)
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="inaiera_agents", user="inaiera", password="inaiera_dev")
        cur = conn.cursor()
        assert ident.as_string(cur) == '"mytable"'
        conn.close()

    def test_unqualified_when_config_schema_is_an_untrusted_arbitrary_value(self):
        kb = KnowledgeBase(id="x", name="x", provider="pgvector", config={"table": "mytable", "schema": "public"})
        ident = _kb_table_identifier(kb)
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="inaiera_agents", user="inaiera", password="inaiera_dev")
        cur = conn.cursor()
        assert ident.as_string(cur) == '"mytable"'
        conn.close()


class TestRealIngestionSchemaIsolation:
    def test_ingesting_into_a_colliding_table_name_never_touches_the_real_pre_existing_table(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        table_name = f"customer_table_{uuid.uuid4().hex[:8]}"

        with Session(engine) as s:
            s.exec(text(f'CREATE TABLE public."{table_name}" (id SERIAL PRIMARY KEY, username TEXT, email TEXT)'))
            s.exec(text(f'INSERT INTO public."{table_name}" (username, email) VALUES (\'real_customer\', \'customer@example.com\')'))
            s.commit()

        kb_id = None
        try:
            r = client.post("/api/integrations/knowledge-bases", json={
                "name": f"Schema Test KB {uuid.uuid4().hex[:6]}", "provider": "pgvector",
                "config": {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"},
            })
            kb_id = r.json()["id"]

            r2 = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"Real InAiEra KB content, unrelated to the customer's own table."), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            assert r2.status_code == 200, r2.text

            with Session(engine) as s:
                cols = s.exec(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
                ), params={"t": table_name}).all()
                assert [c[0] for c in cols] == ["id", "username", "email"]
                rows = s.exec(text(f'SELECT username, email FROM public."{table_name}"')).all()
                assert rows == [("real_customer", "customer@example.com")]

                cols2 = s.exec(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=:s AND table_name=:t"
                ), params={"s": KB_MANAGED_SCHEMA, "t": table_name}).all()
                assert "embedding" in [c[0] for c in cols2]
        finally:
            if kb_id:
                cleanup_kb(kb_id)
            with Session(engine) as s:
                s.exec(text(f'DROP TABLE IF EXISTS public."{table_name}"'))
                s.exec(text(f'DROP TABLE IF EXISTS "{KB_MANAGED_SCHEMA}"."{table_name}"'))
                s.commit()

    def test_a_new_kbs_first_ingestion_genuinely_creates_the_managed_schema(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        table_name = f"new_kb_table_{uuid.uuid4().hex[:8]}"
        kb_id = None
        try:
            r = client.post("/api/integrations/knowledge-bases", json={
                "name": f"New KB {uuid.uuid4().hex[:6]}", "provider": "pgvector",
                "config": {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"},
            })
            kb_id = r.json()["id"]
            r2 = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"content"), "text/plain")},
                data={"table": table_name, "max_words": "10"},
            )
            assert r2.status_code == 200, r2.text

            with Session(engine) as s:
                kb = s.get(KnowledgeBase, kb_id)
                assert kb.config["schema"] == KB_MANAGED_SCHEMA
                schemas = s.exec(text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = :s"),
                                  params={"s": KB_MANAGED_SCHEMA}).all()
                assert len(schemas) == 1
        finally:
            if kb_id:
                cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])

    def test_retrieval_works_correctly_against_the_schema_qualified_table(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        table_name = f"retr_test_{uuid.uuid4().hex[:8]}"
        kb_id = None
        try:
            r = client.post("/api/integrations/knowledge-bases", json={
                "name": f"Retr Test KB {uuid.uuid4().hex[:6]}", "provider": "pgvector",
                "config": {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"},
            })
            kb_id = r.json()["id"]
            client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("t.txt", io.BytesIO(b"the real unique phrase zzyzxquail lives here"), "text/plain")},
                data={"table": table_name, "max_words": "20"},
            )
            r3 = client.post(f"/api/integrations/knowledge-bases/{kb_id}/retrieval-test", json={"query": "zzyzxquail", "top_k": 3})
            assert r3.status_code == 200
            assert any("zzyzxquail" in res["content"] for res in r3.json()["results"])
        finally:
            if kb_id:
                cleanup_kb(kb_id, extra_tables=[f'"{KB_MANAGED_SCHEMA}"."{table_name}"'])


class TestRealBackwardCompatibility:
    def test_a_pre_fix_kb_with_no_schema_key_keeps_reading_its_existing_table(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "v4lBU1Ix6yG8C-QG5SZhUc-r5HOjAWI5eQrIWJip-dw=")
        from app.harness import handle_retrieval
        from app.embeddings import embed_texts, to_pgvector_literal
        from app.secrets_crypto import encrypt_secret
        import psycopg2

        table_name = f"legacy_{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.exec(text(f'CREATE TABLE public."{table_name}" (id BIGSERIAL PRIMARY KEY, content TEXT, source_filename TEXT, document_id TEXT, embedding VECTOR(384))'))
            s.commit()

        vec = embed_texts(["legacy content predating the schema fix"])[0]
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="inaiera_agents", user="inaiera", password="inaiera_dev")
        cur = conn.cursor()
        cur.execute(f'INSERT INTO public."{table_name}" (content, source_filename, document_id, embedding) VALUES (%s, %s, %s, %s::vector)',
                    ("legacy content predating the schema fix", "legacy.txt", "doc-1", to_pgvector_literal(vec)))
        conn.commit()
        conn.close()

        kb_id = f"legacy_{uuid.uuid4().hex[:8]}"
        try:
            with Session(engine) as s:
                kb = KnowledgeBase(
                    id=kb_id, name=kb_id, provider="pgvector", status="connected",
                    config={"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera",
                            "password": encrypt_secret("inaiera_dev"), "table": table_name, "text_column": "content"},
                )
                s.add(kb)
                s.commit()

                ctx = {"input": "legacy content", "vars": {}, "agent_id": "x", "invoking_user": "user:test", "output": None}
                cfg = {"knowledge_bases": [kb.name], "top_k": 3}
                result_ctx = handle_retrieval(cfg, ctx, s)
                assert "legacy content predating the schema fix" in result_ctx["vars"]["retrieved_chunks"]
        finally:
            cleanup_kb(kb_id)
            with Session(engine) as s:
                s.exec(text(f'DROP TABLE IF EXISTS public."{table_name}"'))
                s.commit()
