import pytest

from app.core.config import settings
from app.main import lifespan


@pytest.mark.asyncio
async def test_lifespan_swallows_template_catalog_failure_in_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)

    async def _failing_init():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("app.db.seed_templates.init_template_catalog", _failing_init)

    async with lifespan(app=None):
        pass  # must not raise — dev/test ergonomics are preserved


@pytest.mark.asyncio
async def test_lifespan_propagates_template_catalog_failure_outside_development(monkeypatch) -> None:
    """The core of the fix: main.py must not itself re-swallow a failure
    that app/db/seed_templates.py now deliberately raises outside dev — a
    lifespan that logs-and-continues here would silently undo that fix for
    anyone running `uvicorn app.main:app` directly against a broken database."""
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)

    async def _failing_init():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("app.db.seed_templates.init_template_catalog", _failing_init)

    with pytest.raises(RuntimeError, match="db unreachable"):
        async with lifespan(app=None):
            pass


@pytest.mark.asyncio
async def test_lifespan_runs_the_header_trust_guard_before_anything_else(monkeypatch) -> None:
    """BUG-04: a process that would silently trust client role headers must not finish starting."""

    def _refuse():
        raise RuntimeError("Refusing to start in header-trust mode")

    monkeypatch.setattr("app.core.gateway.assert_explicit_environment", _refuse)

    with pytest.raises(RuntimeError, match="Refusing to start"):
        async with lifespan(app=None):
            pass
