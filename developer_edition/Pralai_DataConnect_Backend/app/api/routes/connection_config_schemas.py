"""Field schemas for ConnectionProfile.connection_config, per source_type —
the same metadata (see app/services/connection_config_schema.py) that drives
backend validation and redaction, exposed so the UI can render the right
form for a given source type instead of guessing at field names."""

from fastapi import APIRouter, Depends
from gateway_auth import Principal

from app.core.permissions import require
from app.schemas.connection import SOURCE_TYPES, ConnectionConfigSchema
from app.services.connection_config_schema import schema_for_source_type

router = APIRouter()


@router.get("", response_model=list[ConnectionConfigSchema], summary="Field schemas for every source type")
def list_connection_config_schemas(
    # FE-BUG-03: every route states its required permission explicitly
    # rather than being reachable by omission.
    _principal: Principal = Depends(require("dataconnect:connections:view")),
) -> list[dict]:
    return [
        {"source_type": source_type, "fields": schema_for_source_type(source_type)}
        for source_type in sorted(SOURCE_TYPES)
    ]


@router.get(
    "/{source_type}", response_model=ConnectionConfigSchema, summary="Field schema for one source type"
)
def get_connection_config_schema(
    source_type: str,
    _principal: Principal = Depends(require("dataconnect:connections:view")),
) -> dict:
    normalized = source_type.lower()
    return {"source_type": normalized, "fields": schema_for_source_type(normalized)}
