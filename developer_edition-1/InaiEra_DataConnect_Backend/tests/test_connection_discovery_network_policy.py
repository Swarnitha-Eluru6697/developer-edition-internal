"""Confirms discover/introspect_columns/sample_rows are guarded by the same
network policy as live_probe. Uses only literal blocked IPs so these never
attempt a real driver connection and stay fast and deterministic."""

import pytest

from app.core.config import settings
from app.services.connection_discovery import discover, introspect_columns, sample_rows


@pytest.mark.asyncio
async def test_discover_blocks_cloud_metadata_address() -> None:
    result = await discover(
        {
            "source_type": "postgres",
            "connection_config": {"hostname": "169.254.169.254", "database": "db"},
        }
    )

    assert result["connected"] is False
    assert "does not permit" in result["message"]


@pytest.mark.asyncio
async def test_introspect_columns_blocks_loopback() -> None:
    result = await introspect_columns(
        {"source_type": "mysql", "connection_config": {"hostname": "127.0.0.1"}},
        "orders",
    )

    assert result["connected"] is False
    assert "does not permit" in result["message"]


@pytest.mark.asyncio
async def test_sample_rows_blocks_cloud_metadata_address() -> None:
    result = await sample_rows(
        {"source_type": "mongodb", "connection_config": {"hostname": "169.254.169.254"}},
        "orders",
    )

    assert result["connected"] is False
    assert "does not permit" in result["message"]


@pytest.mark.asyncio
async def test_sample_rows_clamps_limit_to_deployment_ceiling(monkeypatch) -> None:
    """A caller-requested limit can never exceed the configured ceiling —
    verified by observing the clamped limit reach the driver call."""
    monkeypatch.setattr(settings, "max_sample_rows", 5)
    captured = {}

    async def _fake_pg_rows(cfg, entity, limit, timeout):
        captured["limit"] = limit
        return []

    monkeypatch.setattr("app.services.connection_discovery._ROWS", {"postgres": _fake_pg_rows})

    await sample_rows(
        {"source_type": "postgres", "connection_config": {"hostname": "10.0.0.5", "database": "db"}},
        "orders",
        limit=10_000,
    )

    assert captured["limit"] == 5


@pytest.mark.asyncio
async def test_discover_unsupported_source_still_validates_destination() -> None:
    """Even a source type with no live driver support (falls back to the
    configured object list) must not skip the destination check first."""
    result = await discover(
        {
            "source_type": "s3",
            "connection_config": {"hostname": "169.254.169.254", "objects": ["file.csv"]},
        }
    )

    assert result["connected"] is False
    assert "does not permit" in result["message"]
