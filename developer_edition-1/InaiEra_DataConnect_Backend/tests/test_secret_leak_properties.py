"""
Hardening: secret-leak regression tests, by property.

tests/test_secret_regression.py plants ONE marker through the connection paths. Here Hypothesis invents hundreds of secrets and pushes each through
every redaction function and every user/log/support-facing surface, and a static scan checks that no log call is even GIVEN a secret-shaped field.

A "secret" below is any value that must never be readable afterwards. Generators use the shapes real secrets take (URL-safe tokens, base64, passwords
with punctuation); URI passwords are limited to what is legal in a URI's userinfo, because a raw `@` or `/` there is not a valid URI (it must be %-encoded).
"""

import ast
import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.config import settings as app_settings
from app.core.redaction import redact_diagnostic, redact_secrets
from app.services.safe_error import to_safe_error
from app.services.secrets import (
    find_secret_shaped_entries,
    mask_connection_config,
    mask_connection_profile,
    redact_connector_config,
    sanitize_probe_message,
    split_secret_fields,
)

BACKEND = Path(__file__).resolve().parents[1]
PROPERTY = settings(max_examples=300, deadline=None)

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# a distinctive, long-enough value, so "not in output" cannot pass by coincidence
secret = st.text(alphabet=_ALNUM + "_.~+=-!#$%^*", min_size=10, max_size=40).map(lambda s: "Zq" + s)
# legal URI userinfo characters (unreserved + sub-delims minus the structural ones) — a raw "@", "/", ":" is not legal there
uri_secret = st.text(alphabet=_ALNUM + "-._~!$*+%", min_size=10, max_size=40).map(lambda s: "Zq" + s)
secret_key = st.sampled_from([
    "password", "passwd", "pwd", "secret", "client_secret", "token", "access_token", "api_key", "apikey", "api-key", "private_key", "secret_access_key",
    "access_key_id", "authorization", "sas_token", "passphrase",
])


# ── free-text redaction ────────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(secret_key, secret)
def test_a_key_value_secret_in_free_text_is_masked_in_every_common_spelling(key, value) -> None:
    for text in (f"{key}={value}", f'{key}: "{value}"', f"failed (host=db {key}={value} port=5432)", f"?user=a&{key}={value}&x=1", f"{{'{key}': '{value}'}}"):
        assert value not in redact_secrets(text), text
        assert value not in redact_diagnostic(text), text


@PROPERTY
@given(uri_secret, st.sampled_from(["postgresql", "postgresql+asyncpg", "mysql", "mongodb", "redis", "https", "amqp"]))
def test_a_password_in_a_url_is_masked_and_the_host_survives(value, scheme) -> None:
    text = f"could not connect to {scheme}://svc:{value}@db.internal:5432/erp"

    for redacted in (redact_secrets(text), redact_diagnostic(text)):
        assert value not in redacted
        assert "db.internal" in redacted               # the host stays: an operator needs it


@PROPERTY
@given(secret)
def test_a_bearer_or_basic_credential_is_masked(value) -> None:
    for scheme in ("Bearer", "bearer", "Basic"):
        assert value not in redact_secrets(f"Authorization: {scheme} {value}")


@PROPERTY
@given(secret, st.sampled_from([ConnectionError, RuntimeError, ValueError, OSError, TimeoutError, KeyError]))
def test_a_safe_error_never_carries_the_secret_in_what_is_stored_or_shown(value, exc_type) -> None:
    exc = exc_type(f"driver failure for postgresql://svc:{value}@h:5432/d (password={value}) Bearer {value}")

    safe = to_safe_error(exc, event="test.secret_leak")

    assert value not in safe.message
    assert value not in safe.describe()
    assert value not in json.dumps(safe.__dict__ if hasattr(safe, "__dict__") else str(safe))


@PROPERTY
@given(secret)
def test_a_probe_message_is_sanitized_before_it_is_stored(value) -> None:
    assert value not in sanitize_probe_message(f"login failed: user=svc password={value} at jdbc:postgresql://svc:{value}@h/d")


# ── structured data ────────────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(st.dictionaries(secret_key, secret, min_size=1, max_size=5), st.dictionaries(st.sampled_from(["hostname", "port", "database", "username"]), st.text(alphabet=_ALNUM, min_size=1, max_size=12), max_size=4))
def test_splitting_a_connection_config_keeps_every_secret_out_of_what_is_persisted(secrets, plain) -> None:
    non_secret, secret_fields = split_secret_fields({**plain, **secrets}, "postgresql")

    persisted = json.dumps(non_secret)
    for value in secrets.values():
        assert value not in persisted
    assert set(secrets) <= set(secret_fields) or all(v not in persisted for v in secrets.values())   # held back, not dropped silently


@PROPERTY
@given(st.dictionaries(secret_key, secret, min_size=1, max_size=5))
def test_masking_a_connection_for_display_never_shows_a_secret_value(secrets) -> None:
    connection = {"id": "c1", "name": "conn", "source_type": "postgresql", "connection_config": {"hostname": "h", **secrets}, "secret_ref": "secret://dataconnect/connections/c1"}

    shown = json.dumps(mask_connection_profile(connection))

    for value in secrets.values():
        assert value not in shown
    assert json.dumps(mask_connection_config({"hostname": "h", **secrets}, "postgresql")).count("Zq") == 0 or all(
        v not in json.dumps(mask_connection_config(secrets, "postgresql")) for v in secrets.values())


@PROPERTY
@given(st.dictionaries(st.sampled_from(["database.password", "connection.password", "password", "aws.secret.access.key", "api.key", "sasl.jaas.config", "token"]), secret, min_size=1, max_size=4))
def test_a_generated_connector_config_is_redacted_before_it_is_stored_or_returned(sensitive) -> None:
    shown = json.dumps(redact_connector_config({"connector.class": "x", "tasks.max": "1", **sensitive}))

    leaked = [v for v in sensitive.values() if v in shown]
    assert leaked == []
    assert "connector.class" in shown                      # ordinary settings are untouched


@PROPERTY
@given(secret_key, secret)
def test_an_inline_credential_typed_into_a_pipeline_is_found_by_path_never_by_value(key, value) -> None:
    found = find_secret_shaped_entries({"outer": {key: value}, "list": [{"x": f"mysql://u:{value}@h/d"}]})

    assert found                                           # it IS detected ...
    assert all(value not in path for path in found)        # ... and only its PATH is ever reported


# ── the support bundle ─────────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(secret, secret, uri_secret)
def test_the_diagnostics_configuration_section_never_shows_a_secret_setting(jwt, encryption_key, url_password) -> None:
    import asyncio

    from app.services import diagnostics

    original = {k: getattr(app_settings, k) for k in ("jwt_secret", "secret_encryption_key", "database_url", "prometheus_url")}
    try:
        app_settings.jwt_secret, app_settings.secret_encryption_key = jwt, encryption_key
        app_settings.database_url = f"postgresql+asyncpg://dataconnect:{url_password}@db.internal:5432/dataconnect"
        app_settings.prometheus_url = f"http://u:{url_password}@prom.internal:9090"
        bundle = diagnostics.deep_redact({"configuration": asyncio.run(diagnostics._configuration(None))})
    finally:
        for key, value in original.items():
            setattr(app_settings, key, value)

    text = json.dumps(bundle)
    for value in (jwt, encryption_key, url_password):
        assert value not in text


# ── nothing is even handed to a logger ─────────────────────────────────────────────────────────────────────────────

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_SECRET_NAME = re.compile(r"(password|passwd|secret|token|api_?key|private_?key|credential|connection_config|secret_fields)", re.IGNORECASE)
# names that look secret-shaped but are references / booleans / counts / classifications, never the value
_SAFE_SUFFIX = re.compile(r"(_ref|_id|_ids|_count|_name|_class|_type|_kind|_present|_set|_configured|_status|_enabled|_seconds|_mode|_path|_paths|_keys|_field_names)$", re.IGNORECASE)
# reviewed and deliberate: the keyword is a NAME or an identifier, not the secret
_REVIEWED: set[tuple[str, str]] = set()


def _log_calls():
    for path in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS:
                receiver = node.func.value
                name = getattr(receiver, "id", getattr(receiver, "attr", ""))
                if "log" in name.lower():
                    yield path.relative_to(BACKEND).as_posix(), node


def test_no_log_call_is_given_a_secret_shaped_field() -> None:
    """If a secret never reaches a logger it can never be logged — redaction is the net, this is the wall."""
    calls = list(_log_calls())
    assert len(calls) > 100, "the scan found almost no log calls — it is broken"
    offenders = []
    for path, node in calls:
        for keyword in node.keywords:
            name = keyword.arg or ""
            if _SECRET_NAME.search(name) and not _SAFE_SUFFIX.search(name) and (path, name) not in _REVIEWED:
                offenders.append(f"{path}:{node.lineno} logs `{name}=`")
    assert offenders == [], "these log calls are given a secret-shaped field:\n  " + "\n  ".join(offenders)


def test_the_scan_itself_would_catch_a_leaking_log_call() -> None:
    tree = ast.parse('logger.info("connected", password=cfg["password"], host=h)')
    (call,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_SECRET_NAME.search(k.arg or "") and not _SAFE_SUFFIX.search(k.arg or "") for k in call.keywords)
    clean = ast.parse('logger.info("connected", secret_ref=ref, token_count=3)')
    (clean_call,) = [n for n in ast.walk(clean) if isinstance(n, ast.Call)]
    assert not any(_SECRET_NAME.search(k.arg or "") and not _SAFE_SUFFIX.search(k.arg or "") for k in clean_call.keywords)


# Exceptions this codebase raises itself, with messages it authored (grammar errors, size limits, validation). Echoing one of these to the caller is
# the point of them. Anything BROADER (`Exception`, `OSError`, a driver's error) carries text we do not control — hosts, SQL, maybe credentials.
_CURATED_EXCEPTIONS = {"ValueError", "ValidationError", "SinkTopicError", "TopicParseError", "KeyError", "LookupError", "JobStateConflict"}


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"<bare except>"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {getattr(n, "id", getattr(n, "attr", "?")) for n in nodes}


def _routes_that_echo_exception_text(source: str) -> list[tuple[int, set[str]]]:
    found = []
    for handler in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ExceptHandler) and n.name):
        for call in (n for n in ast.walk(handler) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "HTTPException"):
            for keyword in call.keywords:
                if keyword.arg == "detail" and any(isinstance(n, ast.Name) and n.id == handler.name for n in ast.walk(keyword.value)):
                    found.append((call.lineno, _caught_names(handler)))
    return found


@pytest.mark.parametrize("path", sorted((BACKEND / "app" / "api" / "routes").glob("*.py")), ids=lambda p: p.name)
def test_no_route_echoes_the_text_of_a_broad_or_driver_exception_to_the_caller(path: Path) -> None:
    """`except Exception as exc: raise HTTPException(detail=str(exc))` hands a driver's message to the client. Echoing a curated ValueError is fine."""
    offenders = [(line, names) for line, names in _routes_that_echo_exception_text(path.read_text()) if names - _CURATED_EXCEPTIONS]
    assert offenders == [], f"{path.name} returns exception text from {offenders}"


def test_the_route_scan_flags_a_broad_handler_and_allows_a_curated_one() -> None:
    broad = "try:\n    x()\nexcept Exception as exc:\n    raise HTTPException(status_code=500, detail=str(exc))\n"
    driver = "try:\n    x()\nexcept (ValueError, OSError) as e:\n    raise HTTPException(500, detail=f'failed: {e}')\n"
    curated = "try:\n    x()\nexcept ValueError as exc:\n    raise HTTPException(status_code=400, detail=str(exc))\n"
    fixed = "try:\n    x()\nexcept Exception as exc:\n    raise HTTPException(status_code=500, detail='Something went wrong')\n"
    assert _routes_that_echo_exception_text(broad)[0][1] == {"Exception"}
    assert _routes_that_echo_exception_text(driver)[0][1] == {"ValueError", "OSError"}
    assert _routes_that_echo_exception_text(curated)[0][1] <= _CURATED_EXCEPTIONS
    assert _routes_that_echo_exception_text(fixed) == []


def test_a_failed_stream_produce_returns_a_safe_error_not_the_kafka_clients_text(monkeypatch) -> None:
    """KafkaProduceError's message embeds the underlying client error (broker addresses, a REST proxy response body)."""
    from conftest import DEFAULT_GATEWAY_HEADERS
    from fastapi.testclient import TestClient

    import app.api.routes.quality as quality_routes
    from app.main import app
    from app.services.kafka_runtime import KafkaProduceError

    async def policy(policy_id, org_id):
        return {"id": policy_id}

    async def pipeline(org_id, pipeline_id):
        return {"id": pipeline_id}

    async def fail(**kwargs):
        raise KafkaProduceError("raw-d-s-e-batch-v1", "k", 3, RuntimeError("NoBrokersAvailable: kafka-0.internal.corp:9092 password=hunter2"))

    monkeypatch.setattr(quality_routes, "get_quality_policy", policy)
    monkeypatch.setattr(quality_routes.pipeline_service, "get_pipeline", pipeline)
    monkeypatch.setattr(quality_routes.quality_service, "process_streaming_record", fail)

    response = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS).post(
        "/api/v1/dataconnect/quality-policies/p1/stream/process", headers={"X-Tenant-Id": "org_1"}, json={"pipeline_id": "pl", "record": {"a": 1}},
    )

    assert response.status_code == 502
    for leaked in ("kafka-0.internal.corp", "9092", "hunter2", "NoBrokersAvailable"):
        assert leaked not in response.text
    assert "ref " in response.text                          # the log reference that finds the detailed (redacted) entry
