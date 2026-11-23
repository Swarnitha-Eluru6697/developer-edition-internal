"""Smoke tests — verify auth gates and basic route health."""


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["service"] == "flinksql-runner"


def test_list_jobs_requires_auth(client):
    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 401


def test_create_job_requires_auth(client):
    resp = client.post("/api/v1/jobs", json={"name": "test", "sql": "SELECT 1"})
    assert resp.status_code == 401


def test_validate_requires_internal_token(client):
    resp = client.post("/api/v1/validate", json={"sql": "SELECT 1"})
    assert resp.status_code == 401


def test_validate_rejects_user_jwt(client, auth_headers):
    """The /validate endpoint must only accept the internal service token, not user JWTs."""
    resp = client.post("/api/v1/validate", headers=auth_headers,
                       json={"sql": "SELECT 1"})
    assert resp.status_code == 401


def test_kafka_topics_requires_auth(client):
    resp = client.get("/api/v1/kafka/topics")
    assert resp.status_code == 401


def test_kafka_templates_requires_auth(client):
    resp = client.get("/api/v1/kafka/templates")
    assert resp.status_code == 401


def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "flinksql-runner"
    assert data["version"] == "0.1.0"
