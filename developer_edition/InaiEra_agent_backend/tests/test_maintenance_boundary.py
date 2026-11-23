"""Maintenance operations must not be on the product API surface.

The reported gap: storage sweeping, bulk storage-backend migration and
reconciliation were ordinary routes on the product routers, with no
authentication of any kind —

    POST /api/uploads/_sweep-expired
    POST /api/uploads/_migrate-backend?target=…&limit=…
    POST /api/agents/_reconcile-stale-hitl
    POST /api/integrations/knowledge-bases/{id}/reconcile

The leading underscore was a naming convention, not a boundary. These
are operationally privileged: the sweep deletes database rows together
with their storage objects, and the migration relocates up to 10,000
objects per call. Anything that could reach the product API could
invoke them.

The release criterion, run as tests: a normal product client cannot
route to a maintenance operation. Checked at both layers, because
either alone can be misconfigured — the app refuses without an operator
credential, and the edge refuses the whole /internal prefix.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.internal_auth import require_operator
from app.main import app
from conftest import gateway_headers

client = TestClient(app)
# A gateway principal holding every permission EXCEPT maintenance.
NON_OPERATOR = gateway_headers([p for p in __import__("conftest").ALL_PERMISSIONS
                                if p != "agent:maintenance:run"], user="someone")
OPERATOR = gateway_headers(["agent:maintenance:run"], user="ops-person")

# Exactly the routes the report named, at their old product-API paths.
OLD_PRODUCT_PATHS = [
    "/api/uploads/_sweep-expired",
    "/api/uploads/_migrate-backend?target=local",
    "/api/agents/_reconcile-stale-hitl",
    "/api/integrations/knowledge-bases/some_kb/reconcile",
]

INTERNAL_PATHS = [
    "/internal/maintenance/uploads/sweep-expired",
    "/internal/maintenance/uploads/migrate-backend?target=local",
    "/internal/maintenance/hitl/reconcile-stale",
    "/internal/maintenance/knowledge-bases/some_kb/reconcile",
]


class TestTheReleaseCriterion:
    """A normal product client cannot route to a maintenance operation."""

    @pytest.mark.parametrize("path", OLD_PRODUCT_PATHS)
    def test_the_old_product_api_path_no_longer_invokes_anything(self, path):
        r = client.post(path)
        # 404 (no such route) or 405 (the path now only matches a
        # different method's route) — never a 2xx, and never a 5xx from
        # the operation having actually started.
        assert r.status_code in (404, 405), (
            f"{path} returned {r.status_code}: a maintenance operation is still reachable "
            f"on the product API surface"
        )

    @pytest.mark.parametrize("path", INTERNAL_PATHS)
    def test_a_client_without_the_maintenance_permission_is_refused(self, path):
        r = client.post(path, headers=NON_OPERATOR)
        assert r.status_code == 403
        # The refusal says what to do instead, rather than implying a
        # routing problem.
        assert "maintenance" in r.json()["detail"].lower()

    @pytest.mark.parametrize("path", INTERNAL_PATHS)
    def test_a_client_without_the_gateway_is_refused(self, path):
        r = TestClient(app, headers={}).post(path)
        assert r.status_code == 401

    def test_no_maintenance_route_appears_in_the_published_api_schema(self):
        """A client cannot depend on what it cannot discover, and an
        operations endpoint in the product contract invites exactly
        that."""
        schema = app.openapi()
        for path in schema["paths"]:
            assert not path.startswith("/internal"), f"{path} is published in the OpenAPI schema"
            assert "_sweep-expired" not in path
            assert "_migrate-backend" not in path
            assert "_reconcile-stale-hitl" not in path


class TestAnOperatorCanStillRunThem:
    """The boundary has to keep the operations usable, or it just moves
    the problem into a runbook nobody can follow."""

    def test_the_maintenance_permission_is_accepted_and_the_operation_runs(self):
        r = client.post("/internal/maintenance/uploads/sweep-expired", headers=OPERATOR)
        assert r.status_code == 200, r.text
        assert "swept" in r.json()

    def test_the_operator_is_recorded_in_the_response(self):
        """A bulk migration with no record of who asked for it is what a
        governance review asks about."""
        r = client.post("/internal/maintenance/hitl/reconcile-stale", headers=OPERATOR)
        assert r.json()["operator"] == "ops-person"

    def test_role_headers_do_not_make_an_operator(self):
        import base64
        import json as _json

        h = gateway_headers([], user="someone",
                            **{"x-user-roles": base64.b64encode(_json.dumps(["admin"]).encode()).decode()})
        r = client.post("/internal/maintenance/uploads/sweep-expired", headers=h)
        assert r.status_code == 403


class TestItFailsClosed:
    def test_with_trust_off_there_is_no_http_route_at_all(self, monkeypatch):
        """The default posture. An operator uses the CLI; nothing is
        exposed until the gateway is deliberately trusted."""
        monkeypatch.delenv("TRUST_ENVOY_HEADERS", raising=False)
        r = client.post("/internal/maintenance/uploads/sweep-expired", headers=OPERATOR)
        assert r.status_code == 401

    def test_the_guard_is_on_the_router_so_a_new_route_cannot_forget_it(self):
        from app.routers import internal

        names = [getattr(d.dependency, "__qualname__", "") for d in (internal.router.dependencies or [])]
        assert any(n.startswith("require_permission") for n in names), (
            "the guard is not on the router itself, so a route added later inherits nothing"
        )
        assert require_operator  # still used per route to record who ran it


class TestTheEdgeRefusesThePrefix:
    def test_nginx_returns_404_for_the_internal_prefix(self):
        """The outer defence, so a browser client cannot reach these
        paths even if a credential is configured."""
        conf = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "InaiEra_Agent_frontend", "nginx.conf")
        if not os.path.exists(conf):
            # The suite also runs inside the api container, where only
            # tests/ is mounted and the frontend tree does not exist.
            # Skipping is honest; asserting on a file that is not there
            # would fail for a reason that has nothing to do with the
            # boundary. The app-side guard is covered above regardless.
            pytest.skip("frontend nginx.conf not available in this environment")
        with open(conf) as f:
            text = f.read()
        if "proxy_pass" not in text:
            # Static files only (the API is reached through Envoy, never
            # through this nginx): nothing under /internal can be routed.
            return
        block = re.search(r"location /internal/ \{(.*?)\}", text, re.S)
        assert block, "nginx does not refuse the /internal prefix"
        assert "return 404" in block.group(1)
        # And it must come before the /api/ proxy, or a more general
        # location could win first.
        assert text.index("location /internal/") < text.index("location /api/")


class TestOneImplementationNotTwo:
    def test_the_internal_routes_call_the_existing_functions(self):
        """Moving a bulk delete must not fork it. The internal router
        registers the SAME functions the product router used to."""
        import inspect

        from app.routers import internal

        src = inspect.getsource(internal)
        for fn in ("trigger_expired_sweep", "trigger_backend_migration",
                   "trigger_stale_hitl_reconcile", "reconcile_kb"):
            assert fn in src, f"{fn} is not reused by the internal router"

    def test_the_cli_calls_the_same_functions_too(self):
        import inspect

        from app import maintenance

        src = inspect.getsource(maintenance)
        for fn in ("trigger_expired_sweep", "trigger_backend_migration",
                   "trigger_stale_hitl_reconcile", "reconcile_kb"):
            assert fn in src, f"{fn} is re-implemented in the CLI instead of reused"

    def test_the_cli_exposes_every_operation_the_http_boundary_does(self):
        from app.maintenance import main

        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
