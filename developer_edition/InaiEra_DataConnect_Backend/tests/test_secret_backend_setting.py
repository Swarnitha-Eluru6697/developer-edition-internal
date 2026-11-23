"""
GAP-01: `secret_backend=vault` used to be accepted while nothing stored a secret in Vault, so credentials silently went to PostgreSQL.
It is now rejected when settings load, so a deployment that asks for Vault fails at startup instead of believing it has it.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize("backend", ["vault", "VAULT", " vault "])
def test_the_vault_backend_is_rejected_with_a_clear_reason(backend) -> None:
    with pytest.raises(ValidationError, match="'vault' is not implemented"):
        Settings(secret_backend=backend)


@pytest.mark.parametrize("backend", ["env", "postgres", "Postgres"])
def test_env_and_postgres_are_the_supported_backends(backend) -> None:
    assert Settings(secret_backend=backend).secret_backend in {"env", "postgres"}


def test_an_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be one of: env, postgres"):
        Settings(secret_backend="kms")


def test_a_real_process_configured_for_vault_refuses_to_start() -> None:
    """Not just a validator unit test: the actual settings load, as the app does at startup, exits non-zero and says why."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings"],
        # The child does not inherit pytest's import path, so name the repo root explicitly (not "whatever the cwd happens to be").
        cwd=repo_root, env={**os.environ, "PYTHONPATH": str(repo_root), "DATACONNECT_SECRET_BACKEND": "vault"},
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode != 0
    assert "secret_backend 'vault' is not implemented" in result.stderr
    assert "PostgreSQL" in result.stderr
