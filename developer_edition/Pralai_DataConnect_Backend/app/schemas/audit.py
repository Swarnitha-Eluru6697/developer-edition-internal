from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogEntry(BaseModel):
    id: str
    org_id: str | None
    actor: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    created_at: datetime
