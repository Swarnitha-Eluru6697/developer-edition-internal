"""What environment this process is running in.

One definition, because there were already three. `secrets_crypto`,
`error_responses` and `main` each carried their own copy of the same
`os.getenv("ENVIRONMENT", "").strip().lower() == "production"`, and two
of the three carried a comment explaining that it must not drift from
the others — which is the tell. The production defaults added for the
readiness, migration and CORS fixes needed the same predicate in three
more places, so it lives here instead.

Deliberately a function rather than a module-level constant: tests set
ENVIRONMENT with monkeypatch after import, and a constant frozen at
import time would read as production-hardening that silently does not
apply.
"""
from __future__ import annotations

import os


def environment_name() -> str:
    """The declared environment, lowercased. Empty when unset."""
    return os.getenv("ENVIRONMENT", "").strip().lower()


def is_production() -> bool:
    return environment_name() == "production"


def env_flag(name: str, default: bool) -> bool:
    """A boolean environment variable, with production-aware defaults.

    Accepts the spellings an operator actually types. An unrecognised
    value is NOT quietly treated as false: a deployment that set
    `AUTO_MIGRATE=no` and got the default instead of the thing it asked
    for is the failure mode this exists to avoid.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise RuntimeError(
        f"{name}={raw!r} is not a boolean. Use true/false (also accepted: "
        f"1/0, yes/no, on/off). Refusing to guess, because guessing here "
        f"means silently running with the opposite of what was configured."
    )
