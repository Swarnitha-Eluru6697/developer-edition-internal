"""Validate a FlinkSQL job SQL block without deploying it.

Called by:
  1. POST /api/v1/jobs/{id}/validate  (user-facing)
  2. POST /api/v1/validate            (internal — Workspace codegen chat)
"""
from __future__ import annotations
import re
from app.core.config import get_settings
from app.core.sql_script import split_sql_script, statement_kind
from app.services import pg_lake
from app.executors.flink_gateway import get_flink_client

# Statements that are EXPLAINed, never executed, during validation — an
# INSERT / STATEMENT SET / SELECT executed here would start a real job.
_EXPLAIN_KINDS = frozenset({"INSERT", "STATEMENT_SET", "SELECT", "WITH", "EXPLAIN"})
from app.schemas import ValidateRequest, ValidateResponse
from app.services.sql_plan_service import structured_plan


async def validate_job_sql(sql: str) -> dict:
    # The structured plan is pure parsing — computed up front so it is
    # returned even when the Gateway round-trip below fails.
    try:
        plan = structured_plan(sql)
    except Exception:  # noqa: BLE001
        plan = None
    client = get_flink_client()
    session_id = None
    try:
        session_id = await client.create_session("inaiera-validate")
        sources = re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?", sql, re.IGNORECASE)
        sinks = re.findall(r"INSERT\s+INTO\s+((?:`[^`]+`|\w+)(?:\.(?:`[^`]+`|\w+)){0,2})", sql, re.IGNORECASE)

        # Register DDL/SET/USE statements in the session first, THEN EXPLAIN each
        # INSERT — otherwise EXPLAIN of "INSERT ... FROM src" fails because the
        # source/sink tables were never created in the session.
        # Validate the same SQL a deploy would submit, placeholders and all —
        # otherwise validation passes on text the cluster never sees.
        from app.services.job_service import resolve_placeholders
        sql = resolve_placeholders(sql, run_ref="validate")
        # RUN-FLINK-012: the shared lexically-aware splitter — never
        # sql.split(";"), which cut statements at a ';' inside a string
        # literal, quoted identifier, comment, or STATEMENT SET block.
        statements = split_sql_script(sql)
        errors: list[str] = []
        # FL-001: no credential written in SQL, no placeholder nothing substitutes, and the governed pg_lake catalog
        # registered (from runner configuration) when the SQL uses it.
        settings = get_settings()
        problems = pg_lake.sql_problems(sql, settings)
        if problems:
            return {"valid": False, "errors": problems, "statement_count": len(statements),
                    "detected_sources": sources, "detected_sinks": sinks,
                    "sources": plan["sources"] if plan else [], "sinks": plan["sinks"] if plan else [],
                    "unresolved_tables": plan["unresolved_tables"] if plan else [], "plan": plan}
        if pg_lake.references_catalog(sql, settings):
            await pg_lake.register_catalog(client, session_id, settings)
        explained_any = False
        for stmt in statements:
            # Classified on the first real keyword (comments skipped).
            if statement_kind(stmt) in _EXPLAIN_KINDS:
                res = await client.explain_sql(session_id, stmt)
                explained_any = True
                if not res.get("valid"):
                    errors.extend(res.get("errors", ["Compile error"]))
            else:
                # CREATE TABLE / VIEW, SET, USE, etc. — execute to register it.
                try:
                    await client.execute_statements(session_id, stmt)
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e)[:300])
        # No INSERT? fall back to explaining the final statement (e.g. a SELECT).
        if not explained_any and statements:
            res = await client.explain_sql(session_id, statements[-1])
            if not res.get("valid"):
                errors.extend(res.get("errors", ["Compile error"]))

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "statement_count": len(statements),
            "detected_sources": sources,
            "detected_sinks": sinks,
            "sources": plan["sources"] if plan else [],
            "sinks": plan["sinks"] if plan else [],
            "unresolved_tables": plan["unresolved_tables"] if plan else [],
            "plan": plan,
        }
    except Exception as e:
        return {"valid": False, "errors": [str(e)[:500]], "statement_count": 0,
                "detected_sources": [], "detected_sinks": [],
                "sources": plan["sources"] if plan else [],
                "sinks": plan["sinks"] if plan else [],
                "unresolved_tables": plan["unresolved_tables"] if plan else [], "plan": plan}
    finally:
        if session_id:
            try:
                await client.close_session(session_id)
            except Exception:  # noqa: BLE001
                pass


async def validate_endpoint(req: ValidateRequest) -> ValidateResponse:
    result = await validate_job_sql(req.sql)
    return ValidateResponse(**result)
