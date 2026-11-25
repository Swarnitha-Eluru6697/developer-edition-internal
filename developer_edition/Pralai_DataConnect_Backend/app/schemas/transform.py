from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TransformStep(BaseModel):
    order: int
    name: str
    type: str
    scope: str = "value"
    config: dict[str, Any] = Field(default_factory=dict)


class TransformPolicyBase(BaseModel):
    name: str
    description: str | None = None
    transforms: list[TransformStep] = Field(default_factory=list)


TransformOrigin = Literal["custom", "auto"]


class TransformPolicyCreate(TransformPolicyBase):
    id: str | None = None
    # "custom": made with "+ New SMT" (the default); "auto": made by the pipeline form. Fixed at creation: an edit never changes it.
    origin: TransformOrigin = "custom"


class TransformPolicy(TransformPolicyBase):
    id: str
    org_id: str
    # When it was created (response-only, never client-set).
    created_at: datetime | None = None
    origin: TransformOrigin = "custom"


class TransformPreviewRequest(BaseModel):
    input_record: dict[str, Any]


class TransformPreviewResult(BaseModel):
    input_record: dict[str, Any]
    output_record: dict[str, Any]
    generated_config: dict[str, str]

