"""Response shapes for the availability model in app/core/capability.py (NEW-09)."""

from typing import Literal

from pydantic import BaseModel, Field


class Availability(BaseModel):
    """Whether a choice can be executed right now, and if not, why — see app/core/capability.py for the three states and the reason codes."""

    state: Literal["supported", "requires_configuration", "unavailable"]
    #: A stable identifier to switch on (e.g. PLUGIN_NOT_INSTALLED). None when the state is "supported".
    code: str | None = None
    message: str | None = None
    #: What to do about it, when anything can be done.
    remediation: str | None = None
    #: False when this could not be checked live (Kafka Connect unreachable); the state is then never "supported".
    verified: bool = True


class CapabilityOption(BaseModel):
    id: str
    label: str
    description: str
    availability: Availability


class QualityCapabilities(BaseModel):
    """What a quality policy can actually do: where failing records can go, and what can be done when a record fails."""

    quarantine_sinks: list[CapabilityOption] = Field(default_factory=list)
    failure_actions: list[CapabilityOption] = Field(default_factory=list)
