"""source_type has one canonical spelling for PostgreSQL ("postgresql"); the older
"postgres" is accepted as an alias and normalised, never stored as a second type."""
import pytest
from pydantic import ValidationError

from app.schemas.connection import SOURCE_TYPES, ConnectionProfileCreate


@pytest.mark.parametrize("sent", ["postgres", "Postgres", "POSTGRESQL", "postgresql"])
def test_postgres_spellings_normalise_to_postgresql(sent: str) -> None:
    assert ConnectionProfileCreate(name="c", source_type=sent).source_type == "postgresql"


def test_the_alias_is_not_listed_as_a_separate_source_type() -> None:
    assert "postgresql" in SOURCE_TYPES
    assert "postgres" not in SOURCE_TYPES


def test_an_unknown_source_type_is_still_rejected() -> None:
    with pytest.raises(ValidationError, match="not supported"):
        ConnectionProfileCreate(name="c", source_type="postgress")
