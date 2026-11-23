from app.services.schema_drift import compare_schema_fields


def test_schema_drift_allows_new_optional_fields() -> None:
    result = compare_schema_fields(
        approved_fields=[{"name": "id", "type": "string", "nullable": False, "primary_key": True}],
        observed_fields=[
            {"name": "id", "type": "string", "nullable": False, "primary_key": True},
            {"name": "email", "type": "string", "nullable": True, "primary_key": False},
        ],
    )

    assert result["compatible"] is True
    assert result["events"][0]["action"] == "allow_and_notify"


def test_schema_drift_blocks_primary_key_changes() -> None:
    result = compare_schema_fields(
        approved_fields=[{"name": "id", "type": "string", "nullable": False, "primary_key": True}],
        observed_fields=[{"name": "id", "type": "string", "nullable": False, "primary_key": False}],
    )

    assert result["compatible"] is False
    assert result["events"][0]["action"] == "block_deployment"



def test_a_contract_that_leaves_primary_key_out_means_false_not_a_change():
    result = compare_schema_fields(
        [{"name": "name", "type": "string", "nullable": False}],
        [{"name": "name", "type": "string", "nullable": False, "primary_key": False}],
    )
    assert result["events"] == []


def test_records_matching_a_contract_with_a_primary_key_report_no_drift():
    """Sample records cannot show a primary key (inference says false for every field); the contract's own answer is kept, so matching records are not drift."""
    from app.services.schema_service import schema_service

    contract = {"schema_definition": {"fields": [
        {"name": "product_id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "product_name", "type": "string", "nullable": False},
    ]}}
    result = schema_service.detect_drift(contract, [{"product_id": 1, "product_name": "a"}, {"product_id": 2, "product_name": "b"}])
    assert result["events"] == []
    assert result["approval_required"] is False


def test_a_new_required_column_in_records_still_needs_approval():
    from app.services.schema_service import schema_service

    contract = {"schema_definition": {"fields": [{"name": "product_id", "type": "integer", "nullable": False, "primary_key": True}]}}
    result = schema_service.detect_drift(contract, [{"product_id": 1, "brand": "Dell"}, {"product_id": 2, "brand": "HP"}])
    assert [e["drift_type"] for e in result["events"]] == ["new_required_field"]
    assert result["approval_required"] is True


def test_a_whole_number_in_a_number_column_is_not_a_type_change_but_decimals_in_an_integer_column_are():
    number_column = [{"name": "discount", "type": "number", "nullable": True}]
    assert compare_schema_fields(number_column, [{"name": "discount", "type": "integer", "nullable": True, "primary_key": False}])["events"] == []
    integer_column = [{"name": "stock", "type": "integer", "nullable": False}]
    widened = compare_schema_fields(integer_column, [{"name": "stock", "type": "number", "nullable": False, "primary_key": False}])
    assert [e["drift_type"] for e in widened["events"]] == ["type_changed"]
    text_in_number = compare_schema_fields(number_column, [{"name": "discount", "type": "string", "nullable": True, "primary_key": False}])
    assert [e["drift_type"] for e in text_in_number["events"]] == ["type_changed"]
