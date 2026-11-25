"""Every list the UI shows (connections, pipelines, schemas, quality policies, SMT chains) says when each item was created."""

from datetime import UTC, datetime

from app.schemas.connection import ConnectionProfile
from app.schemas.pipeline import Pipeline
from app.schemas.quality import QualityPolicy
from app.schemas.schema import SchemaVersion
from app.schemas.transform import TransformPolicy
from app.services.quality_service import _policy_to_dict as quality_to_dict
from app.services.schema_service import _schema_to_dict
from app.services.transform_service import _policy_to_dict as transform_to_dict

AT = datetime(2026, 10, 22, 7, 30, tzinfo=UTC)


class _Row:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def test_each_response_model_carries_created_at_and_it_is_optional():
    for model in (ConnectionProfile, Pipeline, QualityPolicy, SchemaVersion, TransformPolicy):
        field = model.model_fields["created_at"]
        assert field.is_required() is False, model.__name__


def test_the_row_to_dict_helpers_pass_the_stored_time_through():
    schema = _schema_to_dict(_Row(id="s", org_id="o", domain="d", source_system="s", entity="e", version="v1", schema_format="x", schema_definition={},
                                  compatibility="BACKWARD", status="active", created_at=AT))
    assert schema["created_at"] == AT
    quality = quality_to_dict(_Row(id="q", org_id="o", name="n", description=None, rules=[], on_failure="log", quarantine_sink="kafka", created_at=AT))
    assert quality["created_at"] == AT
    transform = transform_to_dict(_Row(id="t", org_id="o", name="n", description=None, transforms=[], origin="custom", created_at=AT))
    assert transform["created_at"] == AT
    assert TransformPolicy(**transform).created_at == AT          # and the API model keeps it
