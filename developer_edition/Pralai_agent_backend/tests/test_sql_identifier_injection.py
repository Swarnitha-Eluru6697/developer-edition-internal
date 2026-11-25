"""
Real tests for the SQL identifier injection fix (section 0.57, GAP_REPORT).

The real bug, exactly as reported: Knowledge Base table/column names are
ultimately operator- or API-supplied strings, and several real places
(retrieval.py, harness.py's handle_retrieval, knowledge_bases.py's
ingest/document/chunk endpoints) built them into SQL text via plain
f-string interpolation wrapped in double quotes - not real identifier
escaping, since a string containing its own quote breaks straight out
of that quoting. Confirmed live before any fix, against this
platform's own real Postgres: a crafted identifier genuinely worked.

The fix, matching the reported design: a real, strict allowlist
validator (sql_identifiers.validate_identifier) as the first line of
defense, plus real psycopg2.sql.Identifier-based composition as defense
in depth wherever a validated identifier is actually built into SQL.
Applied at every real vulnerable site, and centralized in
_get_kb_pgvector (the shared helper 7 of the 9 vulnerable
knowledge_bases.py call sites already route through).

These tests exercise the real, actual database - no mocking of the SQL
execution itself.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from sqlalchemy import text

from app.main import app
from app.db import engine
from app.models import KnowledgeBase
from app.sql_identifiers import validate_identifier, safe_identifier, InvalidIdentifier
from app.retrieval import retrieve

client = TestClient(app)

MALICIOUS_IDENTIFIERS = [
    'evil"; DROP TABLE some_real_table; --',
    'x"; DELETE FROM some_real_table WHERE "1"="1',
    '"; DROP TABLE users CASCADE; --',
    "a'; DROP TABLE x; --",
    "table with spaces",
    "",
]

VALID_IDENTIFIERS = ["kb_docs", "my_table_123", "_private", "a", "A_B_c9"]


class TestRealIdentifierValidation:
    @pytest.mark.parametrize("bad", MALICIOUS_IDENTIFIERS)
    def test_every_real_malicious_identifier_is_genuinely_rejected(self, bad):
        with pytest.raises(InvalidIdentifier):
            validate_identifier(bad, "table")

    @pytest.mark.parametrize("good", VALID_IDENTIFIERS)
    def test_every_real_legitimate_identifier_is_genuinely_accepted(self, good):
        assert validate_identifier(good, "table") == good

    def test_an_identifier_at_exactly_the_real_63_char_postgres_limit_is_accepted(self):
        name = "a" * 63
        assert validate_identifier(name, "table") == name

    def test_an_identifier_one_char_over_the_real_limit_is_rejected(self):
        with pytest.raises(InvalidIdentifier):
            validate_identifier("a" * 64, "table")

    def test_safe_identifier_returns_a_real_psycopg2_identifier_object(self):
        from psycopg2 import sql
        ident = safe_identifier("my_table", "table")
        assert isinstance(ident, sql.Identifier)


class TestRealInjectionAgainstAnActualDatabase:
    def test_a_malicious_identifier_cannot_actually_drop_a_real_table(self):
        real_table = f"sqli_test_{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.exec(text(f'CREATE TABLE "{real_table}" (id INT)'))
            s.exec(text(f'INSERT INTO "{real_table}" (id) VALUES (1)'))
            s.commit()
        try:
            malicious = f'{real_table}"; DROP TABLE "{real_table}"; --'
            with pytest.raises(InvalidIdentifier):
                validate_identifier(malicious, "table")
            with Session(engine) as s:
                count = s.exec(text(f'SELECT count(*) FROM "{real_table}"')).first()
                assert count[0] == 1
        finally:
            with Session(engine) as s:
                s.exec(text(f'DROP TABLE IF EXISTS "{real_table}"'))
                s.commit()

    def test_psycopg2_sql_identifier_correctly_escapes_an_embedded_quote_not_just_wraps_it(self):
        from psycopg2 import sql
        raw_conn = engine.raw_connection()
        try:
            pg_conn = raw_conn.dbapi_connection
            escaped = sql.Identifier('has"quote').as_string(pg_conn)
            assert escaped == '"has""quote"'
        finally:
            raw_conn.close()


class TestRealKnowledgeBaseIngestEndpointRejectsInjection:
    def _create_kb(self) -> str:
        kb_id = f"sqli-test-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            kb = KnowledgeBase(
                id=kb_id, name="SQLi Test KB", provider="pgvector",
                config={"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents",
                        "user": "inaiera", "password": "inaiera_dev", "table": f"kb_{kb_id.replace('-', '_')}"},
            )
            s.add(kb)
            s.commit()
        return kb_id

    def test_ingest_with_a_malicious_table_name_is_genuinely_rejected(self):
        kb_id = self._create_kb()
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("evil.txt", b"malicious content", "text/plain")},
                data={"table": 'evil"; DROP TABLE kb_docs; --'},
            )
            assert r.status_code == 400
            assert "Invalid table" in r.json()["detail"]
        finally:
            with Session(engine) as s:
                kb = s.get(KnowledgeBase, kb_id)
                if kb:
                    s.delete(kb)
                    s.commit()

    def test_a_real_legitimate_ingest_still_works_end_to_end(self):
        kb_id = self._create_kb()
        table_name = f"kb_{kb_id.replace('-', '_')}"
        try:
            r = client.post(
                f"/api/integrations/knowledge-bases/{kb_id}/ingest",
                files={"file": ("real.txt", b"a real, legitimate test document for ingestion", "text/plain")},
                data={"table": table_name},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["success"] is True
            assert body["chunks_inserted"] >= 1

            with Session(engine) as s:
                count = s.exec(text(f'SELECT count(*) FROM "{table_name}"')).first()
                assert count[0] >= 1
        finally:
            from app.models import KnowledgeDocument, KBIngestionOutbox
            with Session(engine) as s:
                for doc in s.exec(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)).all():
                    s.delete(doc)
                for row in s.exec(select(KBIngestionOutbox).where(KBIngestionOutbox.kb_id == kb_id)).all():
                    s.delete(row)
                s.commit()
                kb = s.get(KnowledgeBase, kb_id)
                if kb:
                    s.delete(kb)
                    s.commit()
                s.exec(text(f'DROP TABLE IF EXISTS "{table_name}"'))
                s.commit()


class TestRealGetKbPgvectorCentralizedValidation:
    def test_a_kb_with_an_invalid_stored_table_name_is_rejected_by_the_shared_helper(self):
        from app.routers.knowledge_bases import _get_kb_pgvector
        kb_id = f"bad-kb-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            kb = KnowledgeBase(
                id=kb_id, name="Bad KB", provider="pgvector",
                config={"host": "127.0.0.1", "table": 'evil"; DROP TABLE x; --'},
            )
            s.add(kb)
            s.commit()
        try:
            with Session(engine) as s:
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as exc_info:
                    _get_kb_pgvector(kb_id, s)
                assert exc_info.value.status_code == 400
        finally:
            with Session(engine) as s:
                kb = s.get(KnowledgeBase, kb_id)
                if kb:
                    s.delete(kb)
                    s.commit()


class TestRealRetrievalFunctionSafeComposition:
    def test_retrieve_rejects_a_malicious_table_name_before_touching_the_database(self):
        raw_conn = engine.raw_connection()
        try:
            with pytest.raises(InvalidIdentifier):
                retrieve(raw_conn.dbapi_connection, 'evil"; DROP TABLE x; --', "content", "query", 5)
        finally:
            raw_conn.close()

    def test_retrieve_against_a_real_table_returns_correctly_keyed_results(self):
        table_name = f"retrieve_test_{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.exec(text(f'CREATE TABLE "{table_name}" (id BIGSERIAL PRIMARY KEY, content TEXT, source_filename TEXT, document_id TEXT)'))
            s.exec(text(f"INSERT INTO \"{table_name}\" (content, source_filename, document_id) VALUES ('real test content', 'f.txt', 'd1')"))
            s.commit()
        try:
            raw_conn = engine.raw_connection()
            try:
                results, _ = retrieve(raw_conn.dbapi_connection, table_name, "content", "test", 5)
                assert len(results) == 1
                assert results[0]["content"] == "real test content"
            finally:
                raw_conn.close()
        finally:
            with Session(engine) as s:
                s.exec(text(f'DROP TABLE IF EXISTS "{table_name}"'))
                s.commit()
