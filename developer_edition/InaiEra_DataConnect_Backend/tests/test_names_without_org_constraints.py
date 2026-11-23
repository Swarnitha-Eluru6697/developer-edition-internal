"""2026-11-06: topic and connector names carry no organization, so the database rules that leaned on the old names change (alembic 0057).

These need no database: they check the model's declarations and the migration's text agree, and that the old org-prefix rule is gone.
"""
import re
from pathlib import Path

from app.models.pipeline import (
    CONNECTOR_NAME_INDEX,
    DEAD_LETTER_TOPIC_INDEX,
    TOPIC_NAME_INDEX,
    Pipeline,
)

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0057_topic_names_without_org.py"


def _index(name: str):
    return next(i for i in Pipeline.__table__.indexes if i.name == name)


def test_a_sink_input_topic_is_no_longer_required_to_start_with_the_org_id() -> None:
    names = {c.name for c in Pipeline.__table__.constraints}

    assert "ck_dc_pipelines_sink_input_in_org" not in names
    assert "ck_dc_pipelines_sink_input_topic" in names              # a sink still needs an input topic
    assert "ck_dc_pipelines_source_has_no_topics_setting" in names


def test_the_topic_name_and_the_connector_name_are_unique_across_organizations() -> None:
    topic, connector = _index(TOPIC_NAME_INDEX), _index(CONNECTOR_NAME_INDEX)

    assert topic.unique is True
    assert [c.name for c in topic.columns] == ["topic_name"]
    assert connector.unique is True
    assert [c.name for c in connector.columns] == ["connector_name"]
    assert "org_id" not in [c.name for c in topic.columns] + [c.name for c in connector.columns]
    assert "topic_name IS NOT NULL" in str(topic.dialect_options["postgresql"]["where"])      # a sink has no topic of its own: NULLs do not collide


def test_the_migration_creates_exactly_what_the_model_declares() -> None:
    source = MIGRATION.read_text()

    assert TOPIC_NAME_INDEX in source
    assert CONNECTOR_NAME_INDEX in source
    assert DEAD_LETTER_TOPIC_INDEX in source
    assert 'op.drop_constraint(OLD_IN_ORG, "dc_pipelines", type_="check")' in source
    assert re.search(r'DEAD_LETTER_INDEX, "dc_pipelines", \["domain", "source_system", "entity", "version"\]', source)


def test_the_migration_refuses_to_choose_whose_pipeline_keeps_a_name() -> None:
    source = MIGRATION.read_text()

    assert source.count("_refuse_if_any(") == 4        # the definition and three checks: dead-letter queue, topic, connector
    assert "more than one organization" in source
