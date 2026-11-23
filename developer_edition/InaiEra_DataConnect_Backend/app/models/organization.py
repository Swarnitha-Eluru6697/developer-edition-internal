"""Organization model — top-level tenant entity."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Organization(Base, TimestampMixin):
    """
    Represents a tenant organisation.

    Every data-bearing table foreign-keys to org_id so rows can never
    leak across tenants even when queried without a WHERE clause filter.
    """

    __tablename__ = "dc_organizations"

    org_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="active")
