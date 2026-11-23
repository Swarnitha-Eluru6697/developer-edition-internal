"""Tests for RUN-DBT-006: build_event() used to build `outputs` (skipping
any model whose dataset_ref failed) and `model_ids` (every executed
model, unfiltered) as two separate lists and then zip() them together to
attach each test's assertions to "the output at the same index." The
moment one model in the middle has no dataset_ref, every output after it
shifts by one position relative to model_ids — so a later model's
assertions land on the wrong dataset, or on none at all.

The fixture below is exactly the scenario the finding asks for: three
executed models where the middle one has no usable dataset_ref (no
alias/name), each of the outer two carrying its own distinct test. The
whole point of these tests is proving each assertion ends up on the
right model's output — not merely that some non-empty list comes out.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import Settings
from app.core.service_auth import UCS_AUDIENCE
from app.executors import openlineage_emitter
from app.executors.openlineage_emitter import build_event, deliver, read_and_build_event

NAMESPACE = "pg_sales"


def _model(unique_id: str, name: str, *, schema: str = "public", alias: str | None = None,
           depends_on: list[str] | None = None) -> dict:
    return {
        "resource_type": "model",
        "name": name,
        "schema": schema,
        "alias": alias,
        "columns": {},
        "depends_on": {"nodes": depends_on or []},
    }


def _test(unique_id: str, name: str, *, depends_on_model: str, dq_rule: str | None = None) -> dict:
    node = {
        "resource_type": "test",
        "name": name,
        "depends_on": {"nodes": [depends_on_model]},
        "test_metadata": {"kwargs": {}},
    }
    if dq_rule:
        node["meta"] = {"dq_rule": dq_rule}
    return node


def _result(unique_id: str, status: str = "success") -> dict:
    return {"unique_id": unique_id, "status": status}


def test_middle_model_without_dataset_ref_does_not_shift_assertions():
    """The exact RUN-DBT-006 regression: model_b (no name/alias, so
    _dataset_ref() returns None) sits between model_a and model_c in
    execution order. Each of model_a and model_c has its own test. With
    the old zip(outputs, model_ids) logic, model_a's output would steal
    whatever model_b's slot resolved to (nothing, since b has no test) and
    model_c's test would misattach to model_a's output instead of its
    own — or be dropped entirely."""
    manifest = {
        "nodes": {
            "model.demo.model_a": _model("model.demo.model_a", "model_a"),
            "model.demo.model_b": _model("model.demo.model_b", "model_b_no_ref", alias=None),
            "model.demo.model_c": _model("model.demo.model_c", "model_c"),
            "test.demo.test_on_a": _test("test.demo.test_on_a", "not_null_model_a_id",
                                         depends_on_model="model.demo.model_a", dq_rule="a_rule"),
            "test.demo.test_on_c": _test("test.demo.test_on_c", "not_null_model_c_id",
                                         depends_on_model="model.demo.model_c", dq_rule="c_rule"),
        },
        "sources": {},
    }
    # model_b is deliberately given no `schema`/no `alias`/no `name` that
    # _dataset_ref() can use — override it to have neither.
    manifest["nodes"]["model.demo.model_b"] = {
        "resource_type": "model", "name": "", "schema": None, "alias": None,
        "columns": {}, "depends_on": {"nodes": []},
    }

    run_results = {"results": [
        _result("model.demo.model_a"),
        _result("model.demo.model_b"),
        _result("model.demo.model_c"),
        _result("test.demo.test_on_a", status="pass"),
        _result("test.demo.test_on_c", status="fail"),
    ]}

    event = build_event(manifest=manifest, run_results=run_results, run_id=uuid.uuid4(),
                        project_name="demo", namespace=NAMESPACE)

    assert event is not None
    outputs_by_name = {o["name"]: o for o in event["outputs"]}

    # model_b never became an output at all — it had nothing to attach to.
    assert set(outputs_by_name) == {"public.model_a", "public.model_c"}

    a_assertions = outputs_by_name["public.model_a"]["facets"]["dataQualityAssertions"]["assertions"]
    c_assertions = outputs_by_name["public.model_c"]["facets"]["dataQualityAssertions"]["assertions"]

    assert [a["assertion"] for a in a_assertions] == ["a_rule"]
    assert a_assertions[0]["success"] is True
    assert [a["assertion"] for a in c_assertions] == ["c_rule"]
    assert c_assertions[0]["success"] is False


def test_assertion_for_a_model_with_no_dataset_ref_is_dropped_not_misattached():
    """If the model a test targets itself has no dataset_ref, its
    assertion has nowhere correct to go — it must be dropped, never
    silently reassigned to a different model's output."""
    manifest = {
        "nodes": {
            "model.demo.model_a": _model("model.demo.model_a", "model_a"),
            "model.demo.model_b": {
                "resource_type": "model", "name": "", "schema": None, "alias": None,
                "columns": {}, "depends_on": {"nodes": []},
            },
            "test.demo.test_on_b": _test("test.demo.test_on_b", "not_null_model_b_id",
                                         depends_on_model="model.demo.model_b", dq_rule="b_rule"),
        },
        "sources": {},
    }
    run_results = {"results": [
        _result("model.demo.model_a"),
        _result("model.demo.model_b"),
        _result("test.demo.test_on_b", status="pass"),
    ]}

    event = build_event(manifest=manifest, run_results=run_results, run_id=uuid.uuid4(),
                        project_name="demo", namespace=NAMESPACE)

    assert event is not None
    assert len(event["outputs"]) == 1
    only_output = event["outputs"][0]
    assert only_output["name"] == "public.model_a"
    assert "dataQualityAssertions" not in only_output["facets"]


def test_multiple_assertions_on_the_same_model_all_attach_to_it():
    manifest = {
        "nodes": {
            "model.demo.model_a": _model("model.demo.model_a", "model_a"),
            "test.demo.t1": _test("test.demo.t1", "unique_model_a_id",
                                  depends_on_model="model.demo.model_a", dq_rule="rule_1"),
            "test.demo.t2": _test("test.demo.t2", "not_null_model_a_id",
                                  depends_on_model="model.demo.model_a", dq_rule="rule_2"),
        },
        "sources": {},
    }
    run_results = {"results": [
        _result("model.demo.model_a"),
        _result("test.demo.t1", status="pass"),
        _result("test.demo.t2", status="pass"),
    ]}

    event = build_event(manifest=manifest, run_results=run_results, run_id=uuid.uuid4(),
                        project_name="demo", namespace=NAMESPACE)

    assert event is not None
    assert len(event["outputs"]) == 1
    assertions = event["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"]
    assert {a["assertion"] for a in assertions} == {"rule_1", "rule_2"}


def test_no_models_built_returns_none():
    manifest = {"nodes": {}, "sources": {}}
    run_results = {"results": []}
    assert build_event(manifest=manifest, run_results=run_results, run_id=uuid.uuid4(),
                       project_name="demo", namespace=NAMESPACE) is None


def test_model_with_no_tests_has_no_assertions_facet():
    manifest = {
        "nodes": {"model.demo.model_a": _model("model.demo.model_a", "model_a")},
        "sources": {},
    }
    run_results = {"results": [_result("model.demo.model_a")]}

    event = build_event(manifest=manifest, run_results=run_results, run_id=uuid.uuid4(),
                        project_name="demo", namespace=NAMESPACE)

    assert event is not None
    assert "dataQualityAssertions" not in event["outputs"][0]["facets"]


# ---------------------------------------------------------------------
# RUN-DBT-007: deliver() used to send self-asserted X-User-Roles: admin /
# X-User-Groups: admin headers alongside the event — security that
# depended entirely on some proxy in front of UCS stripping/replacing any
# such header an external caller might send. It now sends a short-lived,
# ucs.lineage.write-scoped JWT instead.
#
# (RUN-DBT-008 later split the old combined emit() into
# read_and_build_event() — pure, no network, used by DbtExecutor — and
# deliver() — the actual send, used only by the metadata outbox's
# delivery worker. These tests exercise deliver() directly since that is
# where the header/token behavior itself now lives.)
# ---------------------------------------------------------------------
def _write_dbt_artifacts(project_dir) -> None:
    target = project_dir / "target"
    target.mkdir(parents=True)
    manifest = {
        "metadata": {"project_name": "demo"},
        "nodes": {"model.demo.model_a": _model("model.demo.model_a", "model_a")},
        "sources": {},
    }
    run_results = {"results": [_result("model.demo.model_a")]}
    (target / "manifest.json").write_text(json.dumps(manifest))
    (target / "run_results.json").write_text(json.dumps(run_results))


def test_deliver_sends_a_scoped_bearer_token_not_admin_identity_headers(tmp_path, monkeypatch):
    _write_dbt_artifacts(tmp_path)
    event = read_and_build_event(project_dir=tmp_path, run_id=uuid.uuid4(), namespace=NAMESPACE)
    assert event is not None
    settings = Settings(
        openlineage_url="http://fake-ucs/api/v1/lineage", ucs_internal_token="ucs-secret",
        service_name="dbtrunner", org_id="org-42",
    )

    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return httpx.Response(200, request=httpx.Request("POST", url), json={"status": "ok"})

    monkeypatch.setattr(openlineage_emitter.httpx, "post", fake_post)

    result = deliver(event, settings)

    assert result["sent"] is True
    headers = captured["headers"]
    # The exact vulnerability: no self-asserted identity headers at all.
    for banned in ("X-User-Roles", "X-User-Groups", "X-User-Name", "X-Tenant-Id"):
        assert banned not in headers

    auth = headers["Authorization"]
    assert auth.startswith("Bearer ")
    claims = jwt.decode(auth.removeprefix("Bearer "), "ucs-secret",
                        algorithms=["HS256"], audience=UCS_AUDIENCE)
    assert claims["scope"] == "ucs.lineage.write"
    assert claims["sub"] == "dbtrunner"
    assert claims["org_id"] == "org-42"


def test_deliver_skips_sending_when_ucs_internal_token_not_configured(tmp_path, monkeypatch):
    _write_dbt_artifacts(tmp_path)
    event = read_and_build_event(project_dir=tmp_path, run_id=uuid.uuid4(), namespace=NAMESPACE)
    assert event is not None
    settings = Settings(openlineage_url="http://fake-ucs/api/v1/lineage", ucs_internal_token=None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not call UCS with no credential to mint a token from")

    monkeypatch.setattr(openlineage_emitter.httpx, "post", fail_if_called)

    result = deliver(event, settings)

    assert result == {"sent": False, "reason": "UCS_INTERNAL_TOKEN not configured"}


def test_deliver_a_token_minted_for_ucs_is_rejected_by_a_verifier_expecting_this_services_own_audience(
    tmp_path, monkeypatch,
):
    """Defense-in-depth check on the audience binding itself: even with the
    right secret, a token minted here for UCS must not verify as a token
    meant for THIS service (app.core.service_auth.SERVICE_AUDIENCE)."""
    from app.core.service_auth import SERVICE_AUDIENCE

    _write_dbt_artifacts(tmp_path)
    event = read_and_build_event(project_dir=tmp_path, run_id=uuid.uuid4(), namespace=NAMESPACE)
    assert event is not None
    settings = Settings(
        openlineage_url="http://fake-ucs/api/v1/lineage", ucs_internal_token="shared-secret",
    )
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return httpx.Response(200, request=httpx.Request("POST", url), json={"status": "ok"})

    monkeypatch.setattr(openlineage_emitter.httpx, "post", fake_post)
    deliver(event, settings)

    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    with pytest.raises(JWTError):
        jwt.decode(token, "shared-secret", algorithms=["HS256"], audience=SERVICE_AUDIENCE)


# ---------------------------------------------------------------------
# RUN-DBT-006 (follow-up): relationships tests attach to the TESTED model,
# and table resolution is schema-qualified.
# ---------------------------------------------------------------------
def _rel_test(*, depends_on, model_expr=None, to_expr=None, attached_node=None, dq_rule="rel"):
    kwargs = {"column_name": "customer_id", "field": "id"}
    if model_expr:
        kwargs["model"] = model_expr
    if to_expr:
        kwargs["to"] = to_expr
    node = {"resource_type": "test", "name": "relationships_orders_customer_id__id__ref_customers_",
            "depends_on": {"nodes": depends_on}, "meta": {"dq_rule": dq_rule},
            "test_metadata": {"name": "relationships", "kwargs": kwargs}}
    if attached_node:
        node["attached_node"] = attached_node
    return node


def _two_models():
    return {
        "model.demo.orders": {**_model("model.demo.orders", "orders"), "package_name": "demo"},
        "model.demo.customers": {**_model("model.demo.customers", "customers"), "package_name": "demo"},
    }


def _assertions_by_output(event):
    return {o["name"]: [a["assertion"] for a in o["facets"].get("dataQualityAssertions", {})
                        .get("assertions", [])] for o in event["outputs"]}


@pytest.mark.parametrize("test_node", [
    # `to` model listed first in depends_on, resolved via the `model` kwarg
    _rel_test(depends_on=["model.demo.customers", "model.demo.orders"],
              model_expr="{{ get_where_subquery(ref('orders')) }}", to_expr="ref('customers')"),
    # dbt's attached_node wins
    _rel_test(depends_on=["model.demo.customers", "model.demo.orders"],
              attached_node="model.demo.orders"),
    # no model kwarg: the dependency that is not the `to` target
    _rel_test(depends_on=["model.demo.customers", "model.demo.orders"], to_expr="ref('demo', 'customers')"),
])
def test_relationships_test_attaches_to_tested_model_not_its_to_model(test_node):
    nodes = {**_two_models(), "test.demo.rel": test_node}
    run_results = {"results": [_result("model.demo.orders"), _result("model.demo.customers"),
                               _result("test.demo.rel", status="pass")]}
    event = build_event(manifest={"nodes": nodes, "sources": {}}, run_results=run_results,
                        run_id=uuid.uuid4(), project_name="demo", namespace=NAMESPACE)
    by_out = _assertions_by_output(event)
    assert by_out["public.orders"] == ["rel"]
    assert by_out["public.customers"] == []


def test_ambiguous_multi_model_test_is_dropped_not_guessed():
    nodes = {**_two_models(),
             "test.demo.t": {"resource_type": "test", "name": "custom_two_model_check",
                             "depends_on": {"nodes": ["model.demo.customers", "model.demo.orders"]},
                             "test_metadata": {"kwargs": {}}}}
    run_results = {"results": [_result("model.demo.orders"), _result("model.demo.customers"),
                               _result("test.demo.t", status="pass")]}
    event = build_event(manifest={"nodes": nodes, "sources": {}}, run_results=run_results,
                        run_id=uuid.uuid4(), project_name="demo", namespace=NAMESPACE)
    assert all(v == [] for v in _assertions_by_output(event).values())


def test_table_index_is_schema_qualified():
    idx = openlineage_emitter._TableIndex()
    raw = {"namespace": NAMESPACE, "name": "raw.orders"}
    stg = {"namespace": NAMESPACE, "name": "staging.orders"}
    uniq = {"namespace": NAMESPACE, "name": "raw.customers"}
    idx.add({"schema": "raw", "name": "orders"}, raw)
    idx.add({"schema": "staging", "name": "orders"}, stg)
    idx.add({"schema": "raw", "name": "customers"}, uniq)
    assert idx.lookup("raw.orders") == raw
    assert idx.lookup("staging.orders") == stg
    assert idx.lookup('"warehouse"."staging"."orders"') == stg
    assert idx.lookup("orders") is None  # ambiguous bare name: no guess
    assert idx.lookup("customers") == uniq  # unambiguous bare name still resolves
    assert idx.lookup("other.orders") is None


def test_column_lineage_does_not_collide_across_schemas(tmp_path):
    """raw.orders (source) and staging.orders (model) share a bare name;
    a model reading raw.orders must get lineage to raw.orders."""
    nodes = {
        "model.demo.orders": {"resource_type": "model", "name": "orders", "schema": "staging",
                              "alias": None, "columns": {"id": {"name": "id"}},
                              "depends_on": {"nodes": []}},
        "model.demo.report": {"resource_type": "model", "name": "report", "schema": "marts", "alias": None,
                              "columns": {"id": {"name": "id"}},
                              "compiled_code": "select o.id from raw.orders o",
                              "depends_on": {"nodes": ["source.demo.raw.orders"]}},
    }
    sources = {"source.demo.raw.orders": {"resource_type": "source", "name": "orders", "schema": "raw",
                                          "columns": {"id": {"name": "id"}}}}
    run_results = {"results": [_result("model.demo.orders"), _result("model.demo.report")]}
    # staging.orders indexed LAST would have won the old bare-name dict
    event = build_event(manifest={"nodes": nodes, "sources": sources}, run_results=run_results,
                        run_id=uuid.uuid4(), project_name="demo", namespace=NAMESPACE, project_dir=tmp_path)
    report = next(o for o in event["outputs"] if o["name"] == "marts.report")
    fields = report["facets"]["columnLineage"]["fields"]
    assert fields["id"]["inputFields"][0]["name"] == "raw.orders"


def test_deliver_sends_idempotency_key(tmp_path, monkeypatch):
    _write_dbt_artifacts(tmp_path)
    event = read_and_build_event(project_dir=tmp_path, run_id=uuid.uuid4(), namespace=NAMESPACE)
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(openlineage_emitter.httpx, "post", fake_post)
    deliver(event, Settings(openlineage_url="http://u/l", ucs_internal_token="s"),
            idempotency_key="openlineage:abc")
    assert captured["headers"]["Idempotency-Key"] == "openlineage:abc"
