"""Smoke tests for the storage key derivation — the Snowflake-Workspaces
"personal database" concept maps to a per-user prefix in cloud object
storage. This test pins that mapping so we never accidentally change it.
"""
from app.storage import storage_key_for_file


def test_storage_key_pattern():
    key = storage_key_for_file(
        org_id="acme",
        user_id="alice",
        workspace_id="ws-1",
        branch="main",
        path="models/staging/stg_orders.sql",
    )
    assert key == "acme/alice/ws-1/main/models/staging/stg_orders.sql"


def test_storage_key_normalizes_leading_slash():
    """The 'path' field comes from the user and may have a leading slash;
    the storage layer must normalize so two different inputs map to one key."""
    a = storage_key_for_file(
        org_id="acme", user_id="alice", workspace_id="ws-1",
        branch="main", path="/models/x.sql",
    )
    b = storage_key_for_file(
        org_id="acme", user_id="alice", workspace_id="ws-1",
        branch="main", path="models/x.sql",
    )
    assert a == b


def test_storage_key_isolation_between_users():
    """User Alice's files must never resolve to the same key as Bob's even
    if every other field matches. This is the BYOC isolation contract."""
    a = storage_key_for_file(
        org_id="acme", user_id="alice", workspace_id="ws-1",
        branch="main", path="x.sql",
    )
    b = storage_key_for_file(
        org_id="acme", user_id="bob", workspace_id="ws-1",
        branch="main", path="x.sql",
    )
    assert a != b
