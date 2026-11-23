"""
Hardening: the redacted support diagnostics bundle (app/services/diagnostics.py, GET /diagnostics/bundle, `python -m app.diagnostics`).

The guarantees: it never contains a secret, even when a SECTION tries to leak one; a broken or hung section cannot take the bundle down; it is
bounded; only an Admin can fetch it; and the CLI form is honest about what a separate process cannot know.
"""

import json

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS, gateway_headers
from fastapi.testclient import TestClient

import app.services.diagnostics as diagnostics
from app.core.config import settings
from app.main import app
from app.services.diagnostics import build_diagnostics_bundle

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org_1"}

SECRET = "S3CR3T-MARKER-9f2c41d7b8a35e60"
LEAKY_TEXT = f"could not connect to postgresql://svc:{SECRET}@db.internal:5432/erp (password={SECRET}) Bearer {SECRET}abcdef"


async def _quiet(_org):
    return {"ok": True}


@pytest.fixture
def stub_sections(monkeypatch):
    """Replaces every collector with a harmless one, so a test can swap in just the section it is about."""
    sections = dict.fromkeys(diagnostics.SECTIONS, _quiet)
    monkeypatch.setattr(diagnostics, "SECTIONS", sections)
    return sections


# ── shape ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_bundle_has_every_section_and_its_metadata(stub_sections) -> None:
    bundle = await build_diagnostics_bundle(org_id="org_1")

    assert {"service", "configuration", "readiness", "database", "workers", "metrics", "recent_failures", "dependencies"} <= set(bundle)
    assert bundle["schema"] == "dataconnect-diagnostics/1"
    assert bundle["generated_at"].endswith("Z")
    assert bundle["truncated"] is False
    assert bundle["failed_sections"] == []


# ── no secrets, ever ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_real_configuration_section_reports_secrets_as_set_not_as_values(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "secret_encryption_key", SECRET)
    monkeypatch.setattr(settings, "database_url", f"postgresql+asyncpg://dataconnect:{SECRET}@db.internal:5432/dataconnect")
    monkeypatch.setattr(settings, "prometheus_url", f"http://user:{SECRET}@prom.internal:9090")

    config = await diagnostics._configuration(None)

    assert SECRET not in json.dumps(config)
    assert config["jwt_secret"] == "<set>"
    assert config["secret_encryption_key"] == "<set>"
    assert "db.internal" in config["database_url"]                      # the host is useful to support ...
    assert "dataconnect:" not in config["database_url"]                 # ... the credentials are not
    assert config["env"] == settings.env                                # ordinary settings are shown as they are


@pytest.mark.asyncio
async def test_an_unset_secret_is_reported_as_unset(monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_encryption_previous_keys", "")

    assert (await diagnostics._configuration(None))["secret_encryption_previous_keys"] == "<unset>"


@pytest.mark.asyncio
@pytest.mark.parametrize("leak", [
    {"detail": LEAKY_TEXT},                                             # a credential inside free text
    {"password": SECRET},                                               # a secret-shaped KEY
    {"nested": [{"api_key": SECRET}, {"note": LEAKY_TEXT}]},            # inside lists and sub-objects
    {"connection": {"secret_access_key": SECRET, "authorization": f"Bearer {SECRET}"}},
    {"url": f"https://user:{SECRET}@host.example/path?token={SECRET}"},
])
async def test_a_section_that_tries_to_leak_a_secret_is_cleaned_by_the_final_pass(stub_sections, leak) -> None:
    """The final deep redaction is a net under every section: even a collector with a bug cannot put a credential in the bundle."""
    async def leaky(_org):
        return leak

    stub_sections["recent_failures"] = leaky

    bundle = await build_diagnostics_bundle(org_id="org_1")

    assert SECRET not in json.dumps(bundle)


@pytest.mark.asyncio
async def test_the_markers_the_flags_use_are_not_mistaken_for_secrets(stub_sections) -> None:
    async def flags(_org):
        return {"jwt_secret": "<set>", "token": "<unset>", "ok": True}

    stub_sections["configuration"] = flags

    assert (await build_diagnostics_bundle())["configuration"] == {"jwt_secret": "<set>", "token": "<unset>", "ok": True}


# ── resilience ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_section_reports_its_error_class_and_the_others_still_arrive(stub_sections) -> None:
    async def broken(_org):
        raise ConnectionRefusedError(f"cannot reach postgres://u:{SECRET}@h/db")

    stub_sections["database"] = broken

    bundle = await build_diagnostics_bundle()

    assert bundle["database"] == {"error": "ConnectionRefusedError"}      # the class, never the message
    assert bundle["failed_sections"] == ["database"]
    assert bundle["service"] == {"ok": True}
    assert SECRET not in json.dumps(bundle)


@pytest.mark.asyncio
async def test_a_hung_section_is_abandoned_at_its_deadline(stub_sections, monkeypatch) -> None:
    import asyncio

    async def hang(_org):
        await asyncio.sleep(60)

    monkeypatch.setattr(diagnostics, "SECTION_TIMEOUT_SECONDS", 0.1)
    stub_sections["readiness"] = hang

    bundle = await build_diagnostics_bundle()

    assert bundle["readiness"]["error"] == "TimeoutError"
    assert bundle["failed_sections"] == ["readiness"]


# ── bounded ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_oversized_bundle_is_trimmed_to_the_cap_and_says_so(stub_sections, monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_BUNDLE_BYTES", 20_000)

    async def huge(_org):
        return {"rows": [{"id": i, "note": "x" * 200} for i in range(2_000)]}

    stub_sections["recent_failures"] = huge

    bundle = await build_diagnostics_bundle()

    assert bundle["truncated"] is True
    assert len(json.dumps(bundle)) <= 20_000
    assert 0 < len(bundle["recent_failures"]["rows"]) < 2_000


# ── the CLI form ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_bundle_from_another_process_does_not_claim_the_services_workers_are_down() -> None:
    bundle = await build_diagnostics_bundle(org_id=None, in_service_process=False)

    assert "note" in bundle["workers"]
    assert "operation_worker_running" not in bundle["workers"]
    assert "note" in bundle["metrics"]
    assert bundle["service"]["scope"] == {"deployment": "all organizations (aggregated)"}


# ── the endpoint ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_admin_gets_the_bundle_as_a_no_store_download(stub_sections) -> None:
    response = client.get("/api/v1/dataconnect/diagnostics/bundle", headers=HDR)

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="dataconnect-diagnostics-')
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["service"] == {"ok": True}


@pytest.mark.parametrize("persona", ["integrator", "viewer", "nothing"])
def test_only_a_settings_editor_can_fetch_it(stub_sections, persona) -> None:
    from personas import PERSONAS

    perms = PERSONAS.get(persona, frozenset())
    response = client.get("/api/v1/dataconnect/diagnostics/bundle", headers={**HDR, **gateway_headers(permissions=perms)})

    assert response.status_code == 403
    assert "service" not in response.text


def test_the_route_is_in_the_openapi_contract() -> None:
    assert "/api/v1/dataconnect/diagnostics/bundle" in app.openapi()["paths"]
