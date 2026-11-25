"""
BUG-09: quarantine_sink="object_storage" is a real, named value the model
and API already recognized, but ObjectStorageQuarantineSink.send()
(app/services/quarantine_sink.py) unconditionally raises NotImplementedError
— there's no object-storage client/bucket/credential convention in this
codebase at all. A policy must never be allowed to select it until that
backend exists: validate_quality_policy_fields (app/services/quality.py)
rejects it at policy create/update time, before a policy is ever saved,
rather than only failing the first time a real batch execution tries to
quarantine a record through it.
"""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.core.errors import QualityRuleError
from app.main import app
from app.services.quality import validate_quality_policy_fields
from app.services.quality_service import quality_service

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


def test_validate_quality_policy_fields_rejects_object_storage() -> None:
    errors = validate_quality_policy_fields({"quarantine_sink": "object_storage", "rules": []})
    assert errors
    field, message = errors[0]
    assert field == "quarantine_sink"
    assert "reserved" in message.lower()
    assert "implemented" in message.lower()


def test_validate_quality_policy_fields_accepts_kafka_and_the_default() -> None:
    assert validate_quality_policy_fields({"quarantine_sink": "kafka", "rules": []}) == []
    assert validate_quality_policy_fields({"rules": []}) == []


def test_validate_quality_policy_fields_still_rejects_a_genuinely_unknown_sink() -> None:
    """Distinct code path from the object_storage case above — a typo/bad
    input gets the generic "unsupported" message, not the "reserved for a
    future backend" one, since those are different problems."""
    errors = validate_quality_policy_fields({"quarantine_sink": "smb_share", "rules": []})
    assert errors
    field, message = errors[0]
    assert field == "quarantine_sink"
    assert "unsupported" in message.lower()


@pytest.mark.asyncio
async def test_create_policy_rejects_object_storage_before_saving_anything(fake_postgres) -> None:
    with pytest.raises(QualityRuleError):
        await quality_service.create_policy(
            "org_bug09_1", {"name": "p", "quarantine_sink": "object_storage", "rules": []},
        )
    assert fake_postgres.quality_policies == {}


@pytest.mark.asyncio
async def test_update_policy_rejects_object_storage_without_touching_the_stored_row(fake_postgres) -> None:
    created = await quality_service.create_policy(
        "org_bug09_2", {"name": "p", "quarantine_sink": "kafka", "rules": []},
    )

    with pytest.raises(QualityRuleError):
        await quality_service.update_policy(
            "org_bug09_2", created["id"], {"name": "p", "quarantine_sink": "object_storage", "rules": []},
        )

    stored = fake_postgres.quality_policies[created["id"]]
    assert stored.quarantine_sink == "kafka"


def test_create_quality_policy_route_rejects_object_storage() -> None:
    resp = client.post(
        "/api/v1/dataconnect/quality-policies",
        headers={"X-Tenant-Id": "org_bug09_3"},
        json={"name": "p", "quarantine_sink": "object_storage", "rules": []},
    )
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"].lower()
    assert "reserved" in message
    assert "implemented" in message


# ── defense in depth: the database enforces the same rule ───────────────────────────────────────────

def test_the_database_constraint_allows_exactly_the_sinks_the_api_accepts() -> None:
    """If a sink is implemented, BOTH the API's QUARANTINE_SINKS and this constraint (new migration) must widen."""
    import re

    from sqlalchemy import CheckConstraint

    from app.models.quality import QualityPolicy
    from app.services.quality import QUARANTINE_SINKS

    constraints = [c for c in QualityPolicy.__table__.constraints if isinstance(c, CheckConstraint)]
    named = {c.name: str(c.sqltext) for c in constraints}

    assert "ck_dc_quality_policies_quarantine_sink_implemented" in named
    allowed = set(re.findall(r"'([a-z_]+)'", named["ck_dc_quality_policies_quarantine_sink_implemented"]))
    assert allowed == QUARANTINE_SINKS
    assert "object_storage" not in allowed
