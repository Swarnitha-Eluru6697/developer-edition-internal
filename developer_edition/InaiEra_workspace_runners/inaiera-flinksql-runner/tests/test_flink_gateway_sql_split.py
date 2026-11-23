"""RUN-FLINK-012 — execute_statements() split multi-statement SQL scripts
with plain `sql.split(";")`, which corrupts anything containing a
semicolon that isn't actually a statement terminator: a semicolon inside
a string literal, inside a `--` or `/* */` comment, or inside a
backtick-quoted identifier. split_sql_script() replaces it with a
lexically-aware splitter that tracks which of those it's currently inside
and only treats `;` as a separator outside all of them.
"""
from __future__ import annotations

from app.executors.flink_gateway import FlinkGatewayClient, split_sql_script


# ---------------------------------------------------------------------
# split_sql_script — pure
# ---------------------------------------------------------------------
def test_splits_a_simple_two_statement_script():
    sql = "CREATE TABLE t (id STRING) WITH ('connector'='kafka'); INSERT INTO t SELECT 1;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
    assert stmts[1].startswith("INSERT INTO")


def test_semicolon_inside_a_string_literal_is_not_a_split_point():
    sql = "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c');"
    stmts = split_sql_script(sql)
    assert len(stmts) == 2
    assert stmts[0] == "INSERT INTO t VALUES ('a;b')"
    assert stmts[1] == "INSERT INTO t VALUES ('c')"


def test_escaped_quote_inside_a_string_literal_does_not_end_it_early():
    """'it''s; still one string' — the doubled '' is SQL's escape for a
    literal quote, not the string's end, so the semicolon right after it
    must stay inside the same statement."""
    sql = "INSERT INTO t VALUES ('it''s; still one string');"
    stmts = split_sql_script(sql)
    assert len(stmts) == 1
    assert stmts[0] == "INSERT INTO t VALUES ('it''s; still one string')"


def test_semicolon_inside_a_line_comment_is_not_a_split_point():
    sql = "-- note: default port is 9092; do not change\nINSERT INTO t SELECT 1;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 1
    assert "do not change" in stmts[0]
    assert stmts[0].rstrip().endswith("SELECT 1")


def test_semicolon_inside_a_block_comment_is_not_a_split_point():
    sql = "/* multi-line;\n   comment; with semicolons */\nINSERT INTO t SELECT 1;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 1
    assert stmts[0].startswith("/* multi-line;")
    assert stmts[0].rstrip().endswith("SELECT 1")


def test_semicolon_inside_a_backtick_quoted_identifier_is_not_a_split_point():
    sql = "SELECT `weird;column` FROM t;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 1
    assert stmts[0] == "SELECT `weird;column` FROM t"


def test_a_statement_with_no_trailing_semicolon_is_still_captured():
    sql = "CREATE TABLE t (id STRING) WITH ('connector'='kafka');\nINSERT INTO t SELECT 1"
    stmts = split_sql_script(sql)
    assert len(stmts) == 2
    assert stmts[1] == "INSERT INTO t SELECT 1"


def test_empty_and_whitespace_only_segments_are_dropped():
    sql = "CREATE TABLE t (id STRING) WITH ('connector'='kafka');;  ;\nINSERT INTO t SELECT 1;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 2


def test_richer_multi_statement_script_with_mixed_semicolon_hazards():
    """A single script combining every hazard the finding calls out —
    proves they don't interact badly with each other either."""
    sql = """
    -- Source topic; do not point at prod by mistake
    CREATE TABLE orders (
      id STRING,
      note STRING  -- inline comment; with a semicolon
    ) WITH (
      'connector' = 'kafka',
      'topic' = 'orders;staging',
      'properties.group.id' = 'g1'
    );

    /* Sink table;
       written by the quality job */
    CREATE TABLE sink (id STRING) WITH ('connector' = 'blackhole');

    INSERT INTO sink SELECT id FROM orders WHERE note <> 'skip;this;one';
    """
    stmts = split_sql_script(sql)
    assert len(stmts) == 3
    # Leading comments stay attached to the statement that follows them —
    # same as the original sql.split(";") did for the non-hazard case.
    assert "CREATE TABLE orders" in stmts[0]
    assert "'topic' = 'orders;staging'" in stmts[0]
    assert stmts[1].lstrip().startswith("/* Sink table;")
    assert "CREATE TABLE sink" in stmts[1]
    assert stmts[2].lstrip().startswith("INSERT INTO sink")
    assert "'skip;this;one'" in stmts[2]


def test_naive_split_would_have_corrupted_the_string_literal_case():
    """Documents the exact regression: plain str.split(';') breaks this
    input into 3 pieces (one of them a dangling, invalid fragment); the
    fixed splitter correctly produces 2 whole statements."""
    sql = "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c');"
    naive = [s.strip() for s in sql.split(";") if s.strip()]
    assert len(naive) == 3  # the bug, for contrast
    assert len(split_sql_script(sql)) == 2  # the fix


# ---------------------------------------------------------------------
# execute_statements — the fix wired into the real submission path
# ---------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeHttpClient:
    def __init__(self):
        self.submitted_statements: list[str] = []
        self._op_counter = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        assert url.endswith("/statements")
        self.submitted_statements.append(json["statement"])
        self._op_counter += 1
        return _FakeResponse({"operationHandle": f"op-{self._op_counter}"})

    async def get(self, url):
        if url.endswith("/status"):
            return _FakeResponse({"status": "FINISHED"})
        if "/result/0" in url:
            return _FakeResponse({"jobID": "job-1"})
        raise AssertionError(f"unexpected GET {url}")


def _client() -> FlinkGatewayClient:
    c = FlinkGatewayClient.__new__(FlinkGatewayClient)
    c._gw = "http://localhost:8083"
    c._jm = "http://localhost:8081"
    c._timeout = 5
    c._submit_timeout = 5
    return c


async def test_execute_statements_submits_the_whole_string_literal_intact(monkeypatch):
    """A single INSERT with a semicolon inside its string literal — kept
    to one INSERT so the (separate, pre-existing) multi-INSERT ->
    STATEMENT SET compaction doesn't also kick in and complicate what
    this test is checking."""
    fake = _FakeHttpClient()
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    client = _client()
    sql = "CREATE TABLE t (v STRING) WITH ('connector'='blackhole'); INSERT INTO t VALUES ('a;b');"
    await client.execute_statements("sess-1", sql)

    assert fake.submitted_statements == [
        "CREATE TABLE t (v STRING) WITH ('connector'='blackhole')",
        "INSERT INTO t VALUES ('a;b')",
    ]


async def test_execute_statements_keeps_a_commented_out_semicolon_in_one_statement(monkeypatch):
    fake = _FakeHttpClient()
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)

    client = _client()
    sql = "-- keep default port 9092; do not change\nINSERT INTO t SELECT 1;"
    await client.execute_statements("sess-1", sql)

    assert len(fake.submitted_statements) == 1
    assert "do not change" in fake.submitted_statements[0]


# ---------------------------------------------------------------------
# RUN-FLINK-012 hardening: double quotes, STATEMENT SET blocks, comments
# ---------------------------------------------------------------------
def test_semicolon_inside_double_quotes_is_not_a_split_point():
    sql = 'INSERT INTO t SELECT "a;b" FROM s; INSERT INTO t SELECT 1;'
    assert split_sql_script(sql) == ['INSERT INTO t SELECT "a;b" FROM s', "INSERT INTO t SELECT 1"]


def test_escaped_double_quote_does_not_end_the_token():
    sql = 'SELECT "x"";y" FROM t; SELECT 2;'
    assert split_sql_script(sql) == ['SELECT "x"";y" FROM t', "SELECT 2"]


def test_explicit_statement_set_block_stays_one_statement():
    sql = (
        "CREATE TABLE a (v STRING) WITH ('connector'='blackhole');\n"
        "EXECUTE STATEMENT SET\nBEGIN\n"
        "  INSERT INTO a SELECT 'x;y';  -- trailing; comment\n"
        "  /* block; comment */ INSERT INTO a SELECT \"q;\";\n"
        "END;\n"
        "SET 'k' = 'v';"
    )
    stmts = split_sql_script(sql)
    assert len(stmts) == 3
    assert stmts[0].startswith("CREATE TABLE a")
    assert stmts[1].startswith("EXECUTE STATEMENT SET") and stmts[1].rstrip().endswith("END")
    assert "INSERT INTO a SELECT 'x;y'" in stmts[1] and "-- trailing; comment" in stmts[1]
    assert stmts[2] == "SET 'k' = 'v'"


def test_statement_set_without_execute_keyword_and_lowercase():
    sql = "statement set begin insert into a select 1; insert into b select 2; end; select 3;"
    stmts = split_sql_script(sql)
    assert len(stmts) == 2 and stmts[0].lower().endswith("end") and stmts[1] == "select 3"


def test_the_word_end_inside_a_string_does_not_close_the_statement_set():
    sql = "EXECUTE STATEMENT SET BEGIN INSERT INTO a SELECT 'END'; INSERT INTO b SELECT 1; END;"
    assert len(split_sql_script(sql)) == 1


def test_comment_only_tail_is_not_a_statement():
    assert split_sql_script("INSERT INTO t SELECT 1;\n-- the end; really\n") == ["INSERT INTO t SELECT 1"]


def test_statement_kind_classification():
    from app.core.sql_script import statement_kind
    assert statement_kind("-- c\n/* d */ INSERT INTO t SELECT 1") == "INSERT"
    assert statement_kind("EXECUTE STATEMENT SET BEGIN INSERT INTO a SELECT 1; END") == "STATEMENT_SET"
    assert statement_kind("EXECUTE INSERT INTO t SELECT 1") == "INSERT"
    assert statement_kind("create table x (a int)") == "CREATE"


async def test_compiled_statement_set_survives_inserts_ending_in_line_comments(monkeypatch):
    """Two INSERTs are compiled into one STATEMENT SET; an INSERT ending in
    a `-- comment` must not swallow the `;` separator that follows it."""
    fake = _FakeHttpClient()
    monkeypatch.setattr("app.executors.flink_gateway.httpx.AsyncClient", lambda **kw: fake)
    sql = "INSERT INTO a SELECT 1 -- first rule\n;\nINSERT INTO b SELECT 2 -- second rule\n;"
    await _client().execute_statements("sess-1", sql)
    assert len(fake.submitted_statements) == 1
    compiled = fake.submitted_statements[0]
    assert split_sql_script(compiled + ";") == [compiled]          # still ONE statement
    inner = compiled.split("BEGIN", 1)[1].rsplit("END", 1)[0]
    assert len([p for p in split_sql_script(inner) if p.strip()]) == 2   # both INSERTs intact
