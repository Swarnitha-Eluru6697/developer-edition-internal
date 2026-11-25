"""Database URL construction (Helm no longer relies on $(VAR) expansion
inside a ConfigMap, and libpq's sslmode is never handed to asyncpg)."""
from __future__ import annotations

from sqlalchemy.engine import make_url

from app.core.config import Settings


def test_url_built_from_discrete_fields_escapes_password():
    s = Settings(database_url=None, database_host="pg.svc", database_port=5432, database_name="dbtrunner",
                 database_user="dbtrunner", database_password="p@ss/w:rd#?", database_ssl="require")
    url = make_url(s.effective_database_url)
    assert url.password == "p@ss/w:rd#?"
    assert url.host == "pg.svc" and url.database == "dbtrunner"
    assert "sslmode" not in s.effective_database_url
    assert s.database_connect_args() == {"ssl": "require"}


def test_sslmode_in_asyncpg_url_is_translated_not_passed_through():
    s = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=require")
    assert "sslmode" not in s.effective_database_url
    assert s.database_connect_args() == {"ssl": "require"}


def test_ssl_disabled_means_no_connect_args():
    s = Settings(database_url=None, database_host="h", database_ssl="disable")
    assert s.database_connect_args() == {}


def test_environment_only_accepts_known_values():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(environment="production")
