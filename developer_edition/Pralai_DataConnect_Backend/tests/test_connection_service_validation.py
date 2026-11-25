from uuid import uuid4

import pytest

import app.services.connection_service as connection_service_module
from app.core.errors import ConnectorConfigError
from app.services.connection_service import connection_service

# connection_service is now Postgres-backed (see connection_service_module's
# AsyncSessionLocal/ConnectionProfileRepository usage) — same limitation as
# every other Postgres-backed write introduced this session: there is no
# live Postgres in this sandbox, so calls that would reach the database are
# exercised against a fake session/repository rather than a real one,
# consistent with tests/test_audit_core.py's test_record_audit_persists_a_row.


class _FakeRow:
    def __init__(self, values: dict) -> None:
        self.id = None
        self.org_id = None
        self.name = None
        self.source_type = None
        self.connection_config = None
        self.secret_ref = None
        self.network_profile = None
        self.config_revision = 1
        self.created_at = None
        self.__dict__.update(values)


class _FakeConnectionProfileRepository:
    _rows: dict[str, _FakeRow] = {}

    def __init__(self, session) -> None:
        pass

    async def create(self, values: dict) -> _FakeRow:
        values = dict(values)
        values.setdefault("id", str(uuid4()))
        row = _FakeRow(values)
        _FakeConnectionProfileRepository._rows[row.id] = row
        return row

    async def get(self, item_id: str) -> _FakeRow | None:
        return _FakeConnectionProfileRepository._rows.get(item_id)

    async def get_for_org(self, org_id: str, item_id: str) -> _FakeRow | None:
        row = _FakeConnectionProfileRepository._rows.get(item_id)
        if not row or row.org_id != org_id:
            return None
        return row

    async def update(self, row: _FakeRow, values: dict) -> _FakeRow:
        row.__dict__.update(values)
        return row


class _FakeConnectionVerificationRepository:
    """No connection in these tests has ever been probed, so there is never a verification record."""

    def __init__(self, session) -> None:
        pass

    async def get_many(self, connection_ids: list[str]) -> dict:
        return {}


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        pass


class _FakeSecretStore:
    """connection_service.py now also writes through
    app.services.secret_store on create/update (GAP-01) — faked the same
    way ConnectionProfileRepository is, since there's no live Postgres here
    either."""


    _by_connection: dict[str, dict] = {}

    async def put(self, *, org_id: str, connection_id: str, fields: dict) -> str:
        _FakeSecretStore._by_connection[connection_id] = fields
        return f"secret://dataconnect/connections/{connection_id}"

    async def put_in_session(self, session, *, org_id: str, connection_id: str, fields: dict) -> str:
        return await self.put(org_id=org_id, connection_id=connection_id, fields=fields)

    async def get(self, secret_ref: str | None) -> dict | None:
        if not secret_ref:
            return None
        connection_id = secret_ref.rsplit("/", 1)[-1]
        return _FakeSecretStore._by_connection.get(connection_id)

    async def delete(self, secret_ref: str | None) -> None:
        if not secret_ref:
            return
        connection_id = secret_ref.rsplit("/", 1)[-1]
        _FakeSecretStore._by_connection.pop(connection_id, None)

    async def delete_in_session(self, session: object, secret_ref: str | None) -> None:
        await self.delete(secret_ref)


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    monkeypatch.setattr(connection_service_module, "ConnectionProfileRepository", _FakeConnectionProfileRepository)
    monkeypatch.setattr(connection_service_module, "ConnectionVerificationRepository", _FakeConnectionVerificationRepository)
    monkeypatch.setattr(connection_service_module, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(connection_service_module, "secret_store", _FakeSecretStore())
    _FakeConnectionProfileRepository._rows.clear()
    _FakeSecretStore._by_connection.clear()


async def test_create_connection_rejects_missing_required_field() -> None:
    with pytest.raises(ConnectorConfigError) as exc_info:
        await connection_service.create_connection(
            "org_conn_val_1",
            {
                "name": "Bad Postgres",
                "source_type": "postgres",
                "connection_config": {"hostname": "db.internal", "database": "sales"},  # no username/password
            },
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "connector_config_error"
    assert "username" in exc_info.value.message or "password" in exc_info.value.message
    field_names = {fe.field for fe in exc_info.value.field_errors}
    assert field_names == {"connection_config.username", "connection_config.password"}


async def test_create_connection_accepts_valid_config() -> None:
    created = await connection_service.create_connection(
        "org_conn_val_2",
        {
            "name": "Good Postgres",
            "source_type": "postgres",
            "connection_config": {
                "hostname": "db.internal", "database": "sales",
                "username": "svc", "password": "hunter2",
            },
        },
    )

    assert created["name"] == "Good Postgres"


async def test_update_connection_rejects_invalid_config() -> None:
    created = await connection_service.create_connection(
        "org_conn_val_3",
        {
            "name": "S3 Source",
            "source_type": "s3",
            "connection_config": {"bucket": "my-bucket", "access_key_id": "AKIA", "secret_access_key": "s3cret"},
        },
    )

    with pytest.raises(ConnectorConfigError) as exc_info:
        await connection_service.update_connection(
            "org_conn_val_3",
            created["id"],
            {"name": "S3 Source", "source_type": "s3", "connection_config": {}},  # bucket now missing
        )

    assert exc_info.value.field_errors[0].field == "connection_config.bucket"

    assert exc_info.value.status_code == 422
    assert "bucket" in exc_info.value.message
