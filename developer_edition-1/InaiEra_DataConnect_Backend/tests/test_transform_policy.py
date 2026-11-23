import pytest

from app.services.transform_policy import generate_smt_config, validate_transform


def test_generate_smt_config_fragment() -> None:
    config = generate_smt_config(
        [
            {
                "order": 2,
                "name": "maskPII",
                "type": "MaskField",
                "scope": "value",
                "config": {"fields": "email,phone"},
            },
            {
                "order": 1,
                "name": "createKey",
                "type": "ValueToKey",
                "scope": "value",
                "config": {"fields": "customer_id"},
            },
        ]
    )

    assert config["transforms"] == "createKey,maskPII"
    assert config["transforms.createKey.type"] == "org.apache.kafka.connect.transforms.ValueToKey"
    assert config["transforms.maskPII.type"] == "org.apache.kafka.connect.transforms.MaskField$Value"


def test_reject_non_smt_transformation() -> None:
    with pytest.raises(ValueError, match="must be implemented outside Kafka Connect SMTs"):
        validate_transform({"name": "joinData", "type": "join", "scope": "value", "config": {}})

