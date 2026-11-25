"""Machine-readable availability for things a user can choose: connectors, quarantine destinations, failure actions (NEW-09).

Some choices were offered that could not be executed, and nothing said why or whether it could ever work: a connector whose plugin is not
installed, a quarantine destination that is reserved for a future backend, a failure action nobody implemented. A user could not tell "this product
does not do that" from "your environment is not set up for it". Every such choice now carries ONE of three states:

    supported               It works as offered, right now.
    requires_configuration  It is implemented, but this deployment is not set up for it (or that could not be checked): an operator can fix it.
                            The choice is still offered, flagged, because choosing it is not wrong — it just will not run until configured.
    unavailable             It cannot be executed here, and choosing it is refused: the plugin is not installed, the template is switched off,
                            or the capability does not exist yet.

`code` is a stable identifier a UI or script can switch on (it never changes with the wording of `message`); `message` is for people;
`remediation` says what to do about it, when anything can be done. `verified` is False when availability could not be checked live (Kafka Connect
was unreachable) — the state is then never "supported", because that would claim something nobody confirmed.
"""

from dataclasses import dataclass
from enum import StrEnum


class CapabilityState(StrEnum):
    SUPPORTED = "supported"
    REQUIRES_CONFIGURATION = "requires_configuration"
    UNAVAILABLE = "unavailable"


# ── Reason codes (stable; documented in docs/CAPABILITIES.md) ───────────────────────────────────────────────────────────
# unavailable
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"                         # the capability does not exist (yet)
TEMPLATE_INACTIVE = "TEMPLATE_INACTIVE"                     # the connector template has been switched off
PLUGIN_NOT_INSTALLED = "PLUGIN_NOT_INSTALLED"               # its connector class is not installed on the Kafka Connect worker
PLUGIN_WRONG_KIND = "PLUGIN_WRONG_KIND"                     # installed, but as a source when the template is a sink (or the reverse)
PLUGIN_VERSION_INCOMPATIBLE = "PLUGIN_VERSION_INCOMPATIBLE"  # installed, but a different major version or older than the template was qualified for
WORKER_VERSION_UNSUPPORTED = "WORKER_VERSION_UNSUPPORTED"   # the Kafka Connect worker's version is not one the template was qualified for
NO_CONNECTOR_CLASS = "NO_CONNECTOR_CLASS"                     # the catalog entry names no installable connector class (a placeholder)
UNKNOWN_POOL = "UNKNOWN_POOL"                               # the template names a worker pool that does not exist
# requires_configuration
RUNTIME_UNREACHABLE = "RUNTIME_UNREACHABLE"                 # Kafka Connect could not be reached, so nothing about it could be confirmed
CONFIG_PROVIDER_NOT_CONFIGURED = "CONFIG_PROVIDER_NOT_CONFIGURED"
KAFKA_BOOTSTRAP_MISSING = "KAFKA_BOOTSTRAP_MISSING"
KAFKA_REST_PROXY_URL_MISSING = "KAFKA_REST_PROXY_URL_MISSING"


@dataclass(frozen=True)
class Availability:
    state: CapabilityState
    code: str | None = None
    message: str | None = None
    remediation: str | None = None
    verified: bool = True

    @property
    def usable(self) -> bool:
        """Can it be chosen at all? Everything except `unavailable`."""
        return self.state is not CapabilityState.UNAVAILABLE

    def as_dict(self) -> dict[str, str | bool | None]:
        return {"state": self.state.value, "code": self.code, "message": self.message, "remediation": self.remediation, "verified": self.verified}


SUPPORTED = Availability(CapabilityState.SUPPORTED)


def requires_configuration(code: str, message: str, remediation: str | None = None, *, verified: bool = True) -> Availability:
    return Availability(CapabilityState.REQUIRES_CONFIGURATION, code, message, remediation, verified)


def unavailable(code: str, message: str, remediation: str | None = None) -> Availability:
    return Availability(CapabilityState.UNAVAILABLE, code, message, remediation)
