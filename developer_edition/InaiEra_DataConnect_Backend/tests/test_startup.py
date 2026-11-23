import pytest

import app.startup as startup
from app.core.config import settings


def test_validate_configuration_passes_with_default_dev_settings() -> None:
    startup.validate_configuration()  # must not raise


def test_validate_configuration_rejects_missing_database_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", "")

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        startup.validate_configuration()


def test_validate_configuration_rejects_default_jwt_secret_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "jwt_secret", "CHANGEME")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        startup.validate_configuration()


def test_validate_configuration_allows_default_jwt_secret_outside_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setattr(settings, "jwt_secret", "CHANGEME")

    startup.validate_configuration()  # must not raise — dev/test ergonomics


def test_validate_configuration_reports_every_problem_at_once(monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "jwt_secret", "CHANGEME")

    with pytest.raises(RuntimeError) as exc_info:
        startup.validate_configuration()

    assert "DATABASE_URL" in str(exc_info.value)
    assert "JWT_SECRET" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_postgres_succeeds_immediately_when_reachable(monkeypatch) -> None:
    calls = {"count": 0}

    class _FakeConn:
        async def __aenter__(self):
            calls["count"] += 1
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args, **kwargs):
            return None

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    monkeypatch.setattr("app.db.session.engine", _FakeEngine())

    await startup.verify_postgres(max_attempts=5, delay_seconds=0.001)

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_verify_postgres_retries_then_succeeds(monkeypatch) -> None:
    attempts = {"count": 0}

    class _FakeConn:
        async def __aenter__(self):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionError("not ready yet")
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args, **kwargs):
            return None

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    monkeypatch.setattr("app.db.session.engine", _FakeEngine())

    await startup.verify_postgres(max_attempts=5, delay_seconds=0.001)

    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_verify_postgres_raises_after_exhausting_attempts(monkeypatch) -> None:
    class _FakeConn:
        async def __aenter__(self):
            raise ConnectionError("still not ready")

        async def __aexit__(self, *exc):
            return False

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    monkeypatch.setattr("app.db.session.engine", _FakeEngine())

    with pytest.raises(RuntimeError, match="not reachable after 3 attempts"):
        await startup.verify_postgres(max_attempts=3, delay_seconds=0.001)


def test_main_runs_every_step_in_order_then_execs_uvicorn(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(startup, "validate_configuration", lambda: calls.append("validate_configuration"))

    async def _verify_postgres():
        calls.append("verify_postgres")

    monkeypatch.setattr(startup, "verify_postgres", lambda: _verify_postgres())
    monkeypatch.setattr(startup, "run_migrations", lambda: calls.append("run_migrations"))

    async def _verify_seed_version():
        calls.append("verify_seed_version")

    monkeypatch.setattr(startup, "verify_seed_version", lambda: _verify_seed_version())

    def _fake_execvp(*args):
        calls.append("execvp")

    monkeypatch.setattr(startup.os, "execvp", _fake_execvp)

    startup.main()

    assert calls == [
        "validate_configuration", "verify_postgres", "run_migrations", "verify_seed_version", "execvp",
    ]


def test_main_stops_and_exits_nonzero_when_a_step_fails(monkeypatch) -> None:
    """The core of the release contract: a failed step must never fall
    through to starting the API anyway."""
    calls: list[str] = []

    monkeypatch.setattr(startup, "validate_configuration", lambda: calls.append("validate_configuration"))

    async def _failing_verify_postgres():
        raise RuntimeError("Postgres unreachable")

    monkeypatch.setattr(startup, "verify_postgres", lambda: _failing_verify_postgres())
    monkeypatch.setattr(startup, "run_migrations", lambda: calls.append("run_migrations"))

    def _fake_execvp(*args):
        calls.append("execvp")

    monkeypatch.setattr(startup.os, "execvp", _fake_execvp)

    with pytest.raises(SystemExit) as exc_info:
        startup.main()

    assert exc_info.value.code == 1
    assert calls == ["validate_configuration"]  # never reached run_migrations or execvp
