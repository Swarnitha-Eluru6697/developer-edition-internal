"""
Real tests for the Alembic migration system fix (section 0.69,
GAP_REPORT).

The real bug, exactly as reported: db.py relied purely on
SQLModel.metadata.create_all(engine) plus a growing, unversioned list
of manual ALTER TABLE ADD COLUMN IF NOT EXISTS statements, one per
schema change since - create_all() only ever creates tables that don't
exist yet; it cannot safely evolve an existing production schema from
one real release to the next, and there was no real migration history,
no rollback path, and no way to know what schema version a given
database was actually at.

The fix: a real Alembic setup (migrations/env.py wired to the app's
own real DATABASE_URL and SQLModel.metadata), a real baseline
migration capturing the full, actual schema (verified directly to
match the existing, already-evolved reference database column for
column, including every prior ad-hoc ALTER), and db.py's own init_db()
now runs a real `alembic upgrade head` (via AUTO_MIGRATE, default true
for local dev/this reference environment) instead of create_all().

These tests exercise the real Alembic upgrade against real, disposable
Postgres databases created and dropped for each test - not mocked.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import psycopg2
from sqlmodel import Session, create_engine, text


def make_disposable_db() -> str:
    dbname = f"inaiera_test_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="postgres", user="inaiera", password="inaiera_dev")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'CREATE DATABASE "{dbname}"')
    conn.close()

    conn2 = psycopg2.connect(host="127.0.0.1", port=5432, dbname=dbname, user="inaiera", password="inaiera_dev")
    conn2.autocommit = True
    conn2.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn2.close()
    return dbname


def drop_disposable_db(dbname: str):
    # Real, necessary cleanup before DROP DATABASE can succeed —
    # SQLAlchemy engines (both the module-level app.db.engine, reloaded
    # to point at this disposable database during the test, and any
    # engine created directly in a test) keep a real connection pool
    # open until explicitly disposed; Postgres refuses to drop a
    # database with real, live sessions still attached to it.
    import app.db as db_module
    db_module.engine.dispose()
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="postgres", user="inaiera", password="inaiera_dev")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    conn.close()


class TestRealAlembicBootstrap:
    @pytest.fixture(autouse=True)
    def restore_db_module(self):
        """A real, direct guard against this file's own
        importlib.reload(app.db) leaking a test database's engine into
        the rest of the real test session — pytest runs all test files
        in the same real process, so app.db.engine must be restored to
        the real, original database after every test here."""
        import app.db as db_module
        original_url = db_module.DATABASE_URL
        yield
        import importlib
        os.environ["DATABASE_URL"] = original_url
        os.environ.pop("AUTO_MIGRATE", None)
        importlib.reload(db_module)

    def test_init_db_creates_the_full_real_schema_on_a_totally_fresh_database(self, monkeypatch):
        dbname = make_disposable_db()
        db_module = None
        try:
            monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            monkeypatch.setenv("AUTO_MIGRATE", "true")
            import importlib
            import app.db as db_module
            importlib.reload(db_module)
            db_module.init_db()

            engine = create_engine(f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            with Session(engine) as s:
                tables = s.exec(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
                table_names = {t[0] for t in tables}
                for expected in ("agent", "run", "trace", "modelprovider", "knowledgebase", "kbingestionoutbox",
                                 "template", "templateversion", "alembic_version"):
                    assert expected in table_names, f"missing real table: {expected}"

                # Template lifecycle migration must create the partial,
                # case-insensitive active-name index. This fresh-DB test also
                # executes the TemplateVersion backfill SQL, catching syntax
                # defects such as an unquoted reserved `desc` identifier.
                indexes = s.exec(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='template'"
                )).all()
                assert "uq_template_active_name_ci" in {row[0] for row in indexes}

                version = s.exec(text("SELECT version_num FROM alembic_version")).first()
                assert version is not None
            engine.dispose()
        finally:
            drop_disposable_db(dbname)

    def test_a_second_upgrade_against_an_already_current_database_is_a_safe_no_op(self, monkeypatch):
        dbname = make_disposable_db()
        try:
            monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            monkeypatch.setenv("AUTO_MIGRATE", "true")
            import importlib
            import app.db as db_module
            importlib.reload(db_module)
            db_module.init_db()
            db_module.init_db()

            engine = create_engine(f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            with Session(engine) as s:
                count = s.exec(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).first()
                assert count[0] > 0
            engine.dispose()
        finally:
            drop_disposable_db(dbname)

    def test_auto_migrate_false_genuinely_skips_migration(self, monkeypatch):
        dbname = make_disposable_db()
        try:
            monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            monkeypatch.setenv("AUTO_MIGRATE", "false")
            import importlib
            import app.db as db_module
            importlib.reload(db_module)
            db_module.init_db()

            engine = create_engine(f"postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/{dbname}")
            with Session(engine) as s:
                tables = s.exec(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
                assert "agent" not in {t[0] for t in tables}
            engine.dispose()
        finally:
            drop_disposable_db(dbname)


class TestRealMigrationEnvironmentWiring:
    def test_migrations_env_uses_the_apps_own_real_database_url(self):
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(backend_dir, "migrations", "env.py")
        with open(env_path) as f:
            content = f.read()
        assert "from app.db import DATABASE_URL" in content
        assert "from app.models import SQLModel" in content

    def test_the_baseline_migration_imports_sqlmodel_for_its_own_column_types(self):
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        versions_dir = os.path.join(backend_dir, "migrations", "versions")
        found_any = False
        for fname in os.listdir(versions_dir):
            if fname.endswith(".py"):
                with open(os.path.join(versions_dir, fname)) as f:
                    content = f.read()
                if "sqlmodel.sql.sqltypes" in content:
                    found_any = True
                    assert "import sqlmodel" in content, f"{fname} uses sqlmodel types but never imports sqlmodel"
        assert found_any
