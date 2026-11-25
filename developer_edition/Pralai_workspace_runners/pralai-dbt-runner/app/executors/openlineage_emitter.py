"""Build and send the OpenLineage RunEvent for a finished dbt build.

Why this exists rather than the `dbt-ol` wrapper
─────────────────────────────────────────────────
`openlineage-dbt` ships a `dbt-ol` CLI that wraps the dbt command and emits
events from dbt's artifacts. The runner executes dbt **in-process** through
dbtRunner, which that wrapper never sees — so with the package installed and
OPENLINEAGE_URL set, nothing was ever emitted.

More decisively: dbt-ol cannot produce the field the catalog most depends on.
The integration contract requires every quality assertion to carry an explicit
`dimension`, and states UCS must never fall back to its keyword heuristic. Only
this service knows the dimension — the DQ renderer writes it into each test's
`meta.dq_dimension`, and this module reads it back out of the manifest. A
generic wrapper has no way to know that mapping.

One event per run carries all three signals the catalog wants:
  · lineage   — inputs/outputs derived from each model's dependencies
  · quality   — dataQualityAssertions, one per dbt test that ran
  · schema    — SchemaDatasetFacet columns for every output model

Publishing to UCS (RUN-DBT-007): each call carries a short-lived JWT
scoped to exactly ucs.lineage.write, minted from settings.ucs_internal_token
(see app.core.service_auth.mint_scoped_token) — not self-asserted
X-User-Roles: admin / X-User-Groups: admin headers. Those depended on
every proxy in front of UCS correctly discarding externally supplied
identity headers before UCS ever saw them; a signed, audience-bound,
narrowly-scoped token needs no such assumption on this side of the wire.

Building vs. delivering (RUN-DBT-008): read_and_build_event() does the
pure, network-free half — read dbt's own artifacts, build the event dict —
and is called while the project's working directory still exists (inside
DbtExecutor.run_project(), possibly inside an isolated, DB-less k8s Job
pod). deliver() does the actual HTTP call and is called only by
app.services.metadata_outbox's durable delivery worker, from a durable
outbox row, with retries — never inline from the build path, where a
transient UCS outage would otherwise mean the event is simply lost the
moment the working directory is cleaned up.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import openlineage_sql
import structlog

from app.core.service_auth import UCS_AUDIENCE, mint_scoped_token

log = structlog.get_logger()

_TIMEOUT = 30.0

# Fallback only. A test dbt itself declares (not one of ours) has no
# meta.dq_dimension, so classify it by test type rather than dropping it.
_TYPE_DIMENSION = {
    "not_null": "nulls",
    "unique": "uniqueness",
    "accepted_values": "validity",
    "relationships": "validity",
    "expression_is_true": "validity",
}


def _dimension_for(node: dict) -> str:
    meta = (node.get("meta") or {}) or (node.get("config", {}).get("meta") or {})
    if meta.get("dq_dimension"):
        return str(meta["dq_dimension"])
    name = node.get("name", "")
    for key, dim in _TYPE_DIMENSION.items():
        if name.startswith(key) or f"_{key}_" in name:
            return dim
    return "custom"


def _dataset_ref(node: dict, default_namespace: str) -> dict | None:
    """An OL dataset reference for a model or source node.

    namespace is the catalog, name is schema.table — the shape ns_mapping in UCS
    expects, so `pg_sales` + `public.orders` resolves to pg_sales.public.orders.
    """
    schema = node.get("schema")
    ident = node.get("alias") or node.get("name")
    if not ident:
        return None
    return {
        "namespace": default_namespace,
        "name": f"{schema}.{ident}" if schema else ident,
    }


def _schema_facet(node: dict) -> dict | None:
    cols = node.get("columns") or {}
    fields = [
        {"name": c.get("name", key),
         "type": (c.get("data_type") or "").upper() or "UNKNOWN",
         **({"description": c["description"]} if c.get("description") else {})}
        for key, c in cols.items()
    ]
    return {"fields": fields} if fields else None

class _TableIndex:
    """Parsed-SQL table name -> the dataset ref we publish for it.

    Keyed by SCHEMA-QUALIFIED name (RUN-DBT-006): two models/sources named
    `orders` in different schemas (e.g. raw.orders and staging.orders) used
    to collide on the bare name, the last one silently winning, so column
    lineage could point at the wrong table. A bare (unqualified) name is
    resolved only when exactly one indexed table has it; an ambiguous bare
    name resolves to nothing rather than a guess."""

    def __init__(self) -> None:
        self._qualified: dict[str, dict] = {}
        self._bare: dict[str, list[dict]] = {}

    def add(self, node: dict, ref: dict) -> None:
        ident = (node.get("alias") or node.get("name") or "").lower()
        if not ident:
            return
        schema = (node.get("schema") or "").lower()
        if schema:
            self._qualified[f"{schema}.{ident}"] = ref
        bucket = self._bare.setdefault(ident, [])
        if ref not in bucket:
            bucket.append(ref)

    def lookup(self, name: str) -> dict | None:
        parts = [p.strip('"').lower() for p in str(name or "").split(".") if p]
        if not parts:
            return None
        if len(parts) >= 2:
            hit = self._qualified.get(f"{parts[-2]}.{parts[-1]}")
            if hit is not None:
                return hit
            return None  # qualified, but not one of ours — never guess by bare name
        candidates = self._bare.get(parts[-1]) or []
        return candidates[0] if len(candidates) == 1 else None

    # Mapping-style access kept for callers/tests that index by bare name.
    def get(self, name: str, default=None):
        hit = self.lookup(name)
        return default if hit is None else hit


_REF_RE = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"]\s*)?\)""")


def _ref_target(expr: str | None, nodes: dict) -> str | None:
    """Resolve a `ref('pkg', 'model')` / `ref('model')` expression appearing
    in a generic test's kwargs to the model's unique_id."""
    if not expr:
        return None
    m = _REF_RE.search(str(expr))
    if not m:
        return None
    pkg, name = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(1))
    matches = [uid for uid, n in nodes.items()
               if n.get("resource_type") == "model" and n.get("name") == name
               and (pkg is None or n.get("package_name") == pkg)]
    return matches[0] if len(matches) == 1 else None


def _tested_model(test_node: dict, nodes: dict) -> str | None:
    """The model a test is ATTACHED to (RUN-DBT-006).

    A relationships test depends on two models — the tested one and its
    `to` target — and depends_on.nodes order is not meaningful, so taking
    attached[0] could put the assertion on the referenced model. Resolution
    order: dbt's own `attached_node`; the `model` kwarg of the generic
    test; for relationships, the dependency that is not the `to` model; a
    single model dependency. Otherwise None (dropped, never guessed)."""
    model_deps = [d for d in (test_node.get("depends_on") or {}).get("nodes") or []
                  if d.startswith("model.")]
    attached = test_node.get("attached_node")
    if attached and attached in nodes:
        return attached if attached.startswith("model.") else None
    kwargs = (test_node.get("test_metadata") or {}).get("kwargs") or {}
    via_kwarg = _ref_target(kwargs.get("model"), nodes)
    if via_kwarg and (not model_deps or via_kwarg in model_deps):
        return via_kwarg
    if len(model_deps) == 1:
        return model_deps[0]
    to_model = _ref_target(kwargs.get("to"), nodes)
    if to_model:
        rest = [d for d in model_deps if d != to_model]
        if len(rest) == 1:
            return rest[0]
    return None


def _column_lineage_facet(node: dict, project_dir: Path, namespace: str,
                          by_table: "_TableIndex", nodes: dict, sources: dict) -> dict | None:
    """Derive per-column lineage for one model.

    dbt's manifest records dependencies at *node* level only — it knows
    stg_orders reads raw_orders, not that stg_orders.status came from
    raw_orders.status. Two things make that harder than parsing a SELECT:

    · CTEs. openlineage_sql reports the origin as the CTE alias ("source"),
      not the table behind it, so an origin that is not a known table is
      attributed to the statement's single real input.
    · `select *`. The parser cannot expand a star without a schema, and dbt's
      staging models are almost all `select * from renamed`. Where the manifest
      knows both sides' columns we emit identity edges for the names that exist
      in both — a fact, not a guess. Renamed columns are omitted rather than
      invented.
    """
    sql = node.get("compiled_code") or node.get("compiled_sql")
    if not sql:
        rel = node.get("compiled_path") or node.get("path")
        for candidate in ([project_dir / "target" / "compiled" / rel,
                           project_dir / "target" / "compiled" / node.get("package_name", "") / rel]
                          if rel else []):
            if candidate.exists():
                sql = candidate.read_text(errors="ignore")
                break
    if not sql or not sql.strip():
        return None

    try:
        parsed = openlineage_sql.parse([sql], dialect="postgres")
    except Exception as e:  # noqa: BLE001 — an unparsable dialect is not fatal
        log.debug("column_lineage_parse_failed", node=node.get("unique_id"), error=str(e))
        return None

    # The real upstream tables this statement reads, mapped to the datasets we
    # already publish, so column edges land on the same nodes as table edges.
    in_refs: list[dict] = []
    for t in getattr(parsed, "in_tables", None) or []:
        t_name = str(getattr(t, "name", t))
        t_schema = getattr(t, "schema", None)
        ref = by_table.lookup(f"{t_schema}.{t_name}" if t_schema and "." not in t_name else t_name)
        if ref and ref not in in_refs:
            in_refs.append(ref)

    fields: dict[str, dict] = {}
    for entry in getattr(parsed, "column_lineage", None) or []:
        out_field = getattr(entry.descendant, "name", None)
        if not out_field:
            continue
        inputs = []
        for dep in entry.lineage or []:
            origin = getattr(dep, "origin", None)
            o_name = str(getattr(origin, "name", "") or "")
            o_schema = getattr(origin, "schema", None)
            ref = by_table.lookup(f"{o_schema}.{o_name}" if o_schema and "." not in o_name else o_name)
            if ref is None and len(in_refs) == 1:
                ref = in_refs[0]      # a CTE alias over a single real input
            if ref is None:
                continue
            inputs.append({"namespace": ref["namespace"], "name": ref["name"],
                           "field": getattr(dep, "name", ""),
                           "transformations": [{"type": "IDENTITY", "masking": False}]})
        if inputs:
            fields[out_field] = {"inputFields": inputs}

    # `select *` case. The parser cannot expand a star without a schema, and
    # dbt staging models are almost all `select * from renamed`. Fall back to
    # name matching against the upstreams the manifest documents: a column is
    # attributed only when exactly ONE upstream declares that name, so an
    # ambiguous match is left out rather than guessed at. Columns renamed inside
    # a CTE simply do not appear — omission beats invention.
    if not fields and in_refs:
        upstream_cols: list[tuple[dict, set[str]]] = []
        for ref in in_refs:
            for cand in list(nodes.values()) + list(sources.values()):
                if _dataset_ref(cand, namespace) == ref:
                    cols = {c.lower() for c in (cand.get("columns") or {})}
                    if cols:
                        upstream_cols.append((ref, cols))
                    break

        for col in sorted({c.lower() for c in (node.get("columns") or {})}):
            owners = [ref for ref, cols in upstream_cols if col in cols]
            if len(owners) != 1:
                continue
            fields[col] = {"inputFields": [{
                "namespace": owners[0]["namespace"], "name": owners[0]["name"],
                "field": col, "transformations": [{"type": "IDENTITY", "masking": False}]}]}

    return {"fields": fields} if fields else None


def build_event(*, manifest: dict, run_results: dict, run_id: uuid.UUID,
                project_name: str, namespace: str,
                project_dir: Path | None = None) -> dict | None:
    """Assemble one RunEvent from dbt's own artifacts, or None if there is
    nothing worth sending."""
    nodes: dict[str, dict] = manifest.get("nodes") or {}
    sources: dict[str, dict] = manifest.get("sources") or {}
    results = run_results.get("results") or []

    # Order-preserving de-dup — dbt's run_results list is already one entry
    # per executed node, so this is mostly a safety net rather than a real
    # dedup, but it also means the model list no longer depends on Python
    # set iteration order (arbitrary) the way a set comprehension would.
    model_ids = list(dict.fromkeys(
        uid for uid in (r.get("unique_id") for r in results)
        if uid and uid in nodes and nodes[uid].get("resource_type") == "model"
    ))

    # Outputs keyed by dbt unique_id — never a parallel positional list next
    # to model_ids. A model whose dataset_ref can't be built (e.g. missing
    # alias/name) is simply absent from this dict rather than skipped with
    # a `continue` that would otherwise shift every later output out of
    # alignment with model_ids, misattaching another model's assertions
    # onto it (RUN-DBT-006).
    outputs_by_uid: dict[str, dict] = {}
    inputs: list[dict] = []
    seen_in: set[str] = set()

    # Parsed table name -> the dataset ref we publish for it, keyed by
    # schema-qualified name (see _TableIndex).
    by_table = _TableIndex()
    for uid, n in list(nodes.items()) + list(sources.items()):
        r = _dataset_ref(n, namespace)
        if r:
            by_table.add(n, r)

    for uid in model_ids:
        node = nodes[uid]
        ref = _dataset_ref(node, namespace)
        if not ref:
            continue
        facets: dict[str, Any] = {}
        sch = _schema_facet(node)
        if sch:
            facets["schema"] = sch
        if project_dir is not None:
            cl = _column_lineage_facet(node, project_dir, namespace, by_table, nodes, sources)
            if cl:
                facets["columnLineage"] = cl
        outputs_by_uid[uid] = {**ref, "facets": facets}

        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            dep_node = nodes.get(dep) or sources.get(dep)
            if not dep_node:
                continue
            dref = _dataset_ref(dep_node, namespace)
            key = f"{dref['namespace']}.{dref['name']}" if dref else None
            if dref and key not in seen_in:
                seen_in.add(key)
                inputs.append(dref)

    # Every test that ran becomes an assertion on the model it tests.
    assertions_by_model: dict[str, list[dict]] = {}
    for r in results:
        uid = r.get("unique_id")
        node = nodes.get(uid or "")
        if not node or node.get("resource_type") != "test":
            continue
        tested = _tested_model(node, nodes)
        if not tested:
            continue
        status = str(r.get("status", "")).lower()
        col = (node.get("column_name") or
               (node.get("test_metadata") or {}).get("kwargs", {}).get("column_name"))
        assertion: dict[str, Any] = {
            "assertion": (node.get("meta") or {}).get("dq_rule") or node.get("name", uid),
            "success": status in ("pass", "success"),
            "dimension": _dimension_for(node),
        }
        if col:
            assertion["column"] = {"name": str(col)}
        detail = r.get("message") or (f"{r.get('failures')} failing rows"
                                      if r.get("failures") else None)
        if detail:
            assertion["detail"] = str(detail)[:400]
        assertions_by_model.setdefault(tested, []).append(assertion)

    for uid, assertions in assertions_by_model.items():
        out = outputs_by_uid.get(uid)
        if out is None:
            # The tested model exists in the manifest but never became an
            # output (e.g. no usable dataset_ref) — nothing to attach this
            # to. Dropping it beats silently reattaching it to whichever
            # other model happened to land at the same list position.
            continue
        out["facets"]["dataQualityAssertions"] = {
            "_producer": "https://inaiera.com/dbt-runner",
            "assertions": assertions,
        }

    outputs = list(outputs_by_uid.values())
    if not outputs:
        return None

    return {
        "eventType": "COMPLETE",
        "eventTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": "https://inaiera.com/dbt-runner",
        "run": {"runId": str(run_id)},
        "job": {"namespace": "inaiera.dbt", "name": project_name},
        "inputs": inputs,
        "outputs": outputs,
    }


def read_and_build_event(*, project_dir: Path, run_id: uuid.UUID, namespace: str) -> dict | None:
    """Read dbt's own artifacts from this run's (still-existing) working
    directory and build the event, or None if there is nothing worth
    queueing. Pure I/O + build — no network call, so it works the same
    whether called from an in-process subprocess run or from inside an
    isolated k8s Job pod that has no durable-outbox database access of its
    own (see app.executors.job_entrypoint). The caller is responsible for
    persisting the returned event into the metadata outbox
    (app.services.metadata_outbox) for actual delivery."""
    target = project_dir / "target"
    try:
        manifest = json.loads((target / "manifest.json").read_text())
        run_results = json.loads((target / "run_results.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("openlineage_artifacts_unreadable", run_id=str(run_id), error=str(e))
        return None

    project_name = (manifest.get("metadata") or {}).get("project_name") or "dbt"
    return build_event(manifest=manifest, run_results=run_results, run_id=run_id,
                       project_name=project_name, namespace=namespace, project_dir=project_dir)


def deliver(event: dict, settings, *, idempotency_key: str | None = None) -> dict:
    """Send an already-built event. Called only by the metadata outbox's
    delivery worker (RUN-DBT-008) — never inline from the build path,
    where a transient UCS outage would otherwise mean the event is lost
    the moment the working directory it was read from is cleaned up.
    Never raises: a delivery failure is reported back to the caller, which
    persists it and retries on the next sweep."""
    if not settings.openlineage_url:
        return {"sent": False, "reason": "OPENLINEAGE_URL not configured"}
    if not settings.ucs_internal_token:
        # RUN-DBT-007: no self-asserted identity fallback. Without a
        # credential to mint a scoped token from, the call is skipped
        # rather than sent with no (or a spoofable) identity at all.
        return {"sent": False, "reason": "UCS_INTERNAL_TOKEN not configured"}

    # A short-lived, ucs.lineage.write-scoped JWT instead of self-asserted
    # X-User-Roles: admin / X-User-Groups: admin headers — those depended on
    # every proxy in front of UCS correctly stripping/replacing externally
    # supplied identity headers, an assumption this side of the wire had no
    # way to enforce (RUN-DBT-007). UCS verifies the signature and scope
    # instead of trusting a header arrived unmodified.
    token = mint_scoped_token(
        secret=settings.ucs_internal_token, audience=UCS_AUDIENCE, scope="ucs.lineage.write",
        subject=settings.service_name, org_id=settings.org_id,
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if idempotency_key:
        # RUN-DBT-008: lets UCS collapse outbox retries of the same event.
        headers["Idempotency-Key"] = idempotency_key
    url = f"{settings.openlineage_url.rstrip('/')}/events"
    try:
        r = httpx.post(url, json=event, timeout=_TIMEOUT, headers=headers)
    except httpx.HTTPError as e:
        log.warning("openlineage_deliver_failed", error=str(e))
        return {"sent": False, "reason": f"catalog unreachable: {e}"}

    ok = 200 <= r.status_code < 300
    log.info("openlineage_delivered", status=r.status_code, outputs=len(event["outputs"]),
             inputs=len(event["inputs"]),
             column_fields=sum(len(o["facets"].get("columnLineage", {}).get("fields", {}))
                               for o in event["outputs"]),
             assertions=sum(len(o["facets"].get("dataQualityAssertions", {}).get("assertions", []))
                            for o in event["outputs"]))
    return {"sent": ok, "status": r.status_code, "reason": None if ok else r.text[:200]}
