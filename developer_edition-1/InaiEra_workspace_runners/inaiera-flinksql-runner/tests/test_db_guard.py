"""The DB-test guard itself: anything that is not unmistakably a throwaway
*_test PostgreSQL database is refused (pytest.fail), never used."""
from __future__ import annotations

import pytest

from tests.conftest import assert_safe_test_database_url


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://flinksql:flinksql@localhost:5435/flinksql",          # the live runner DB
    "postgresql+asyncpg://flinksql:flinksql@flinksql-postgres-1:5432/flinksql",
    "postgresql+asyncpg://test:test@localhost:5435/flinksql_test",             # right name, live port
    "postgresql+asyncpg://test:test@db:5432/flinksql_testing",
    "postgresql+asyncpg://test:test@db:5432/production",
    "sqlite+aiosqlite:///x_test",
    "not a url",
])
def test_unsafe_urls_are_refused(url):
    with pytest.raises(pytest.fail.Exception, match="REFUSING"):
        assert_safe_test_database_url(url)


def test_throwaway_test_db_is_accepted():
    url = "postgresql+asyncpg://test:test@flink-runner-testdb:5432/flinksql_test"
    assert assert_safe_test_database_url(url) == url
