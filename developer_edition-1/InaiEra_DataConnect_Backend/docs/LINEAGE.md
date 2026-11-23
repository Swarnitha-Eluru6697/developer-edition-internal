# Lineage for a source pipeline

The lineage graph shows the flow a pipeline really has. It is drawn when the pipeline is deployed, and redrawn when a deployed pipeline's quality policy or
quality mode is edited.

| Pipeline | What is drawn |
|---|---|
| No quality policy | source -> connector -> raw topic. Nothing after it. |
| A quality policy | ... -> raw topic -> **the policy** (named `<policy name> (quality policy)`) -> **the quarantine topic** (when the policy routes failures to quarantine on Kafka) |
| The policy, ENFORCE | and also -> the **prepared topic** (in OBSERVE nothing is written to it, so it is not drawn) |
| A policy that only logs (`on_failure=log`) | the policy node, but no quarantine topic |

Edges: `CONNECTS_TO` (source -> connector), `INGESTS_TO` (connector -> raw topic), `VALIDATED_BY` (raw topic -> policy), `QUARANTINES_TO` (policy -> quarantine
topic), `EMITS_TO` (policy -> prepared topic).

The quarantine topic is the one the quality worker writes to (`quarantine-<domain>-<source>-<entity>-validation-<version>`; a sink pipeline's
uses mode `validation_sink`, so a source's and a sink's failures never share a topic). It is named from domain, source system, entity and version
only, so two pipelines of the same direction and entity (for example a CDC and a batch source) share it.

## Staying correct

- Drawing is idempotent, and it also **removes** what the current configuration no longer produces: when a policy is removed, or ENFORCE is switched back to
  OBSERVE, the old processor / prepared / quarantine edges are deleted, and a processor or prepared/quarantine topic left with no edge at all is deleted from
  the catalog. A quarantine topic still used by another pipeline is kept.
- Removing a quality policy through an edit now really removes it (an edit with `quality_policy_id: null` used to be ignored, so the pipeline kept enforcing).
- A pipeline's graph, and the asset list and counts the Lineage page shows for it, contain only that pipeline's assets (plus anything the user expands).

Pipelines deployed before this change keep their old drawing until they are redeployed or edited. To redraw them all at once, call
`lineage_service.materialize_pipeline_lineage(pipeline, connection)` for each deployed source pipeline (it is safe to repeat).

Sink pipelines are unchanged: input topic(s) -> connector -> sink target.

Tests: `tests/test_lineage_quality_flow_real_db.py` (real PostgreSQL, `DATACONNECT_REAL_DB_TESTS=1`).
