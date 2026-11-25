"""Regression test for the WS-DATA-007 UCS-authorization bypass.

The execution worker treats any string shaped like "connection:<uuid>" as
a connection reference — that shape is only
ever supposed to be constructed by submit_sql_execution *after* ucs_authorize
grants access (app/api/executions.py). Without the schema-level guard below,
a client could set `warehouse="connection:<victim-uuid>"` directly and skip
the `if payload.connection_id:` branch entirely, reaching a connection's real
credentials with zero UCS check. See app/schemas/execution.py.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.execution import SqlExecutionRequest


def _base_kwargs(**overrides):
    kwargs = {"session_id": "00000000-0000-0000-0000-000000000001", "code_text": "select 1"}
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("smuggled", [
    "connection:11111111-1111-1111-1111-111111111111",
    "Connection:11111111-1111-1111-1111-111111111111",
    "CONNECTION:11111111-1111-1111-1111-111111111111",
    "  connection:11111111-1111-1111-1111-111111111111",
])
def test_warehouse_cannot_smuggle_a_connection_reference(smuggled):
    with pytest.raises(ValidationError, match="reserved 'connection:' prefix"):
        SqlExecutionRequest(**_base_kwargs(warehouse=smuggled))


def test_legitimate_warehouse_name_still_accepted():
    req = SqlExecutionRequest(**_base_kwargs(warehouse="trino"))
    assert req.warehouse == "trino"


def test_warehouse_none_still_accepted():
    req = SqlExecutionRequest(**_base_kwargs(warehouse=None))
    assert req.warehouse is None


def test_connection_id_path_is_unaffected():
    req = SqlExecutionRequest(**_base_kwargs(
        connection_id="22222222-2222-2222-2222-222222222222",
    ))
    assert req.warehouse is None
    assert str(req.connection_id) == "22222222-2222-2222-2222-222222222222"
