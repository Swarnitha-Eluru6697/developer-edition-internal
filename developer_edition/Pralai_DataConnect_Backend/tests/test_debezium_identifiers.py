from app.services.debezium_identifiers import (
    debezium_publication_name,
    debezium_server_id,
    debezium_slot_name,
    debezium_topic_prefix,
)


def test_topic_prefix_is_deterministic() -> None:
    assert debezium_topic_prefix("sales--sfdc--orders--cdc--v1") == debezium_topic_prefix(
        "sales--sfdc--orders--cdc--v1"
    )


def test_topic_prefix_sanitizes_disallowed_characters() -> None:
    prefix = debezium_topic_prefix("sales--sfdc--orders--cdc--v1")
    assert all(c.isalnum() or c in "._-" for c in prefix)


def test_topic_prefix_differs_across_entities_sharing_org_domain_source() -> None:
    """The bug this closes: two pipelines under the same org/domain/source
    but different entities used to get an IDENTICAL topic.prefix."""
    orders_prefix = debezium_topic_prefix("sales--sfdc--orders--cdc--v1")
    accounts_prefix = debezium_topic_prefix("sales--sfdc--accounts--cdc--v1")
    assert orders_prefix != accounts_prefix


def test_slot_name_is_valid_postgres_identifier() -> None:
    slot = debezium_slot_name("sales--sfdc--orders--cdc--v1")
    assert len(slot) <= 63
    assert slot[0].isalpha() or slot[0] == "_"
    assert all(c.islower() or c.isdigit() or c == "_" for c in slot)


def test_slot_and_publication_names_differ_across_orgs_sharing_an_entity_name() -> None:
    """The other half of the bug: slot/publication names used to be derived
    from the entity name alone — "customers" collided across every org and
    domain that happened to have an entity with that name."""
    slot_a = debezium_slot_name("sales--pg--customers--cdc--v1")
    slot_b = debezium_slot_name("support--pg--customers--cdc--v1")
    assert slot_a != slot_b

    pub_a = debezium_publication_name("sales--pg--customers--cdc--v1")
    pub_b = debezium_publication_name("support--pg--customers--cdc--v1")
    assert pub_a != pub_b


def test_slot_name_truncates_long_identifiers_without_colliding() -> None:
    """Two names that only differ after PostgreSQL's 63-byte limit must
    still produce distinct slot names — the hash suffix carries the part
    that got cut off."""
    long_a = "org_1--" + "a" * 80 + "--entity-one--cdc--v1"
    long_b = "org_1--" + "a" * 80 + "--entity-two--cdc--v1"

    slot_a = debezium_slot_name(long_a)
    slot_b = debezium_slot_name(long_b)

    assert len(slot_a) <= 63
    assert len(slot_b) <= 63
    assert slot_a != slot_b


def test_server_id_is_a_valid_mysql_server_id() -> None:
    server_id = debezium_server_id("sales--mysql--orders--cdc--v1")
    assert 1 <= server_id <= 2**32 - 1


def test_server_id_differs_across_pipelines() -> None:
    """The bug this closes: every MySQL CDC pipeline previously got the
    identical hardcoded server.id ("184054"), guaranteeing a replication
    collision the moment a second one existed against the same server."""
    id_a = debezium_server_id("sales--mysql--orders--cdc--v1")
    id_b = debezium_server_id("sales--mysql--accounts--cdc--v1")
    assert id_a != id_b


def test_server_id_is_deterministic() -> None:
    assert debezium_server_id("sales--mysql--orders--cdc--v1") == debezium_server_id(
        "sales--mysql--orders--cdc--v1"
    )
