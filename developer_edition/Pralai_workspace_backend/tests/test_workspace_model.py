"""W-021: Workspace's collaboration model is declared PERSONAL-ONLY for this release, and nothing quietly drifts from that.

Before: the model comment said "personal (or later shared)", the service enforced owner-only access, no membership/ACL existed,
and nothing said which of those was the decision — so a runner's `workspace_id` could be mistaken for a collaboration ACL.
Now `WORKSPACE_MODEL = "personal"` is the declaration, /auth/me reports it, and these tests fail if a sharing mechanism appears
(a membership/ACL table, a client-settable workspace type or owner, an owner-less workspace, a workspace route that is not
owner-checked) without the decision being revisited and `WORKSPACE_MODEL` changed on purpose.
"""
from __future__ import annotations

import ast
import asyncio
import types
import uuid
from pathlib import Path

import pytest

from app.api import auth as auth_api
from app.core import workspace_scope
from app.core.errors import WorkspaceError
from app.models import Base, Workspace
from app.schemas.workspace import WorkspaceCreate, WorkspaceUpdate
from app.services.workspace_service import WorkspaceService

HERE = Path(__file__).resolve().parent.parent


def test_the_declared_model_is_personal():
    assert workspace_scope.WORKSPACE_MODEL == "personal"
    assert Workspace.__table__.c.workspace_type.default.arg == "personal"


def test_me_reports_the_model_so_clients_do_not_offer_collaboration():
    assert "workspace_model" in auth_api.MeOut.model_fields
    src = (HERE / "app" / "api" / "auth.py").read_text()
    assert '"workspace_model": WORKSPACE_MODEL' in src


def test_no_membership_acl_or_sharing_table_exists():
    names = {t.name for t in Base.metadata.sorted_tables}
    sharing = {n for n in names if any(w in n for w in ("member", "acl", "share", "collaborat", "invite", "grant", "permission"))}
    assert not sharing, f"a sharing mechanism appeared ({sorted(sharing)}): revisit W-021 and WORKSPACE_MODEL deliberately"


def test_a_workspace_always_has_exactly_one_owner():
    owner = Workspace.__table__.c.owner_id
    assert owner.nullable is False and [fk.column.table.name for fk in owner.foreign_keys] == ["users"]
    assert any(c.name == "uq_workspace_owner_name" for c in Workspace.__table__.constraints)


@pytest.mark.parametrize("model", [WorkspaceCreate, WorkspaceUpdate])
def test_a_client_cannot_choose_the_type_or_the_owner(model):
    assert not {"workspace_type", "owner_id", "owner", "members", "shared_with", "visibility"} & set(model.model_fields)


def test_the_service_creates_personal_workspaces_only():
    tree = ast.parse((HERE / "app" / "services" / "workspace_service.py").read_text())
    values = [kw.value.value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Workspace"
              for kw in n.keywords if kw.arg == "workspace_type"]
    assert values == ["personal"]


def test_another_users_workspace_is_a_404_exactly_like_a_missing_one():
    mine, theirs = uuid.uuid4(), uuid.uuid4()

    class FakeDB:
        async def get(self, model, ident):
            return types.SimpleNamespace(id=ident, owner_id=uuid.UUID(int=1))   # owned by user #1

    svc = WorkspaceService(FakeDB())
    me = types.SimpleNamespace(id=uuid.UUID(int=2))
    owner = types.SimpleNamespace(id=uuid.UUID(int=1))
    assert asyncio.run(svc.get(owner, mine)).id == mine
    for ident in (mine, theirs):
        with pytest.raises(WorkspaceError) as exc:
            asyncio.run(svc.get(me, ident))
        assert exc.value.status_code == 404


def test_every_workspace_route_goes_through_the_owner_check():
    """Each handler in the workspace router resolves the caller's user row and then `get`s the workspace through the service
    (owner-only) — there is no route that loads a workspace by id any other way."""
    tree = ast.parse((HERE / "app" / "api" / "workspaces.py").read_text())
    offenders = []
    for fn in [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]:
        takes_id = any(a.arg in ("workspace_id", "wid") for a in fn.args.args)
        text = ast.unparse(fn)
        if takes_id and "db.get(Workspace" in text:
            offenders.append(fn.name)
        if takes_id and not any(f"{v}.{m}(user_row" in text or f"{v}.{m}(user," in text
                                for v in ("svc", "ws_svc") for m in ("get", "update", "delete")):
            offenders.append(fn.name)
    assert not offenders, f"workspace routes that do not use the owner-checked lookup: {offenders}"
    # ... and the service's own update/delete resolve the workspace through the owner-checked `get`
    service = ast.parse((HERE / "app" / "services" / "workspace_service.py").read_text())
    methods = {m.name: ast.unparse(m) for c in service.body if isinstance(c, ast.ClassDef) for m in c.body
               if isinstance(m, ast.AsyncFunctionDef)}
    assert "self.get(" in methods["update"] and "self.get(" in methods["delete"]


def test_the_decision_is_documented_where_the_runners_label_is_explained():
    text = " ".join((HERE / "docs" / "RESOURCE_SCOPING.md").read_text().replace("*", "").split())
    assert "personal-only" in text.lower() and "not an access-control list" in text
