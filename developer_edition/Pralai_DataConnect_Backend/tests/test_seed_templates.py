import pytest

import app.db.seed_templates as seed_templates
from app.core.config import settings


class _FailingConnBegin:
    """Simulates engine.begin() always failing (DB unreachable)."""

    async def __aenter__(self):
        raise ConnectionError("could not connect to server")

    async def __aexit__(self, *exc):
        return False


class _FailingEngine:
    def begin(self):
        return _FailingConnBegin()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """The real retry loop sleeps 2s between up to 15 attempts — make it
    instantaneous so these tests don't take ~30s each."""

    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(seed_templates.asyncio, "sleep", _no_sleep)


@pytest.mark.asyncio
async def test_init_template_catalog_raises_in_development(monkeypatch) -> None:
    """There is no in-memory fallback for any environment, including
    development — the connector-template catalog has exactly one copy of
    its data, in PostgreSQL. A database outage means the catalog can't be
    seeded/verified, and startup must surface that rather than silently
    serving from a disconnected in-memory copy."""
    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setattr(seed_templates, "engine", _FailingEngine())

    with pytest.raises(RuntimeError, match="unreachable"):
        await seed_templates.init_template_catalog()


@pytest.mark.asyncio
async def test_init_template_catalog_raises_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(seed_templates, "engine", _FailingEngine())

    with pytest.raises(RuntimeError, match="unreachable"):
        await seed_templates.init_template_catalog()


@pytest.mark.asyncio
async def test_init_template_catalog_raises_in_staging(monkeypatch) -> None:
    """Staging is meant to mirror production infrastructure — a silent
    fallback there would mask the same class of problem it's meant to catch
    before it reaches production."""
    monkeypatch.setattr(settings, "env", "staging")
    monkeypatch.setattr(seed_templates, "engine", _FailingEngine())

    with pytest.raises(RuntimeError, match="unreachable"):
        await seed_templates.init_template_catalog()


def test_new_seed_templates_include_object_store_sinks() -> None:
    """Regression guard for a real bug: init_template_catalog()'s refresh path
    used to only iterate rows already in the DB, so a template added to
    TEMPLATE_SEED after the catalog's first seed would never get inserted on
    a later restart (fixed by the new-rows backfill in init_template_catalog's
    else-branch). This pins the object-store sinks the seed declares today; the
    actual insert-on-restart behavior was verified live against a running
    Postgres-backed catalog rather than mocked here, since init_template_catalog
    talks to AsyncSessionLocal/engine directly.

    tpl_gcs_sink, tpl_iceberg_sink and tpl_adls_sink are no longer seeded (the
    catalog is now exactly the supplied connector list, which has none of them). connect_config.py can still build both if a template for
    them is registered by hand, which is covered in
    test_connect_config_object_store_sinks.py."""
    ids = {row["id"] for row in seed_templates._seed_rows()}
    assert "tpl_s3_sink" in ids
    assert not ids & {"tpl_gcs_sink", "tpl_iceberg_sink", "tpl_adls_sink"}


def test_every_seed_template_declares_its_supported_modes() -> None:
    """FE-BUG-23: the bug this regresses against — _BASE never set
    supported_modes, so every template fell back to the "empty means
    unrestricted" default and reported as supporting every mode regardless
    of what it actually does. Every seed row must now declare a real,
    non-empty, curated list."""
    for row in seed_templates._seed_rows():
        assert row["supported_modes"], row["id"]


def test_cdc_sources_do_not_declare_batch_mode() -> None:
    """Sanity check on the curation itself, not just its presence: a
    log-based CDC source doesn't poll in discrete batches."""
    for row in seed_templates._seed_rows():
        if row["category"] == "database_cdc":
            assert "batch" not in row["supported_modes"], row["id"]
            assert "cdc" in row["supported_modes"], row["id"]


def test_object_store_and_api_sources_do_not_declare_cdc_or_snapshot() -> None:
    """Neither concept applies outside a database source — file/object-store
    and API/webhook sources aren't log-based or table-snapshotable."""
    for row in seed_templates._seed_rows():
        if row["connector_type"] == "source" and row["category"] in ("file", "api"):
            assert "cdc" not in row["supported_modes"], row["id"]
            assert "snapshot" not in row["supported_modes"], row["id"]


def test_only_jdbc_connectors_declare_schema_discovery_support() -> None:
    """supports_schema_discovery is real code capability, not a vibe — see
    app/services/connection_discovery.py's _COL, which only knows how to
    introspect postgres/mysql/sqlserver/oracle/mongodb. A template whose
    connection can't possibly land in one of those dialects must say so."""
    for row in seed_templates._seed_rows():
        if row["category"] in ("database_cdc", "database_jdbc") or row["id"] == "tpl_jdbc_sink":
            assert row["supports_schema_discovery"] is True, row["id"]
        else:
            assert row["supports_schema_discovery"] is False, row["id"]


def test_every_seed_template_declares_its_compatibility_contract() -> None:
    """A production connector template must identify its plugin, plugin
    version, and the Kafka Connect versions it's qualified against —
    otherwise reproducing or safely upgrading a deployed connector is
    guesswork. supported_source_versions is allowed to be empty (some
    connectors, e.g. generic JDBC or managed cloud sinks, aren't pinned to
    a discrete external-system version)."""
    for row in seed_templates._seed_rows():
        assert row["connector_plugin"], row["id"]
        assert row["connector_version"], row["id"]
        assert row["supported_kafka_connect_versions"], row["id"]
