from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Timestamped(BaseModel):
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApiMessage(BaseModel):
    message: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class JsonObject(BaseModel):
    data: dict[str, Any]


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

