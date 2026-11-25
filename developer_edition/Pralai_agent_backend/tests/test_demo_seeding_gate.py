"""A clean production database must stay free of demo data.

The reported gap: startup called seed(), and seed() returned early only
when an Agent row already existed. A fresh database — production
included — therefore received the whole demo set on first startup:
sample agents, runs, traces, alerts, role bindings, agent logs, and data
sources marked "connected" that were never connected. Synthetic runs and
traces skew every monitoring average the platform computes, and nothing
afterwards distinguishes them from real activity.

What makes this more than cosmetic: the seeded operational records are
indistinguishable from real ones by design (they were built to make the
UI look populated), so "is this metric real?" becomes unanswerable, and
a governance review finds role bindings to agents nobody created.

The split these tests pin:

  reference data — model providers, the chat/embedding models they
    serve, the tool-connector catalog, the template gallery. A fresh
    install needs these to function (there is nothing to attach an API
    key to without the provider catalog), so they are NOT gated.

  demo data — the sample agents and their synthetic history. Gated
    behind SEED_DEMO_DATA, and refused outright in production.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from app import seed as seed_module
from app.models import (Agent, AgentLog, Alert, DataSource, MCPServer, ModelProvider,
                        ProviderModel, Role, Run, Template, Tool, ToolAction, Trace)

DEMO_TABLES = ("agent", "run", "trace", "alert", "role", "agentlog", "datasource", "mcpserver")
REFERENCE_TABLES = ("modelprovider", "providermodel", "tool", "toolaction", "template")


@pytest.fixture
def fresh_db(monkeypatch):
    """A genuinely empty database, created and dropped per test — the
    "clean production database" the release criterion names. A shared
    scratch database would already hold rows and could not tell a
    no-op apart from a refusal."""
    admin_url = os.getenv("DATABASE_URL", "postgresql://inaiera:inaiera_dev@localhost:5435/inaiera_agents")
    base, _, _current = admin_url.rpartition("/")
    db_name = f"seedgate_{uuid.uuid4().hex[:10]}"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = f"{base}/{db_name}"

    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    # The seed module holds its own module-level engine; point both it
    # and anything it imports at this throwaway database.
    monkeypatch.setattr(seed_module, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
        admin.dispose()


def _counts(engine, tables):
    with Session(engine) as s:
        return {t: s.exec(text(f"select count(*) from {t}")).one()[0] for t in tables}


class TestTheReleaseCriterion:
    def test_a_clean_production_database_receives_no_demo_data(self, fresh_db, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("SEED_DEMO_DATA", raising=False)

        seed_module.seed()

        demo = _counts(fresh_db, DEMO_TABLES)
        assert demo == {t: 0 for t in DEMO_TABLES}, f"demo rows reached a production database: {demo}"

    def test_the_flag_is_refused_in_production_rather_than_honoured(self, fresh_db, monkeypatch):
        """Defaulting off is not enough: a production deployment that
        inherited SEED_DEMO_DATA=true from a dev template must still get
        nothing."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("SEED_DEMO_DATA", "true")

        assert seed_module.demo_seeding_enabled() is False
        seed_module.seed()
        demo = _counts(fresh_db, DEMO_TABLES)
        assert demo == {t: 0 for t in DEMO_TABLES}, f"the flag was honoured in production: {demo}"

    def test_a_clean_non_production_database_also_stays_clean_by_default(self, fresh_db, monkeypatch):
        """Off unless asked for, in every environment — the flag is the
        opt-in, not production the opt-out."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("SEED_DEMO_DATA", raising=False)

        seed_module.seed()
        assert _counts(fresh_db, DEMO_TABLES) == {t: 0 for t in DEMO_TABLES}

    def test_no_synthetic_run_or_trace_can_skew_monitoring(self, fresh_db, monkeypatch):
        """The specific harm named: seeded runs and traces are
        indistinguishable from real ones and move every average."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        with Session(fresh_db) as s:
            assert s.exec(select(Run)).all() == []
            assert s.exec(select(Trace)).all() == []
            assert s.exec(select(Alert)).all() == []

    def test_no_integration_is_reported_as_connected_that_is_not(self, fresh_db, monkeypatch):
        """Two seeded data sources and both MCP servers carried
        status="connected" without any credential behind them."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        with Session(fresh_db) as s:
            assert s.exec(select(DataSource)).all() == []
            assert s.exec(select(MCPServer)).all() == []


class TestReferenceDataIsStillInstalled:
    """The other half of correctness: gating everything would leave a
    fresh install with no provider catalog, so there would be nothing to
    attach an API key to and no tool for a Tool node to call."""

    def test_a_clean_production_database_gets_the_capability_catalog(self, fresh_db, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        counts = _counts(fresh_db, REFERENCE_TABLES)
        for table, n in counts.items():
            assert n > 0, f"reference table {table} is empty on a fresh install: {counts}"

    def test_the_providers_the_integrations_page_needs_are_present(self, fresh_db, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        with Session(fresh_db) as s:
            names = {p.name for p in s.exec(select(ModelProvider)).all()}
        for expected in ("Anthropic", "OpenAI", "Gemini", "DeepSeek"):
            assert expected in names

    def test_the_tool_connectors_the_runtime_implements_are_present(self, fresh_db, monkeypatch):
        """A function-calling loop resolves "tool:Name:Action" against
        these rows — without them a real builtin connector is
        unreachable from the Builder."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        with Session(fresh_db) as s:
            actions = {f"tool:{a.tool_name}:{a.name}" for a in s.exec(select(ToolAction)).all()}
        from app.tool_connectors import _BUILTIN_CONNECTORS
        for builtin in _BUILTIN_CONNECTORS:
            assert builtin in actions, f"{builtin} has a real connector but no catalog row"


class TestReferenceSeedingIsIdempotent:
    def test_running_it_twice_duplicates_nothing(self, fresh_db, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        first = _counts(fresh_db, REFERENCE_TABLES)
        seed_module.seed()
        seed_module.seed()
        assert _counts(fresh_db, REFERENCE_TABLES) == first

    def test_it_never_overwrites_a_row_someone_has_edited(self, fresh_db, monkeypatch):
        """Insert-if-absent, not upsert: a workspace that configured an
        API key must not have it reset by the next restart."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        seed_module.seed()
        with Session(fresh_db) as s:
            row = s.get(ModelProvider, "Anthropic")
            row.api_key_configured = True
            row.status = "credits"
            s.add(row)
            s.commit()

        seed_module.seed()

        with Session(fresh_db) as s:
            row = s.get(ModelProvider, "Anthropic")
            assert row.api_key_configured is True, "a restart reset a configured provider"
            assert row.status == "credits"


class TestDemoSeedingStillWorksWhenAskedFor:
    def test_the_flag_installs_the_sample_content_outside_production(self, fresh_db, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("SEED_DEMO_DATA", "true")

        seed_module.seed()
        with Session(fresh_db) as s:
            agents = s.exec(select(Agent)).all()
        assert len(agents) == len(seed_module.AGENTS)

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("", False), ("maybe", False),
    ])
    def test_the_flag_is_read_strictly(self, monkeypatch, value, expected):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("SEED_DEMO_DATA", value)
        assert seed_module.demo_seeding_enabled() is expected

    def test_demo_seeding_is_still_once_only(self, fresh_db, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("SEED_DEMO_DATA", "true")
        seed_module.seed()
        seed_module.seed()
        with Session(fresh_db) as s:
            assert len(s.exec(select(Agent)).all()) == len(seed_module.AGENTS)


class TestStartupCallsTheGatedEntryPoint:
    def test_main_seeds_through_seed_not_the_demo_function_directly(self):
        import inspect

        from app import main

        src = inspect.getsource(main)
        assert "seed()" in src
        assert "seed_demo_data()" not in src, "startup bypasses the gate"
