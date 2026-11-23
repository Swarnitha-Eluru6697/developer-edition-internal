"""The test-DB guard itself (runs without any database)."""
import pytest

from tests.conftest import UnsafeTestDatabase, check_test_database_url


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://test:test@dq-testdb:5432/dataquality_test",
    "postgresql://u:p@127.0.0.1:55432/anything_test",
])
def test_accepts_throwaway_test_databases(url):
    assert check_test_database_url(url) == url


@pytest.mark.parametrize("url,why", [
    ("postgresql+asyncpg://dataquality:dataquality@localhost:5437/dataquality", "_test"),
    ("postgresql+asyncpg://dataquality:dataquality@localhost:5437/dataquality_test", "5437"),
    ("postgresql+asyncpg://u:p@localhost:5433/dbtrunner_test", "5433"),
    ("postgresql+asyncpg://u:p@localhost:5435/flinksql_test", "5435"),
    ("postgresql+asyncpg://u:p@dataquality-postgres-1:5432/x_test", "live compose"),
    ("sqlite+aiosqlite:///:memory:", "PostgreSQL"),
    ("postgresql+asyncpg://u:p@dq-testdb:5432/dataquality", "_test"),
])
def test_refuses_live_or_non_test_databases(url, why):
    with pytest.raises(UnsafeTestDatabase, match=why):
        check_test_database_url(url)


def test_no_hardcoded_dsn_in_test_suite():
    """No test file may name a live DSN / port."""
    import pathlib
    here = pathlib.Path(__file__).parent
    for f in here.glob("*.py"):
        if f.name in ("test_db_safety.py", "conftest.py"):
            continue
        text = f.read_text()
        for bad in (":5437", ":5433", ":5435", "/dataquality\"", "drop_all("):
            assert bad not in text, f"{f.name} contains {bad!r}"
