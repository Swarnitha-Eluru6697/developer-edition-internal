import pytest

from app.services.connection_adapters import adapter_for_source_type


@pytest.mark.asyncio
async def test_api_adapter_validates_base_url_without_network_call() -> None:
    adapter = adapter_for_source_type("api")

    result = await adapter.test(
        {"source_type": "api", "connection_config": {"base_url": "https://example.com"}}
    )

    assert result.status == "ok"


@pytest.mark.asyncio
async def test_database_adapter_reports_missing_fields() -> None:
    adapter = adapter_for_source_type("postgres")

    result = await adapter.test({"source_type": "postgres", "connection_config": {}})

    assert result.status == "failed"
    assert "Missing database fields" in result.message

