from app.services.topic_naming import connector_name_for_class

NAME = "t1--little_horse_sink--customer_events--stream--v3"


def test_littlehorse_name_uses_hyphens_only():
    assert connector_name_for_class(NAME, "io.littlehorse.connect.WfRunSinkConnector") == (
        "t1--little-horse-sink--customer-events--stream--v3"
    )


def test_other_connectors_keep_their_name():
    assert connector_name_for_class(NAME, "io.confluent.connect.jdbc.JdbcSinkConnector") == NAME
    assert connector_name_for_class(NAME, None) == NAME
