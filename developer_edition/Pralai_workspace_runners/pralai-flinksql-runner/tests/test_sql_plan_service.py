"""DQ-FLINK-008 — structured SQL plan extraction must survive exactly the
shapes a single non-greedy regex breaks on: quoted identifiers, nested
types, multiple sources, comments, and catalog-defined (un-CREATEd) tables.

Pure-function tests — sql_plan_service has no DB/IO dependency.
"""
from __future__ import annotations

from app.services.sql_plan_service import extract_create_tables, extract_plan, strip_comments


def test_strip_comments_removes_line_and_block_comments():
    sql = "SELECT 1 -- trailing comment\n/* block\ncomment */ SELECT 2"
    cleaned = strip_comments(sql)
    assert "trailing comment" not in cleaned
    assert "block" not in cleaned
    assert "SELECT 1" in cleaned and "SELECT 2" in cleaned


def test_strip_comments_does_not_touch_a_string_literal_containing_comment_markers():
    sql = "CREATE TABLE t (x STRING) WITH ('topic' = 'orders--v2')"
    cleaned = strip_comments(sql)
    assert "orders--v2" in cleaned


def test_simple_kafka_source_is_extracted():
    sql = """
    CREATE TABLE orders (
      id STRING,
      amount DECIMAL(10,2),
      event_ts TIMESTAMP(3)
    ) WITH (
      'connector' = 'kafka',
      'topic' = 'orders-topic',
      'format' = 'json'
    );
    INSERT INTO sink SELECT * FROM orders;
    """
    tables = extract_create_tables(sql)
    assert len(tables) == 1
    t = tables[0]
    assert t["name"] == "orders"
    assert {"name": "id", "type": "STRING"} in t["columns"]
    assert {"name": "amount", "type": "DECIMAL(10,2)"} in t["columns"]
    assert t["options"] == {"connector": "kafka", "topic": "orders-topic", "format": "json"}


def test_nested_types_do_not_truncate_the_column_list():
    # A non-greedy `\((.*?)\)` regex would stop at the first `)` inside
    # DECIMAL(10,2) or the first `>` inside ROW<...>, silently losing every
    # column and WITH option declared after it.
    sql = """
    CREATE TABLE complex_src (
      id STRING,
      amount DECIMAL(10,2),
      payload ROW<a INT, b ARRAY<STRING>, c MAP<STRING, INT>>,
      event_ts TIMESTAMP(3)
    ) WITH (
      'connector' = 'kafka',
      'topic' = 'complex-topic',
      'properties.bootstrap.servers' = 'broker:9092',
      'format' = 'json'
    );
    """
    tables = extract_create_tables(sql)
    assert len(tables) == 1
    t = tables[0]
    names = {c["name"] for c in t["columns"]}
    assert names == {"id", "amount", "payload", "event_ts"}
    payload = next(c for c in t["columns"] if c["name"] == "payload")
    assert payload["type"] == "ROW<a INT, b ARRAY<STRING>, c MAP<STRING, INT>>"
    # All four WITH options survive — a truncating regex would have lost
    # everything after the first nested paren/bracket.
    assert t["options"] == {
        "connector": "kafka", "topic": "complex-topic",
        "properties.bootstrap.servers": "broker:9092", "format": "json",
    }


def test_quoted_identifiers_with_special_characters_are_unquoted_correctly():
    sql = """
    CREATE TABLE `orders-raw` (
      `event-id` STRING,
      `amount` DECIMAL(10,2)
    ) WITH ('connector' = 'kafka', 'topic' = 't');
    """
    tables = extract_create_tables(sql)
    assert tables[0]["name"] == "orders-raw"
    names = {c["name"] for c in tables[0]["columns"]}
    assert names == {"event-id", "amount"}


def test_comment_containing_ddl_like_text_is_not_mistaken_for_a_table():
    sql = """
    -- CREATE TABLE fake_table (x INT) WITH ('connector' = 'kafka')
    CREATE TABLE real_table (x INT) WITH ('connector' = 'kafka', 'topic' = 'real');
    """
    tables = extract_create_tables(sql)
    assert len(tables) == 1
    assert tables[0]["name"] == "real_table"


def test_primary_key_and_watermark_clauses_are_not_treated_as_columns():
    sql = """
    CREATE TABLE t (
      id STRING,
      event_ts TIMESTAMP(3),
      WATERMARK FOR event_ts AS event_ts - INTERVAL '5' SECOND,
      PRIMARY KEY (id) NOT ENFORCED
    ) WITH ('connector' = 'kafka', 'topic' = 't');
    """
    columns = extract_create_tables(sql)[0]["columns"]
    names = {c["name"] for c in columns}
    assert names == {"id", "event_ts"}


def test_multiple_sources_are_each_extracted_independently():
    sql = """
    CREATE TABLE src_a (id STRING) WITH ('connector' = 'kafka', 'topic' = 'a');
    CREATE TABLE src_b (id STRING) WITH ('connector' = 'kafka', 'topic' = 'b');
    CREATE TABLE sink_c (id STRING) WITH ('connector' = 'kafka', 'topic' = 'c');
    INSERT INTO sink_c SELECT a.id FROM src_a a JOIN src_b b ON a.id = b.id;
    """
    plan = extract_plan(sql)
    source_names = {t["name"] for t in plan["sources"]}
    sink_names = {t["name"] for t in plan["sinks"]}
    assert source_names == {"src_a", "src_b"}
    assert sink_names == {"sink_c"}
    assert plan["unresolved_tables"] == []


def test_a_table_referenced_but_never_created_is_reported_unresolved():
    # A catalog-defined table (e.g. a Hive/Paimon table already registered
    # in the catalog) has no inline CREATE TABLE for a regex to even find —
    # this must be surfaced explicitly, not silently produce an empty result.
    sql = "INSERT INTO local_sink SELECT * FROM catalog_defined_source;"
    plan = extract_plan(sql)
    assert plan["sources"] == []
    assert plan["sinks"] == []
    assert set(plan["unresolved_tables"]) == {"local_sink", "catalog_defined_source"}


def test_a_declared_but_unreferenced_table_still_counts_as_a_source():
    sql = "CREATE TABLE orphan (id STRING) WITH ('connector' = 'kafka', 'topic' = 'x');"
    plan = extract_plan(sql)
    assert [t["name"] for t in plan["sources"]] == ["orphan"]


def test_a_table_that_is_both_read_and_a_named_sink_is_classified_as_a_sink_only():
    sql = """
    CREATE TABLE t (id STRING) WITH ('connector' = 'kafka', 'topic' = 't');
    INSERT INTO t SELECT id FROM t;
    """
    plan = extract_plan(sql)
    assert [x["name"] for x in plan["sinks"]] == ["t"]
    assert plan["sources"] == []


def test_computed_column_and_comment_suffix_do_not_pollute_the_type():
    sql = """
    CREATE TABLE t (
      id STRING,
      raw_ts BIGINT,
      event_ts AS TO_TIMESTAMP_LTZ(raw_ts, 3) COMMENT 'derived',
      amount DECIMAL(10,2) NOT NULL COMMENT 'never null'
    ) WITH ('connector' = 'kafka', 'topic' = 't');
    """
    columns = {c["name"]: c["type"] for c in extract_create_tables(sql)[0]["columns"]}
    assert columns["id"] == "STRING"
    assert columns["amount"] == "DECIMAL(10,2)"
    # event_ts is a computed column (AS <expr>) — no plain type to report,
    # so it's correctly dropped rather than reporting the expression as a type.
    assert "event_ts" not in columns
