"""
The local-development principal (app/core/gateway.py) — the replacement for the old X-User-Role header bypass.

What must hold:
  * it exists only with DATACONNECT_ENV=development, TRUST_ENVOY_HEADERS not "true", and DATACONNECT_DEV_PERMISSIONS set;
  * it never reads a request header — a forged x-user-role / x-user-permissions changes nothing;
  * startup refuses DATACONNECT_DEV_PERMISSIONS outside development, and refuses the dev principal when
    DATACONNECT_ENV was never set explicitly (BUG-04);
  * without DATACONNECT_DEV_PERMISSIONS a development process is deny-by-default like any other (401).

The mode is chosen when the app is built, from settings fixed at import time, and conftest.py sets
TRUST_ENVOY_HEADERS=true for the whole suite — so each case runs in a fresh subprocess with its own environment.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HTTP_SCRIPT = """
import json
from fastapi.testclient import TestClient
from app.main import create_app

client = TestClient(create_app())
forged = {"X-User-Role": "DataConnect Admin", "X-User-Roles": "admin",
          "X-User-Permissions": "dataconnect:templates:manage", "X-User-Name": "mallory", "X-Tenant-Id": "other-org"}
print(json.dumps({
    "me": client.get("/api/v1/dataconnect/me").json(),
    "me_forged": client.get("/api/v1/dataconnect/me", headers=forged).json(),
    "me_status": client.get("/api/v1/dataconnect/me").status_code,
    "health": client.get("/api/v1/dataconnect/health").status_code,
}))
"""

_GUARD_SCRIPT = "from app.core.gateway import assert_explicit_environment; assert_explicit_environment(); print('started')"


def _run_python(script: str, *, env_overrides: dict[str, str | None], cwd: Path = _REPO_ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("TRUST_ENVOY_HEADERS", "DATACONNECT_ENV", "DATACONNECT_DEV_PERMISSIONS", "DATACONNECT_DEV_USER", "DATACONNECT_DEV_TENANT"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


def _http(env_overrides: dict[str, str | None]) -> dict:
    result = _run_python(_HTTP_SCRIPT, env_overrides=env_overrides)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_dev_principal_holds_exactly_the_configured_permissions_and_ignores_forged_headers() -> None:
    out = _http({
        "DATACONNECT_ENV": "development",
        "DATACONNECT_DEV_PERMISSIONS": "dataconnect:access:view,dataconnect:connections:view,not:a:real-one",
        "DATACONNECT_DEV_USER": "dev-alice", "DATACONNECT_DEV_TENANT": "org_1001",
    })
    expected = {"user": "dev-alice", "userId": "dev-alice", "tenant": "org_1001",
                "permissions": ["dataconnect:access:view", "dataconnect:connections:view"]}
    assert out["me"] == expected
    assert out["me_forged"] == expected  # no request header changes who the caller is or what they may do
    assert out["health"] == 200


def test_dev_principal_all_expands_to_every_declared_permission() -> None:
    out = _http({"DATACONNECT_ENV": "development", "DATACONNECT_DEV_PERMISSIONS": "all"})
    declared = sorted(p["id"] for p in json.loads((_REPO_ROOT / "permissions.json").read_text())["permissions"])
    assert out["me"]["permissions"] == declared


def test_development_without_dev_permissions_is_deny_by_default() -> None:
    out = _http({"DATACONNECT_ENV": "development"})
    assert out["me_status"] == 401
    assert out["health"] == 200


def test_envoy_wins_over_dev_permissions_when_trust_is_on() -> None:
    out = _http({"DATACONNECT_ENV": "development", "DATACONNECT_DEV_PERMISSIONS": "all", "TRUST_ENVOY_HEADERS": "true"})
    assert out["me_status"] == 401  # no gateway secret: the dev principal is not used behind Envoy


def test_dev_permissions_refuse_to_start_outside_development(tmp_path) -> None:
    for env in ("staging", "production"):
        result = _run_python(
            _GUARD_SCRIPT, env_overrides={"DATACONNECT_ENV": env, "DATACONNECT_DEV_PERMISSIONS": "all"}, cwd=tmp_path,
        )
        assert result.returncode != 0, env
        assert "DATACONNECT_DEV_PERMISSIONS" in result.stderr


def test_dev_principal_refuses_to_start_when_the_environment_was_never_set(tmp_path) -> None:
    # tmp_path has no .env, so DATACONNECT_ENV is genuinely absent and settings.env is only its default.
    result = _run_python(_GUARD_SCRIPT, env_overrides={"DATACONNECT_DEV_PERMISSIONS": "all"}, cwd=tmp_path)
    assert result.returncode != 0
    assert "Refusing to start with the development principal" in result.stderr


def test_dev_principal_starts_when_the_environment_is_set_explicitly(tmp_path) -> None:
    result = _run_python(
        _GUARD_SCRIPT, env_overrides={"DATACONNECT_ENV": "development", "DATACONNECT_DEV_PERMISSIONS": "all"}, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout


def test_dev_principal_starts_when_the_environment_comes_from_the_env_file(tmp_path) -> None:
    (tmp_path / ".env").write_text("DATACONNECT_ENV=development\nDATACONNECT_DEV_PERMISSIONS=all\n")
    result = _run_python(_GUARD_SCRIPT, env_overrides={}, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_the_guard_does_nothing_without_dev_permissions_even_if_the_environment_was_never_set(tmp_path) -> None:
    """No development principal requested: nothing to guard against (every request needs Envoy)."""
    result = _run_python(_GUARD_SCRIPT, env_overrides={}, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
