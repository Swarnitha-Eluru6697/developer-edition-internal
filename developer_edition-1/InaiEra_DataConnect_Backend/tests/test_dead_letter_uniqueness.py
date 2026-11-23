"""NEW-08, the parts that need no database: the unique index mirrors the DLQ policy constant, the migration's copy of it, and the shared error."""
import inspect
import re
from pathlib import Path

from app.core.error_policies import DLQ_POLICIES
from app.models.pipeline import DEAD_LETTER_TOPIC_INDEX, Pipeline
from app.services.pipeline_spec_validator import shared_dead_letter_error
from app.services.topic_naming import build_connector_quarantine_topic_name

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0053_dead_letter_topic_unique.py"


def _index():
    return next(i for i in Pipeline.__table__.indexes if i.name == DEAD_LETTER_TOPIC_INDEX)


def _policies_in(sql: str) -> set[str]:
    return set(re.search(r"error_handling_policy IN \(([^)]*)\)", sql).group(1).replace("'", "").replace(" ", "").split(","))


def test_the_index_is_unique_and_covers_exactly_the_fields_a_dead_letter_topic_is_named_from() -> None:
    index = _index()
    columns = [c.name for c in index.columns]

    assert index.unique is True
    assert columns == ["domain", "source_system", "entity", "version"]   # no org_id: the topic name has none (alembic 0057), so it is unique across organizations
    # Tied to the topic-name function itself: if the dead-letter topic is ever named from different fields, this fails and the index must follow.
    assert set(inspect.signature(build_connector_quarantine_topic_name).parameters) == set(columns)
    assert "zone" not in columns
    assert "mode" not in columns


def test_the_index_applies_to_exactly_the_dead_letter_policies_and_only_to_sinks() -> None:
    where = str(_index().dialect_options["postgresql"]["where"])

    assert _policies_in(where) == set(DLQ_POLICIES)
    assert "direction = 'SINK'" in where


def test_the_migration_names_the_same_policies_as_the_application() -> None:
    """The migration writes the policies out (history must not move when the constant does); this fails if the two drift apart."""
    source = MIGRATION.read_text()

    assert _policies_in(source) == set(DLQ_POLICIES)
    assert DEAD_LETTER_TOPIC_INDEX in source


def test_the_shared_queue_error_names_the_owner_when_known_and_not_otherwise() -> None:
    named, unnamed = shared_dead_letter_error("Orders sink"), shared_dead_letter_error(None)

    assert "'Orders sink'" in str(named)
    assert "'" not in str(unnamed).split("already uses")[0]
    assert str(unnamed).startswith("Another sink pipeline already uses this dead-letter queue")
    assert [f.field for f in named.field_errors] == [f.field for f in unnamed.field_errors] == ["entity"]


# ── The HTTP backstop for any other writer that reaches the database ───────────────────────────────────────────────────

def _integrity_error(constraint: str):
    from asyncpg.exceptions import UniqueViolationError
    from sqlalchemy.exc import IntegrityError

    cause = UniqueViolationError("duplicate key value violates unique constraint")
    cause.constraint_name = constraint
    orig = Exception("wrapped by the SQLAlchemy asyncpg dialect")
    orig.__cause__ = cause
    return IntegrityError("INSERT INTO dc_pipelines ...", {}, orig)


def _post_pipeline(monkeypatch, constraint: str):
    from conftest import DEFAULT_GATEWAY_HEADERS
    from fastapi.testclient import TestClient

    from app.main import app

    async def _boom(org_id, payload):
        raise _integrity_error(constraint)

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.create_pipeline", _boom)
    body = {"name": "p", "template_id": "t", "connection_profile_id": "c", "zone": "raw", "domain": "d", "source_system": "s", "entity": "e", "mode": "batch"}
    return TestClient(app, headers=DEFAULT_GATEWAY_HEADERS, raise_server_exceptions=False).post(
        "/api/v1/dataconnect/pipelines", json=body, headers={"X-Tenant-Id": "org_1"})


def test_a_dead_letter_conflict_that_reaches_the_http_layer_is_a_409_that_explains_itself(monkeypatch) -> None:
    response = _post_pipeline(monkeypatch, DEAD_LETTER_TOPIC_INDEX)

    assert response.status_code == 409
    body = response.json()
    assert "dead-letter queue" in body["message"]
    assert body["field_errors"][0]["field"] == "entity"
    assert body["retryable"] is False


def test_the_existing_identity_conflict_is_still_explained_as_before(monkeypatch) -> None:
    response = _post_pipeline(monkeypatch, "dc_pipelines_org_id_connector_name_key")

    assert response.status_code == 409
    assert "already exists" in response.json()["message"]
    assert "dead-letter" not in response.json()["message"]


def test_an_unrelated_unique_violation_does_not_get_the_dead_letter_explanation(monkeypatch) -> None:
    response = _post_pipeline(monkeypatch, "some_other_unique_key")

    assert "dead-letter" not in response.text
