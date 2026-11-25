from app.core.config import settings
from app.services.secrets import (
    MASK,
    SENSITIVE_CONNECTION_KEYS,
    mask_connection_config,
    mask_connection_profile,
    redact_connector_config,
    resolve_secret,
    split_secret_fields,
)


def test_mask_connection_config_masks_known_secret_keys() -> None:
    masked = mask_connection_config(
        {"hostname": "db.internal", "port": 5432, "username": "svc", "password": "hunter2"}
    )
    assert masked["password"] == MASK
    assert masked["hostname"] == "db.internal"
    assert masked["port"] == 5432
    assert masked["username"] == "svc"


def test_mask_connection_config_masks_aws_and_token_style_keys() -> None:
    masked = mask_connection_config(
        {"access_key_id": "AKIA123", "secret_access_key": "abc", "api_key": "xyz", "token": "t"}
    )
    assert masked == {
        "access_key_id": MASK,
        "secret_access_key": MASK,
        "api_key": MASK,
        "token": MASK,
    }


def test_mask_connection_config_scrubs_embedded_uri_credentials() -> None:
    masked = mask_connection_config({"uri": "mongodb://admin:s3cr3t@mongo.internal:27017/db"})
    assert "s3cr3t" not in masked["uri"]
    assert "admin" not in masked["uri"]
    assert masked["uri"].startswith("mongodb://")
    assert masked["uri"].endswith("@mongo.internal:27017/db")


def test_mask_connection_config_handles_none() -> None:
    assert mask_connection_config(None) == {}


def test_mask_connection_config_uses_schema_for_known_source_type() -> None:
    """postgres's schema doesn't declare a "notes" field as secret — it
    should pass through even though a made-up global-heuristic name might
    not catch it, proving the schema (not just the hardcoded set) is
    actually consulted."""
    masked = mask_connection_config(
        {"hostname": "db.internal", "password": "hunter2", "notes": "internal db"}, "postgres"
    )
    assert masked["password"] == MASK
    assert masked["notes"] == "internal db"


def test_mask_connection_config_schema_catches_a_field_the_global_set_would_miss(monkeypatch) -> None:
    """Every field the built-in schemas mark secret happens to already be in
    the hardcoded global set too — this proves the schema signal actually
    does the masking on its own, by registering a made-up source type whose
    secret field name isn't in that global set at all."""
    import app.services.connection_config_schema as schema_module

    monkeypatch.setitem(
        schema_module.CONNECTION_CONFIG_SCHEMAS,
        "made_up_source",
        [
            schema_module._field("connect_pin", "Connect PIN", schema_module.SECRET, persist=False),
            schema_module._field("host", "Host", schema_module.STRING),
        ],
    )

    masked = mask_connection_config({"host": "x.internal", "connect_pin": "1234"}, "made_up_source")

    assert masked["connect_pin"] == MASK
    assert masked["host"] == "x.internal"
    assert "connect_pin" not in SENSITIVE_CONNECTION_KEYS  # confirms it's schema-driven, not the global set


def test_mask_connection_profile_only_touches_connection_config() -> None:
    profile = {
        "id": "c1",
        "org_id": "org_1",
        "secret_ref": "vault://dataconnect/org_1/db",
        "connection_config": {"password": "hunter2", "hostname": "db"},
    }
    masked = mask_connection_profile(profile)
    assert masked["connection_config"]["password"] == MASK
    assert masked["connection_config"]["hostname"] == "db"
    assert masked["secret_ref"] == "vault://dataconnect/org_1/db"  # a pointer, not a secret
    assert masked["id"] == "c1"


def test_redact_connector_config_masks_known_connector_keys() -> None:
    redacted = redact_connector_config(
        {
            "connection.password": "hunter2",
            "database.password": "hunter2",
            "aws.access.key.id": "AKIA123",
            "aws.secret.access.key": "abc",
            "connection.user": "svc",
            "topics": "org.raw.orders",
        }
    )
    assert redacted["connection.password"] == MASK
    assert redacted["database.password"] == MASK
    assert redacted["aws.access.key.id"] == MASK
    assert redacted["aws.secret.access.key"] == MASK
    assert redacted["connection.user"] == "svc"
    assert redacted["topics"] == "org.raw.orders"


def test_redact_connector_config_scrubs_mongo_uri() -> None:
    redacted = redact_connector_config(
        {"connection.uri": "mongodb://admin:s3cr3t@mongo.internal:27017/"}
    )
    assert "s3cr3t" not in redacted["connection.uri"]
    assert "admin" not in redacted["connection.uri"]


def test_redact_connector_config_handles_none() -> None:
    assert redact_connector_config(None) == {}


def test_split_secret_fields_separates_secret_from_non_secret() -> None:
    """GAP-01: connection_service.py persists only the non-secret half —
    the secret half never reaches ConnectionProfile.connection_config."""
    non_secret, secret = split_secret_fields(
        {"hostname": "db.internal", "port": 5432, "username": "svc", "password": "hunter2"}
    )
    assert non_secret == {"hostname": "db.internal", "port": 5432, "username": "svc"}
    assert secret == {"password": "hunter2"}


def test_split_secret_fields_drops_an_echoed_mask_without_storing_it() -> None:
    """A client echoing back the mask it was shown (didn't touch this field)
    must not overwrite the real stored secret with the literal "********" —
    and must not appear in `secret` either, so connection_service.py's
    merge-on-update leaves the existing stored value alone."""
    non_secret, secret = split_secret_fields({"hostname": "db.internal", "password": MASK})
    assert non_secret == {"hostname": "db.internal"}
    assert secret == {}


def test_split_secret_fields_drops_empty_or_none_secret_values() -> None:
    non_secret, secret = split_secret_fields({"password": None, "api_key": ""})
    assert non_secret == {}
    assert secret == {}


def test_split_secret_fields_handles_none() -> None:
    assert split_secret_fields(None) == ({}, {})


def test_split_secret_fields_uses_schema_for_known_source_type() -> None:
    non_secret, secret = split_secret_fields(
        {"hostname": "db.internal", "password": "hunter2", "notes": "internal db"}, "postgres"
    )
    assert secret == {"password": "hunter2"}
    assert non_secret == {"hostname": "db.internal", "notes": "internal db"}


def test_resolve_secret_returns_raw_value_by_default() -> None:
    connection = {
        "secret_ref": "vault://dataconnect/org_1/db",
        "connection_config": {"password": "hunter2"},
    }
    assert resolve_secret(connection, "password", "password") == "hunter2"


def test_resolve_secret_prefers_config_provider_ref_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "prefer_config_provider_refs", True)
    connection = {
        "secret_ref": "vault://dataconnect/org_1/db",
        "connection_config": {"password": "hunter2"},
    }
    assert resolve_secret(connection, "password", "password") == "${vault:dataconnect/org_1/db:password}"


def test_resolve_secret_falls_back_without_a_vault_secret_ref(monkeypatch) -> None:
    monkeypatch.setattr(settings, "prefer_config_provider_refs", True)
    connection = {"secret_ref": None, "connection_config": {"password": "hunter2"}}
    assert resolve_secret(connection, "password", "password") == "hunter2"
