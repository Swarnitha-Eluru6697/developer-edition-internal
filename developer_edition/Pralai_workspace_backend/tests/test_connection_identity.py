"""W-011: a connection gets a persisted, canonical UCS identity when it is created or (re)linked — and renaming never moves it."""
from __future__ import annotations

import pytest
from conftest import _headers
from sqlalchemy.exc import IntegrityError
from test_connection_secrets import BODY, FakeBackend, FakeDB, env  # noqa: F401  (fixtures)

from app.api import catalog as catalog_api
from app.services.ucs_client import UcsUnavailableError

ADMIN = {"roles": ["workspace_admin"]}
TREE = {"pg_sales": {"engine": "relational", "connection": {"host": "other", "port": 5432, "database": "x", "user": "y"}},
        "lake": {"engine": "iceberg"}}


class _PgUnique(Exception):
    """What asyncpg raises for a unique violation: SQLSTATE 23505 and the constraint's name."""
    sqlstate = "23505"

    def __init__(self, constraint_name):
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


@pytest.fixture
def ucs(monkeypatch):
    state = {"tree": TREE, "down": False}

    async def tree(principal):
        if state["down"]:
            raise UcsUnavailableError("down")
        return state["tree"]

    monkeypatch.setattr(catalog_api, "get_explorer_tree", tree)
    return state


def _create(client, **over):
    return client.post("/api/v1/connections", json={**BODY, **over}, headers=_headers(**ADMIN))


def test_an_unlinked_connection_is_governed_by_its_own_immutable_id(env, ucs):
    client, rows, *_ = env
    r = _create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["catalog_name"] is None and body["ucs_resource_fqn"] == f"inaiera_workspace.connection.{body['id']}"


def test_an_explicit_catalog_link_must_exist_in_ucs_and_becomes_the_identity(env, ucs):
    client, *_ = env
    r = _create(client, catalog_name="pg_sales")
    assert r.status_code == 201 and r.json()["ucs_resource_fqn"] == "pg_sales"


def test_a_catalog_that_ucs_does_not_have_is_refused_so_nobody_borrows_its_grants(env, ucs):
    client, rows, backend, _ = env
    r = _create(client, catalog_name="catalog_i_do_not_own")
    assert r.status_code == 400 and "catalog_name" in r.text
    assert rows == {} and backend.data == {}                # nothing created, no secret written


def test_an_explicit_link_cannot_be_verified_while_ucs_is_down_so_it_is_refused(env, ucs):
    client, rows, *_ = env
    ucs["down"] = True
    r = _create(client, catalog_name="pg_sales")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "UCS_UNAVAILABLE" and rows == {}


def test_an_unlinked_create_still_works_while_ucs_is_down(env, ucs):
    client, *_ = env
    ucs["down"] = True
    assert _create(client).status_code == 201


def test_renaming_keeps_the_governed_identity(env, ucs):
    client, rows, *_ = env
    created = _create(client).json()
    r = client.patch(f"/api/v1/connections/{created['id']}", json={"name": "renamed"}, headers=_headers(**ADMIN))
    assert r.status_code == 200 and r.json()["name"] == "renamed"
    assert r.json()["ucs_resource_fqn"] == created["ucs_resource_fqn"]


def test_changing_the_link_changes_the_identity_and_clearing_it_returns_to_the_id_form(env, ucs):
    client, *_ = env
    created = _create(client).json()
    r = client.patch(f"/api/v1/connections/{created['id']}", json={"catalog_name": "pg_sales"}, headers=_headers(**ADMIN))
    assert r.json()["ucs_resource_fqn"] == "pg_sales"
    r = client.patch(f"/api/v1/connections/{created['id']}", json={"catalog_name": ""}, headers=_headers(**ADMIN))
    assert r.json()["ucs_resource_fqn"] == f"inaiera_workspace.connection.{created['id']}"


def test_relinking_to_an_unknown_catalog_is_refused_and_keeps_the_old_identity(env, ucs):
    client, rows, *_ = env
    created = _create(client, catalog_name="pg_sales").json()
    r = client.patch(f"/api/v1/connections/{created['id']}", json={"catalog_name": "nope"}, headers=_headers(**ADMIN))
    assert r.status_code == 400
    assert next(iter(rows.values())).ucs_resource_fqn == "pg_sales"


def test_a_rename_that_loses_a_race_for_the_name_is_a_409_not_a_500(env, ucs, monkeypatch):
    client, rows, backend, db = env
    created = _create(client).json()

    async def racing_flush():
        raise IntegrityError("UPDATE", {}, _PgUnique("uq_dbconn_org_name"))

    monkeypatch.setattr(db, "flush", racing_flush)
    r = client.patch(f"/api/v1/connections/{created['id']}", json={"name": "taken-meanwhile"}, headers=_headers(**ADMIN))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONNECTION_NAME_TAKEN"


# ── against a real database: what fake sessions cannot show ───────────────────
@pytest.mark.integration
def test_renaming_and_updating_a_connection_works_against_a_real_database():
    """Found by the live scenario: a successful update answered 500 (MissingGreenlet serialising the server-set `updated_at`).
    Fake sessions never expire attributes, so only a real database shows it."""
    import os
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from app.main import app

    if not os.environ.get("INTEGRATION_DATABASE_URL"):
        pytest.skip("needs the integration database")
    c = TestClient(app)
    name = f"it-{_uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/connections", json={"name": name, "connection_type": "postgres", "host": "db", "port": 5432, "database_name": "x",
                                           "use_tls": False}, headers=_headers(**ADMIN))
    assert r.status_code == 201, r.text
    cid, fqn = r.json()["id"], r.json()["ucs_resource_fqn"]
    try:
        r = c.patch(f"/api/v1/connections/{cid}", json={"name": name + "-renamed", "description": "d"}, headers=_headers(**ADMIN))
        assert r.status_code == 200, r.text
        assert r.json()["name"] == name + "-renamed" and r.json()["ucs_resource_fqn"] == fqn and r.json()["updated_at"]
        other = c.post("/api/v1/connections", json={"name": name + "-other", "connection_type": "postgres", "host": "db", "port": 5432,
                                                    "database_name": "x", "use_tls": False}, headers=_headers(**ADMIN)).json()
        try:
            clash = c.patch(f"/api/v1/connections/{other['id']}", json={"name": name + "-renamed"}, headers=_headers(**ADMIN))
            assert clash.status_code == 409 and clash.json()["detail"]["code"] == "CONNECTION_NAME_TAKEN"
        finally:
            c.delete(f"/api/v1/connections/{other['id']}", headers=_headers(**ADMIN))
    finally:
        c.delete(f"/api/v1/connections/{cid}", headers=_headers(**ADMIN))
