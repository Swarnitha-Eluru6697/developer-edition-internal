"""Transform Policy model — reusable Kafka Connect SMT (Single Message Transform) chains."""

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class TransformPolicy(Base, UuidPk, TimestampMixin):
    """
    An ordered list of Kafka Connect SMT steps to apply to messages in a pipeline.

    Each entry in `transforms` is a dict with:
        order       : int   — execution order (ascending)
        name        : str   — unique step name within the policy
        type        : str   — SMT type, e.g. ValueToKey, MaskField, ExtractField
        scope       : str   — "key" | "value"
        config      : dict  — SMT-specific config parameters

    The pipeline_config service converts this list into the flat
    `transforms.<name>.*` key-value pairs that Kafka Connect expects.
    """

    __tablename__ = "dc_transform_policies"
    __table_args__ = (CheckConstraint("origin IN ('custom', 'auto')", name="ck_dc_transform_policies_origin"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    transforms: Mapped[list] = mapped_column(JSONB, default=list)
    # How it was made: "custom" — created with the Transforms page's "+ New SMT"; "auto" — created by the pipeline form while a pipeline was built.
    origin: Mapped[str] = mapped_column(String(20), default="custom", server_default="custom")
    created_by: Mapped[str | None] = mapped_column(String(255))
