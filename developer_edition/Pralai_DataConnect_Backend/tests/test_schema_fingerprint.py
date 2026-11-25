from app.services.schema_fingerprint import compute_schema_fingerprint


def test_fingerprint_is_order_independent() -> None:
    a = [{"name": "id", "type": "string", "nullable": False, "primary_key": True}]
    b = [{"name": "email", "type": "string", "nullable": True, "primary_key": False}]

    forward = compute_schema_fingerprint(a + b)
    backward = compute_schema_fingerprint(b + a)

    assert forward == backward


def test_fingerprint_ignores_inference_only_metadata() -> None:
    contract_only = [{"name": "id", "type": "string", "nullable": False, "primary_key": True}]
    with_metadata = [
        {
            "name": "id", "type": "string", "nullable": False, "primary_key": True,
            "confidence": 0.42, "null_only": False,
        }
    ]

    assert compute_schema_fingerprint(contract_only) == compute_schema_fingerprint(with_metadata)


def test_fingerprint_changes_when_a_type_changes() -> None:
    a = [{"name": "id", "type": "string", "nullable": False, "primary_key": True}]
    b = [{"name": "id", "type": "integer", "nullable": False, "primary_key": True}]

    assert compute_schema_fingerprint(a) != compute_schema_fingerprint(b)


def test_fingerprint_recurses_into_nested_fields() -> None:
    a = [{"name": "addr", "type": "object", "nullable": True, "primary_key": False,
          "fields": [{"name": "city", "type": "string", "nullable": True, "primary_key": False}]}]
    b = [{"name": "addr", "type": "object", "nullable": True, "primary_key": False,
          "fields": [{"name": "city", "type": "integer", "nullable": True, "primary_key": False}]}]

    assert compute_schema_fingerprint(a) != compute_schema_fingerprint(b)


def test_empty_fields_is_stable() -> None:
    assert compute_schema_fingerprint([]) == compute_schema_fingerprint([])
