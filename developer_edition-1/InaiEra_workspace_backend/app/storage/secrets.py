"""SecretsBackend: the customer cloud secret manager, chosen via SECRETS_BACKEND.

Mirrors the BlobStore pattern (app/storage/base.py + the factory in
app/storage/__init__.py): callers store/read a credential by a path, and
only that path — never the secret value — is persisted in the metadata
database (DatabaseConnection.vault_secret_path, GitCredential.vault_path).

`local` is a dev-only fallback that keeps writing to
WORKSPACE_GIT_ROOT/.secrets-store.json (still plaintext, but scoped to this
module only) — startup_guard() below refuses to run it in staging/prod.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

from app.core.config import get_settings

log = structlog.get_logger()


class SecretsBackend(Protocol):
    async def get(self, path: str) -> dict | None:
        """Return the secret's fields, or None if it doesn't exist."""
        ...

    async def put(self, path: str, data: dict) -> None:
        """Create or overwrite the secret at `path`."""
        ...

    async def delete(self, path: str) -> None:
        """Delete the secret at `path`. No-op if it doesn't exist."""
        ...


def _safe_secret_name(path: str) -> str:
    """Azure Key Vault and GCP Secret Manager restrict secret names to
    letters, numbers, hyphens (GCP also allows underscores) — our paths use
    '/' and ':' as separators, so translate them into a name that stays
    unique per path instead of failing at the API call."""
    return path.replace("/", "--").replace(":", "-")


class AWSSecretsManagerBackend:
    def __init__(self) -> None:
        import boto3
        settings = get_settings()
        self._client = boto3.client("secretsmanager", region_name=settings.storage_region)

    async def get(self, path: str) -> dict | None:
        import json
        from botocore.exceptions import ClientError
        try:
            resp = await asyncio.to_thread(self._client.get_secret_value, SecretId=path)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("ResourceNotFoundException",):
                return None
            raise
        return json.loads(resp["SecretString"])

    async def put(self, path: str, data: dict) -> None:
        import json
        from botocore.exceptions import ClientError
        secret_string = json.dumps(data)
        try:
            await asyncio.to_thread(
                self._client.create_secret, Name=path, SecretString=secret_string,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceExistsException":
                await asyncio.to_thread(
                    self._client.put_secret_value, SecretId=path, SecretString=secret_string,
                )
            else:
                raise

    async def delete(self, path: str) -> None:
        from botocore.exceptions import ClientError
        try:
            await asyncio.to_thread(
                self._client.delete_secret, SecretId=path, ForceDeleteWithoutRecovery=True,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise


class AzureKeyVaultBackend:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.secrets_azure_vault_url:
            raise RuntimeError(
                "SECRETS_AZURE_VAULT_URL required for azure_key_vault secrets backend"
            )
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient
        self._client = SecretClient(
            vault_url=settings.secrets_azure_vault_url,
            credential=DefaultAzureCredential(),
        )

    async def get(self, path: str) -> dict | None:
        import json
        from azure.core.exceptions import ResourceNotFoundError
        try:
            secret = await self._client.get_secret(_safe_secret_name(path))
        except ResourceNotFoundError:
            return None
        return json.loads(secret.value)

    async def put(self, path: str, data: dict) -> None:
        import json
        await self._client.set_secret(_safe_secret_name(path), json.dumps(data))

    async def delete(self, path: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            poller = await self._client.begin_delete_secret(_safe_secret_name(path))
            await poller.wait()
        except ResourceNotFoundError:
            pass


class GCPSecretManagerBackend:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.secrets_gcp_project_id:
            raise RuntimeError(
                "SECRETS_GCP_PROJECT_ID required for gcp_secret_manager secrets backend"
            )
        from google.cloud import secretmanager
        self._client = secretmanager.SecretManagerServiceClient()
        self._project_id = settings.secrets_gcp_project_id

    def _secret_id(self, path: str) -> str:
        return _safe_secret_name(path)

    def _secret_name(self, path: str) -> str:
        return f"projects/{self._project_id}/secrets/{self._secret_id(path)}"

    async def get(self, path: str) -> dict | None:
        import json
        from google.api_core.exceptions import NotFound
        try:
            resp = await asyncio.to_thread(
                self._client.access_secret_version,
                name=f"{self._secret_name(path)}/versions/latest",
            )
        except NotFound:
            return None
        return json.loads(resp.payload.data.decode("utf-8"))

    async def put(self, path: str, data: dict) -> None:
        import json
        from google.api_core.exceptions import AlreadyExists, NotFound
        secret_id = self._secret_id(path)
        try:
            await asyncio.to_thread(
                self._client.get_secret, name=self._secret_name(path),
            )
        except NotFound:
            try:
                await asyncio.to_thread(
                    self._client.create_secret,
                    parent=f"projects/{self._project_id}",
                    secret_id=secret_id,
                    secret={"replication": {"automatic": {}}},
                )
            except AlreadyExists:
                pass
        await asyncio.to_thread(
            self._client.add_secret_version,
            parent=self._secret_name(path),
            payload={"data": json.dumps(data).encode("utf-8")},
        )

    async def delete(self, path: str) -> None:
        from google.api_core.exceptions import NotFound
        try:
            await asyncio.to_thread(self._client.delete_secret, name=self._secret_name(path))
        except NotFound:
            pass


class LocalSecretsBackend:
    """Dev-only fallback. Never used in staging/prod — see startup_guard()."""

    def __init__(self) -> None:
        import os
        from pathlib import Path
        base = Path(os.environ.get("WORKSPACE_GIT_ROOT", "/var/lib/workspace/git"))
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / ".secrets-store.json"

    def _read_all(self) -> dict:
        import json
        if not self._file.exists():
            return {}
        try:
            return json.loads(self._file.read_text())
        except Exception:
            return {}

    def _write_all(self, data: dict) -> None:
        import json
        self._file.write_text(json.dumps(data))

    async def get(self, path: str) -> dict | None:
        return self._read_all().get(path)

    async def put(self, path: str, data: dict) -> None:
        all_data = self._read_all()
        all_data[path] = data
        self._write_all(all_data)

    async def delete(self, path: str) -> None:
        all_data = self._read_all()
        if path in all_data:
            del all_data[path]
            self._write_all(all_data)


_backend: SecretsBackend | None = None


def get_secrets_backend() -> SecretsBackend:
    global _backend
    if _backend is None:
        kind = get_settings().secrets_backend
        if kind == "aws_secrets_manager":
            _backend = AWSSecretsManagerBackend()
        elif kind == "azure_key_vault":
            _backend = AzureKeyVaultBackend()
        elif kind == "gcp_secret_manager":
            _backend = GCPSecretManagerBackend()
        elif kind == "local":
            _backend = LocalSecretsBackend()
        else:
            raise ValueError(f"Unknown secrets backend: {kind}")
    return _backend


def assert_secrets_backend_safe_for_environment() -> None:
    """Startup guard: the plaintext-on-disk fallback must never run outside
    local dev — a pod-local emptyDir is not a substitute for the customer's
    cloud secret manager (it's unencrypted-at-rest by our own code, per-replica,
    and lost on restart)."""
    settings = get_settings()
    if settings.secrets_backend == "local" and settings.environment in ("staging", "prod"):
        raise RuntimeError(
            "secrets_backend=local is not permitted when environment is "
            f"'{settings.environment}'. Configure SECRETS_BACKEND to "
            "aws_secrets_manager, azure_key_vault, or gcp_secret_manager."
        )


def git_credential_path(user_id: str, provider: str) -> str:
    """Canonical secret path for a stored Git PAT. Kept in one place so
    save/load/migrate never disagree with the path recorded on
    GitCredential.vault_path (see app/api/git.py:store_pat)."""
    return f"inaiera/git/{user_id}/{provider}"


async def migrate_legacy_plaintext_credentials(base_dir: str | None = None) -> dict:
    """Move credentials still sitting in the legacy plaintext files (written
    by pre-fix code) into the configured secrets backend.

    Runs at startup, and offline via `python -m app.storage.migrate_credentials
    --dir <path>` for files recovered from wherever old pods kept them (the
    old emptyDir volumes don't survive a rollout, so the startup run alone
    can't be relied on). A file is removed only once EVERY entry in it was
    migrated; on partial failure it is rewritten with just the entries that
    failed, so nothing is ever lost and a re-run retries only those. An
    unreadable file is left untouched. No-op while the backend is `local`.
    Returns per-file counts.
    """
    import json
    import os
    from pathlib import Path

    settings = get_settings()
    if settings.secrets_backend == "local":
        return {}

    backend = get_secrets_backend()
    base = Path(base_dir or os.environ.get("WORKSPACE_GIT_ROOT", "/var/lib/workspace/git"))
    report: dict = {}

    async def migrate_file(path: Path, kind: str, to_secret) -> None:
        if not path.exists():
            return
        try:
            entries = json.loads(path.read_text())
            if not isinstance(entries, dict):
                raise ValueError("expected a JSON object")
        except Exception as e:  # noqa: BLE001
            log.error(f"secrets_migration_{kind}_read_failed", error=str(e))
            report[kind] = {"error": "unreadable; left in place"}
            return
        failed: dict = {}
        migrated = 0
        for key, value in entries.items():
            target = to_secret(key, value)
            if target is None:
                failed[key] = value        # malformed key: keep for a human
                continue
            secret_path, data = target
            try:
                await backend.put(secret_path, data)
                migrated += 1
            except Exception as e:  # noqa: BLE001
                log.error(f"secrets_migration_{kind}_entry_failed", path=secret_path, error=str(e))
                failed[key] = value
        if failed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(failed))
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)
        report[kind] = {"migrated": migrated, "remaining": len(failed)}
        log.info(f"secrets_migration_{kind}_done", migrated=migrated, remaining=len(failed))

    def db_entry(vault_path, creds):
        return (vault_path, creds) if isinstance(creds, dict) else None

    def git_entry(key, token):
        user_id, sep, provider = str(key).partition(":")
        return (git_credential_path(user_id, provider), {"token": token}) if sep else None

    await migrate_file(base / ".db-credentials.json", "db", db_entry)
    await migrate_file(base / ".git-credentials.json", "git", git_entry)
    return report
