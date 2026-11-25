from app.services.connection_config_schema import (
    schema_for_source_type,
    sensitive_keys_for_source_type,
    validate_connection_config,
)


def test_schema_for_postgres_marks_password_as_secret_and_not_persisted() -> None:
    fields = schema_for_source_type("postgres")
    password = next(f for f in fields if f["key"] == "password")

    assert password["type"] == "secret"
    assert password["persist"] is False
    assert password["required"] is True


def test_schema_for_unknown_source_type_is_empty() -> None:
    assert schema_for_source_type("no-such-type") == []


def test_schema_lookup_is_case_insensitive() -> None:
    assert schema_for_source_type("POSTGRES") == schema_for_source_type("postgres")


def test_sensitive_keys_for_postgres_includes_password_only() -> None:
    assert sensitive_keys_for_source_type("postgres") == {"password"}


def test_sensitive_keys_for_s3_includes_both_aws_credential_fields() -> None:
    assert sensitive_keys_for_source_type("s3") == {"access_key_id", "secret_access_key"}


def test_sensitive_keys_for_unknown_source_type_is_empty() -> None:
    assert sensitive_keys_for_source_type("no-such-type") == set()


def test_validate_connection_config_rejects_missing_required_field() -> None:
    errors = validate_connection_config(
        "postgres", {"hostname": "db.internal", "database": "sales", "username": "svc"}
    )

    assert any("password" in e for e in errors)


def test_validate_connection_config_accepts_complete_config() -> None:
    errors = validate_connection_config(
        "postgres",
        {"hostname": "db.internal", "database": "sales", "username": "svc", "password": "hunter2"},
    )

    assert errors == []


def test_validate_connection_config_rejects_wrong_type() -> None:
    errors = validate_connection_config(
        "postgres",
        {
            "hostname": "db.internal", "database": "sales", "username": "svc",
            "password": "hunter2", "port": "not-a-number",
        },
    )

    assert any("port" in e for e in errors)


def test_validate_connection_config_enum_field_rejects_invalid_option() -> None:
    errors = validate_connection_config(
        "elasticsearch", {"hostname": "es.internal", "scheme": "gopher"}
    )

    assert any("scheme" in e for e in errors)


def test_validate_connection_config_allows_unknown_extra_fields() -> None:
    """The schema documents and enforces the KNOWN fields — it doesn't lock
    out connector-specific passthrough options."""
    errors = validate_connection_config(
        "postgres",
        {
            "hostname": "db.internal", "database": "sales", "username": "svc",
            "password": "hunter2", "some_future_option": "value",
        },
    )

    assert errors == []


def test_validate_connection_config_unknown_source_type_is_always_clean() -> None:
    assert validate_connection_config("no-such-type", {"anything": "goes"}) == []


def test_validate_connection_config_accepts_list_field_as_string_or_list() -> None:
    base = {"hostname": "db.internal", "database": "sales", "username": "svc", "password": "x"}
    assert validate_connection_config("postgres", {**base, "tables": "orders,customers"}) == []
    assert validate_connection_config("postgres", {**base, "tables": ["orders", "customers"]}) == []
