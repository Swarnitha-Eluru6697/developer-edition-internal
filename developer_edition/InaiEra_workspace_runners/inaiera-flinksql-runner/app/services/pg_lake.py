"""The governed pg_lake connection (FL-001).

The old "pg_lake PostgreSQL sink" template asked the user's SQL to carry the JDBC URL, user name and PASSWORD as
`${PG_LAKE_JDBC_URL}` / `${PG_USER}` / `${PG_PASSWORD}`. Nothing resolved them (so the advertised path reached validation and
deployment with unresolved text), and "just substitute them" would have embedded a database password in stored, user-visible
Flink SQL: in `flink_jobs.sql`, in every deployment manifest, in API responses.

Now the connection is a NAMED JDBC CATALOG owned by the runner. Its URL, user and password come from the runner's own
configuration (a Secret in the chart), and the runner registers the catalog in each Gateway session before the user's
statements run. The SQL only names the catalog identity, `pg_lake.<database>.<table>`, and never carries a credential:

    INSERT INTO pg_lake.analytics.orders_sink SELECT id, amount FROM orders;

The same module refuses the two ways a job can still smuggle the problem back in: a credential written into the SQL itself,
and a `${PLACEHOLDER}` that nothing resolved.
"""
from __future__ import annotations

import re

from app.core.config import Settings

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# 'some.option.name' = 'value'  where the OPTION NAME says it is a secret and the value is not empty
_SECRET_OPTION = re.compile(
    r"'([^']*(?:password|passwd|secret|credential|access[-_.]?key|token|user[-_.]?info|jaas)[^']*)'\s*=\s*'([^']+)'", re.IGNORECASE)
# a password written inside an option VALUE, e.g. a SASL JAAS config: password="..."
_SECRET_IN_VALUE = re.compile(r"'[^']*'\s*=\s*'[^']*\bpassword\s*=\s*[\"']?[^\"'\s;]+", re.IGNORECASE)


def is_configured(settings: Settings) -> bool:
    return bool(settings.pg_lake_jdbc_base_url and settings.pg_lake_database and settings.pg_lake_user
                and settings.pg_lake_password)


def references_catalog(sql: str, settings: Settings) -> bool:
    """Does the SQL address the governed catalog (`pg_lake.<db>.<table>`)?"""
    name = re.escape(settings.pg_lake_catalog_name)
    return re.search(rf"(?<![\w.`\"])`?{name}`?\s*\.", _strip_strings_and_comments(sql), re.IGNORECASE) is not None


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def catalog_statement(settings: Settings) -> str:
    """The CREATE CATALOG statement for the session. Built from runner configuration; never stored, never returned."""
    options = [("type", "jdbc"), ("base-url", settings.pg_lake_jdbc_base_url),
               ("default-database", settings.pg_lake_database), ("username", settings.pg_lake_user),
               ("password", settings.pg_lake_password)]
    body = ",".join(f"{_quote(k)}={_quote(str(v))}" for k, v in options)
    return f"CREATE CATALOG `{settings.pg_lake_catalog_name}` WITH ({body})"


def scrub(text: str, settings: Settings) -> str:
    """Remove the catalog password and the CREATE CATALOG statement text from an error or log line, keeping the reason
    (the Gateway echoes a prefix of the statement in its error: "Flink SQL error in `<statement[:80]>`: <reason>")."""
    secret = settings.pg_lake_password
    statement = catalog_statement(settings)
    for length in (len(statement), 300, 200, 80):                 # the whole statement, then the prefixes it is echoed as
        text = text.replace(statement[:length], "CREATE CATALOG … (options hidden)")
    if secret:
        text = text.replace(_quote(secret), "'***'").replace(secret, "***")
    return text


async def register_catalog(client, session_id: str, settings: Settings) -> None:
    """Create the governed catalog in a Gateway session, from runner configuration. Any failure is re-raised with the
    password and the statement scrubbed out (the Gateway echoes statements in errors)."""
    try:
        await client.execute_statements(session_id, catalog_statement(settings))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not register the `{settings.pg_lake_catalog_name}` catalog: "
                           f"{_reason(scrub(str(e), settings))}") from None


def _reason(text: str) -> str:
    """The useful part of a Gateway error: its last "Caused by:" line, else the start of the text. The Gateway wraps the real
    cause (e.g. "password authentication failed") in a long Java stack trace."""
    causes = re.findall(r"Caused by: ([^\n]*?)(?:\\n|\n|\\tat |\tat |$)", text)
    return (causes[-1] if causes else text)[:400]


def unresolved_placeholders(sql: str) -> list[str]:
    """`${NAME}` placeholders still present (after resolution) — text no engine will ever substitute."""
    return sorted(set(_PLACEHOLDER.findall(_strip_comments(sql))))


def embedded_credentials(sql: str) -> list[str]:
    """Options that put a credential in the SQL: the option names (never the values)."""
    text = _strip_comments(sql)
    names = {m.group(1) for m in _SECRET_OPTION.finditer(text)}
    if _SECRET_IN_VALUE.search(text):
        names.add("(a password inside an option value)")
    return sorted(names)


def sql_problems(sql: str, settings: Settings) -> list[str]:
    """Reasons this SQL must not be validated/deployed as written. `sql` has already had its known placeholders resolved."""
    problems: list[str] = []
    for name in unresolved_placeholders(sql):
        problems.append(f"unresolved placeholder ${{{name}}}: nothing substitutes it. Remove it"
                        + (" and use the governed pg_lake catalog instead (INSERT INTO pg_lake.<database>.<table>)"
                           if name.startswith(("PG_", "PG_LAKE")) else ""))
    for option in embedded_credentials(sql):
        problems.append(f"the SQL carries a credential (option {option}). Credentials must not be written in SQL: "
                        "use a governed connection (the pg_lake catalog is configured on the runner)")
    if references_catalog(sql, settings) and not is_configured(settings):
        problems.append(f"the `{settings.pg_lake_catalog_name}` catalog is not configured on this runner "
                        "(PG_LAKE_JDBC_BASE_URL, PG_LAKE_DATABASE, PG_LAKE_USER, PG_LAKE_PASSWORD)")
    return problems


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", sql)


def _strip_strings_and_comments(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", _strip_comments(sql))
