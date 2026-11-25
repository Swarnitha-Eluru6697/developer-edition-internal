from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
BASE = "/api/v1/dataconnect/topic-policies"


def _hdr(org_id: str) -> dict:
    return {"X-Tenant-Id": org_id}


def test_create_and_get_topic_policy(fake_postgres) -> None:
    resp = client.post(
        BASE,
        headers=_hdr("org_tp_1"),
        json={"name": "High-throughput", "partitions": 24, "replication_factor": 3, "cleanup_policy": "delete"},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["org_id"] == "org_tp_1"
    assert created["partitions"] == 24

    resp = client.get(f"{BASE}/{created['id']}", headers=_hdr("org_tp_1"))
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_rejects_cross_org_access(fake_postgres) -> None:
    resp = client.post(BASE, headers=_hdr("org_tp_owner"), json={"name": "Owner Policy"})
    policy_id = resp.json()["id"]

    resp = client.get(f"{BASE}/{policy_id}", headers=_hdr("org_tp_other"))
    assert resp.status_code == 404


def test_delete_rejects_cross_org_deletion(fake_postgres) -> None:
    """The same class of bug fixed earlier for quality policies: deleting
    another org's policy must 404, not silently succeed."""
    resp = client.post(BASE, headers=_hdr("org_tp_owner2"), json={"name": "Owner Policy"})
    policy_id = resp.json()["id"]

    resp = client.delete(f"{BASE}/{policy_id}", headers=_hdr("org_tp_other2"))
    assert resp.status_code == 404

    # Still there — the cross-org delete attempt must not have gone through.
    resp = client.get(f"{BASE}/{policy_id}", headers=_hdr("org_tp_owner2"))
    assert resp.status_code == 200


def test_list_is_org_scoped(fake_postgres) -> None:
    client.post(BASE, headers=_hdr("org_tp_list_a"), json={"name": "A's policy"})
    client.post(BASE, headers=_hdr("org_tp_list_b"), json={"name": "B's policy"})

    resp = client.get(BASE, headers=_hdr("org_tp_list_a"))
    names = [p["name"] for p in resp.json()]
    assert "A's policy" in names
    assert "B's policy" not in names


def test_create_rejects_invalid_cleanup_policy(fake_postgres) -> None:
    resp = client.post(BASE, headers=_hdr("org_tp_invalid"), json={"name": "Bad", "cleanup_policy": "wipe"})
    assert resp.status_code == 422


def test_create_rejects_invalid_schema_strategy(fake_postgres) -> None:
    resp = client.post(
        BASE, headers=_hdr("org_tp_invalid"), json={"name": "Bad", "schema_strategy": "made_up_strategy"}
    )
    assert resp.status_code == 422


def test_update_preserves_org_id(fake_postgres) -> None:
    resp = client.post(BASE, headers=_hdr("org_tp_update"), json={"name": "Original", "partitions": 6})
    policy_id = resp.json()["id"]

    resp = client.put(f"{BASE}/{policy_id}", headers=_hdr("org_tp_update"), json={"name": "Renamed", "partitions": 12})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["name"] == "Renamed"
    assert updated["partitions"] == 12
    assert updated["org_id"] == "org_tp_update"
