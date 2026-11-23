import os
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from gateway_auth import RoleConfig, make_gateway_dependency

ML_STUDIO_ROLES = RoleConfig(
    all_roles=("ml-viewer", "ml-engineer", "ml-platform-admin"),
    tiers={"ml-viewer": 0, "ml-engineer": 1, "ml-platform-admin": 2},
    default_role="ml-viewer",
)
WORKSPACE_ROLES = RoleConfig(
    all_roles=("workspace-guest", "workspace-member", "workspace-owner"),
    tiers={"workspace-guest": 0, "workspace-member": 1, "workspace-owner": 2},
    default_role="workspace-guest",
)


def make_client(roles=ML_STUDIO_ROLES, default_tenant_id="default"):
    app = FastAPI()
    get_principal = make_gateway_dependency(roles, default_tenant_id=default_tenant_id)

    @app.get("/whoami")
    def whoami(principal=Depends(get_principal)):
        return {"role": principal.role, "username": principal.username, "groups": principal.groups, "tenant_id": principal.tenant_id}

    return TestClient(app)


@pytest.fixture(autouse=True)
def env():
    os.environ["TRUST_ENVOY_HEADERS"] = "true"
    os.environ["ENVOY_INTERNAL_SECRET"] = "real-secret"
    yield
    os.environ.pop("TRUST_ENVOY_HEADERS", None)
    os.environ.pop("ENVOY_INTERNAL_SECRET", None)


def test_rejects_when_trust_envoy_headers_off():
    os.environ["TRUST_ENVOY_HEADERS"] = "false"
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "alice", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 401


def test_rejects_missing_gateway_secret():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "alice", "X-User-Roles": "ml-platform-admin"})
    assert r.status_code == 401
    assert "did not come through the gateway" in r.json()["detail"]


def test_rejects_wrong_gateway_secret():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "alice", "X-Inaiera-Gateway-Secret": "wrong"})
    assert r.status_code == 401


def test_rejects_missing_username_even_with_correct_secret():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Roles": "ml-platform-admin", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 401
    assert "missing identity headers" in r.json()["detail"]


def test_accepts_correct_secret_single_role():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "alice", "X-User-Roles": "ml-engineer", "X-Tenant-Id": "org_acme", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "ml-engineer"
    assert body["username"] == "alice"
    assert body["tenant_id"] == "org_acme"


def test_picks_recognized_role_from_mixed_list():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "bob", "X-User-Roles": "some-other-product-role,ml-viewer,another-role", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["role"] == "ml-viewer"


def test_resolves_highest_tier_when_multiple_match():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "carol", "X-User-Roles": "ml-viewer,ml-platform-admin", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["role"] == "ml-platform-admin"


def test_defaults_to_least_privileged_when_no_match():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "dave", "X-User-Roles": "totally-unrelated", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["role"] == "ml-viewer"


def test_splits_and_trims_groups():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "erin", "X-User-Groups": "eng, data-stewards ,ml-team", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["groups"] == ["eng", "data-stewards", "ml-team"]


def test_falls_back_to_default_tenant():
    client = make_client(default_tenant_id="ml_models")
    r = client.get("/whoami", headers={"X-User-Name": "frank", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.json()["tenant_id"] == "ml_models"


def test_genuinely_generic_across_different_role_vocabularies():
    client = make_client(roles=WORKSPACE_ROLES)
    r = client.get("/whoami", headers={"X-User-Name": "grace", "X-User-Roles": "workspace-owner,ml-viewer", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["role"] == "workspace-owner"


def test_never_crashes_on_empty_header_values():
    client = make_client()
    r = client.get("/whoami", headers={"X-User-Name": "iris", "X-User-Roles": "", "X-User-Groups": "", "X-Inaiera-Gateway-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json()["role"] == "ml-viewer"
    assert r.json()["groups"] == []
