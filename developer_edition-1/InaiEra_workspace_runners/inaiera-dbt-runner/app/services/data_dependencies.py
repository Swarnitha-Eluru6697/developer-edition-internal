"""Data dependencies between projects, worked out from the lineage dbt itself reports.

Every successful run records its dataset lineage (`lineage_edges`: model -> model and source -> model, as fully qualified table names).
Project B depends on project A's DATA when a table B's latest build read is a table A's latest build wrote — e.g. products reads
customers' `dim_customers` through a dbt `source()`. Nothing has to be configured; the link appears after both projects have run
successfully once, and follows them as their models change.

dbt reports `ref()` and `source()` only. A model that names another project's table directly (`from public.monthly_revenue`) is read
from its compiled SQL too (`table_refs`, stored as lineage edges of type "sql"): only names right after FROM / JOIN, schema-qualified,
with comments and quoted text removed. It is a best guess, so a name more than one other project builds is not linked (logged), and
what the compiled SQL does not show — a name built by a Jinja variable, read only in a macro or hook, unqualified, or the second table
of a comma join — is not seen. Declaring the table under `sources:` is exact.

A SQL job has no manifest: after a successful run its own statements are read the same way (`statement_lineage`): the tables a statement
reads (`table_refs`) and the table it creates or changes (`create table / view`, `insert into`, `merge into`, `update`, `delete from`),
stored as "sql" edges with the catalog dropped (`postgresql.public.dim_customers` through Trino is `public.dim_customers`). So a SQL job
reading customers' table is linked below customers, and a project reading a table a SQL job wrote is linked below the job. Same guesses
and the same limits as above, plus: a written table must be schema-qualified (`create table foo as ...` names no schema, so it is not
linked), and a statement that writes but reads nothing (`create table x (...)`, `insert ... values`) adds no edge. A Python job is never
read: its SQL lives in strings and variables.

Unrelated to Dependents (app/services/dependents.py), which is a job link the user sets by hand.
"""
from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LineageEdge, Project, Run

log = structlog.get_logger()

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRINGS = re.compile(r"'(?:[^']|'')*'")
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_TABLE_AFTER_FROM_OR_JOIN = re.compile(rf"\b(?:from|join)\s+({_IDENT}(?:\s*\.\s*{_IDENT}){{1,2}})", re.IGNORECASE)


def _table(name: str) -> str:
    """'"dbtrunner"."public"."dim_customers"' and 'dbtrunner.public.DIM_CUSTOMERS' are the same table."""
    return re.sub(r"\s+", "", name.replace('"', "").replace("`", "")).lower()


def short_name(name: str) -> str:
    """schema.table — what SQL usually writes, without the database."""
    return ".".join(_table(name).split(".")[-2:])


_WRITE_TARGET = re.compile(
    rf"\b(?:create\s+(?:or\s+replace\s+)?(?:(?:temporary|temp)\s+)?(?:table|view|materialized\s+view)\s+(?:if\s+not\s+exists\s+)?"
    rf"|insert\s+into\s+|merge\s+into\s+|update\s+|delete\s+from\s+)({_IDENT}(?:\s*\.\s*{_IDENT}){{1,2}})", re.IGNORECASE)


def statement_lineage(statements: list[str]) -> list[tuple[str, str]]:
    """(table read, table written) for a SQL job's statements, as schema.table: every table a statement reads paired with every table it
    writes. A table a statement both reads and writes is not its own source. First-seen order, no repeats."""
    edges: dict[tuple[str, str], None] = {}
    for statement in statements:
        text = _STRINGS.sub("''", _COMMENTS.sub(" ", statement or ""))
        writes = {short_name(m.group(1)) for m in _WRITE_TARGET.finditer(text)}
        reads = [short_name(t) for t in table_refs(statement)]
        for target in sorted(writes):
            for source in reads:
                if source != target:
                    edges.setdefault((source, target), None)
    return list(edges)


def table_refs(sql: str) -> list[str]:
    """Schema-qualified tables a SQL statement reads (after FROM / JOIN), comments and quoted text removed, in first-seen order."""
    text = _STRINGS.sub("''", _COMMENTS.sub(" ", sql or ""))
    seen: dict[str, None] = {}
    for m in _TABLE_AFTER_FROM_OR_JOIN.finditer(text):
        seen.setdefault(_table(m.group(1)), None)
    return list(seen)


async def data_upstream(db: AsyncSession, org_id: str) -> dict[uuid.UUID, list[uuid.UUID]]:
    """project id -> the projects whose data it reads (ordered by name). Projects with none are absent."""
    latest = (
        select(LineageEdge.run_id, LineageEdge.project_id,
               func.rank().over(partition_by=LineageEdge.project_id,
                                order_by=(Run.completed_at.desc().nulls_last(), Run.created_at.desc())).label("rk"))
        .join(Run, Run.id == LineageEdge.run_id)
        .join(Project, Project.id == LineageEdge.project_id)
        .where(Project.org_id == org_id, Run.status == "succeeded")
        .subquery()
    )
    runs = select(latest.c.run_id).where(latest.c.rk == 1).distinct()
    rows = (await db.execute(
        select(LineageEdge.project_id, LineageEdge.source_table, LineageEdge.target_table).where(LineageEdge.run_id.in_(runs))
    )).all()
    builds: dict[uuid.UUID, set[str]] = {}
    reads: dict[uuid.UUID, set[str]] = {}
    for project_id, source, target in rows:
        builds.setdefault(project_id, set()).add(_table(target))
        reads.setdefault(project_id, set()).add(_table(source))
    if not reads:
        return {}
    by_full: dict[str, set[uuid.UUID]] = {}
    by_short: dict[str, set[uuid.UUID]] = {}
    by_schema_table: dict[str, set[uuid.UUID]] = {}       # tables written WITHOUT a catalog (a SQL job): schema.table only
    for owner, tables in builds.items():
        for t in tables:
            by_full.setdefault(t, set()).add(owner)
            by_short.setdefault(short_name(t), set()).add(owner)
            if t.count(".") < 2:
                by_schema_table.setdefault(t, set()).add(owner)
    names = dict((await db.execute(select(Project.id, Project.name).where(Project.org_id == org_id))).all())
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for reader, tables in reads.items():
        own = builds.get(reader, set())
        own_short = {short_name(t) for t in own}
        ups: set[uuid.UUID] = set()
        for t in tables:
            if t in own or short_name(t) in own_short:              # a table the project builds itself is never another's data
                continue
            owners = (by_full.get(t) if t.count(".") >= 2 else by_short.get(t)) or set()
            if not owners and t.count(".") >= 2:                 # dbt names its database, a SQL job's table has no catalog: same schema.table
                owners = by_schema_table.get(short_name(t)) or set()
            owners = owners - {reader}
            if len(owners) == 1:
                ups |= owners
            elif len(owners) > 1:                               # built by several projects: do not guess
                log.warning("data_dependency_ambiguous_table", table=t, reader=str(reader),
                            builders=sorted(names.get(o, str(o)) for o in owners))
        if ups:
            out[reader] = sorted(ups, key=lambda i: (names.get(i, ""), str(i)))
    return out
