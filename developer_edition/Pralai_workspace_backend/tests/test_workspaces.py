"""Smoke tests for workspace + file CRUD.

These tests exercise the round-trip: create a workspace, create a file,
read it back, update it, delete it. Storage backend is `local` (a temp
directory) for tests.

Integration tests: they need a real, migrated Postgres plus Redis and an
S3-compatible store (the models use Postgres-only types), so they run when
INTEGRATION_DATABASE_URL is set — the `integration` CI job does that.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.mark.integration
def test_create_and_read_workspace(client, auth_headers):
    r = client.post(
        "/api/v1/workspaces",
        json={"name": f"test-ws-{uuid.uuid4().hex[:8]}", "description": "round-trip test"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    ws = r.json()
    assert ws["name"].startswith("test-ws-")
    assert ws["workspace_type"] == "personal"

    r2 = client.get(f"/api/v1/workspaces/{ws['id']}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == ws["id"]


@pytest.mark.integration
def test_file_create_then_optimistic_update(client, auth_headers):
    # Create a workspace
    ws = client.post(
        "/api/v1/workspaces",
        json={"name": f"files-test-{uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    ).json()

    # Create a file
    f = client.post(
        f"/api/v1/workspaces/{ws['id']}/files",
        json={
            "path": "models/staging/stg_orders.sql",
            "language": "sql",
            "content": "select 1 as id",
        },
        headers=auth_headers,
    ).json()

    # Update with the right hash succeeds
    upd = client.put(
        f"/api/v1/workspaces/{ws['id']}/files/{f['id']}",
        json={"content": "select 2 as id", "if_match_sha256": f["sha256"]},
        headers=auth_headers,
    )
    assert upd.status_code == 200

    # Update with the now-stale hash is refused as a conflict
    stale = client.put(
        f"/api/v1/workspaces/{ws['id']}/files/{f['id']}",
        json={"content": "select 3", "if_match_sha256": f["sha256"]},
        headers=auth_headers,
    )
    assert stale.status_code == 409


@pytest.mark.integration
def test_disallowed_language_rejected(client, auth_headers):
    ws = client.post(
        "/api/v1/workspaces",
        json={"name": f"lang-test-{uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    ).json()
    r = client.post(
        f"/api/v1/workspaces/{ws['id']}/files",
        json={"path": "script.py", "language": "python", "content": "print(1)"},
        headers=auth_headers,
    )
    # Phase 1: only sql + ipynb
    assert r.status_code in (400, 422)
