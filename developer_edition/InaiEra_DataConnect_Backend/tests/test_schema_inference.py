from decimal import Decimal

from app.services.schema_inference import infer_schema


def _field(fields: list[dict], name: str) -> dict:
    return next(f for f in fields if f["name"] == name)


def test_flat_scalar_types_are_detected() -> None:
    records = [{"id": "1", "count": 3, "active": True, "score": 1.5}]

    result = infer_schema(records)

    fields = result["fields"]
    assert _field(fields, "id")["type"] == "string"
    assert _field(fields, "count")["type"] == "integer"
    assert _field(fields, "active")["type"] == "boolean"
    assert _field(fields, "score")["type"] == "number"
    assert result["sample_size"] == 1


def test_mixed_type_conflict_falls_back_to_string() -> None:
    records = [{"v": "hello"}, {"v": True}]

    result = infer_schema(records)

    assert _field(result["fields"], "v")["type"] == "string"


def test_integer_and_number_widen_to_number() -> None:
    records = [{"v": 1}, {"v": 2.5}]

    result = infer_schema(records)

    assert _field(result["fields"], "v")["type"] == "number"


def test_mixed_type_combination_is_order_independent() -> None:
    forward = infer_schema([{"v": 1}, {"v": "x"}, {"v": True}])
    backward = infer_schema([{"v": True}, {"v": "x"}, {"v": 1}])

    assert _field(forward["fields"], "v")["type"] == _field(backward["fields"], "v")["type"] == "string"


def test_null_only_field_is_string_and_flagged() -> None:
    records = [{"v": None}, {"v": None}]

    result = infer_schema(records)

    field = _field(result["fields"], "v")
    assert field["type"] == "string"
    assert field["null_only"] is True
    assert field["confidence"] == 0.0


def test_nullable_reflects_partial_presence() -> None:
    records = [{"v": "x"}, {"v": None}]

    field = _field(infer_schema(records)["fields"], "v")

    assert field["nullable"] is True
    assert field["confidence"] == 0.5


def test_date_and_timestamp_are_detected() -> None:
    records = [{"d": "2024-01-01", "t": "2024-01-01T12:30:00Z"}]

    fields = infer_schema(records)["fields"]

    assert _field(fields, "d")["type"] == "date"
    assert _field(fields, "t")["type"] == "timestamp"


def test_date_and_timestamp_conflict_widens_to_timestamp() -> None:
    records = [{"v": "2024-01-01"}, {"v": "2024-01-01T12:30:00Z"}]

    field = _field(infer_schema(records)["fields"], "v")

    assert field["type"] == "timestamp"


def test_non_matching_date_shaped_string_is_plain_string() -> None:
    records = [{"v": "01/01/2024"}]

    field = _field(infer_schema(records)["fields"], "v")

    assert field["type"] == "string"


def test_nested_object_recurses_into_fields() -> None:
    records = [{"address": {"city": "NYC", "zip": 10001}}]

    field = _field(infer_schema(records)["fields"], "address")

    assert field["type"] == "object"
    child_names = {f["name"] for f in field["fields"]}
    assert child_names == {"city", "zip"}
    city = next(f for f in field["fields"] if f["name"] == "city")
    assert city["type"] == "string"


def test_array_infers_element_type_from_every_element() -> None:
    records = [{"tags": ["a", "b"]}, {"tags": ["c"]}]

    field = _field(infer_schema(records)["fields"], "tags")

    assert field["type"] == "array"
    assert field["items"]["type"] == "string"


def test_array_of_objects_recurses_into_item_fields() -> None:
    records = [{"items": [{"sku": "a", "qty": 1}, {"sku": "b", "qty": 2}]}]

    field = _field(infer_schema(records)["fields"], "items")

    assert field["type"] == "array"
    assert field["items"]["type"] == "object"
    assert {f["name"] for f in field["items"]["fields"]} == {"sku", "qty"}


def test_empty_array_gets_null_only_string_items() -> None:
    records = [{"tags": []}]

    field = _field(infer_schema(records)["fields"], "tags")

    assert field["type"] == "array"
    assert field["items"]["type"] == "string"
    assert field["items"]["null_only"] is True


def test_shape_conflict_between_object_and_scalar_falls_back_to_string() -> None:
    records = [{"v": {"a": 1}}, {"v": "plain"}]

    field = _field(infer_schema(records)["fields"], "v")

    assert field["type"] == "string"


def test_nesting_beyond_max_depth_stops_recursing(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "schema_infer_max_depth", 1)
    records = [{"a": {"b": {"c": 1}}}]

    top = infer_schema(records)["fields"][0]
    assert top["type"] == "object"
    assert top["fields"] is not None
    nested = top["fields"][0]
    assert nested["name"] == "b"
    assert nested["type"] == "object"
    assert nested["fields"] is None  # depth ceiling reached — no further recursion


def test_decimal_precision_and_scale_are_computed() -> None:
    records = [{"amount": Decimal("12.345")}, {"amount": Decimal("1.5")}]

    field = _field(infer_schema(records)["fields"], "amount")

    assert field["type"] == "decimal"
    assert field["precision"] == 5  # "12345" -> 5 significant digits
    assert field["scale"] == 3  # max of 3 and 1


def test_decimal_mixed_with_integer_widens_to_decimal() -> None:
    records = [{"amount": Decimal("1.50")}, {"amount": 3}]

    field = _field(infer_schema(records)["fields"], "amount")

    assert field["type"] == "decimal"


def test_decimal_precision_is_capped_by_setting(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "schema_infer_decimal_max_precision", 3)
    records = [{"amount": Decimal("123456.789")}]

    field = _field(infer_schema(records)["fields"], "amount")

    assert field["precision"] == 3


def test_empty_records_returns_empty_schema() -> None:
    result = infer_schema([])

    assert result == {"fields": [], "confidence": 0.0, "sample_size": 0}


def test_overall_confidence_is_the_average_of_per_field_confidence() -> None:
    records = [{"a": "x", "b": None}, {"a": "y", "b": None}]

    result = infer_schema(records)

    # a: 2/2 present = 1.0, b: 0/2 present = 0.0 -> average 0.5
    assert result["confidence"] == 0.5
