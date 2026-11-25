"""
Live runtime flow metrics — reads the REAL Kafka Connect + Kafka, so the UI can
show how many records a source loaded into a topic and how many a sink wrote out.

- Connector states come from the Kafka Connect REST API (settings.kafka_connect_url).
- Topic message counts (end-offset − begin-offset) and sink consumer-group progress
  (committed offset + lag) come from Kafka (settings.kafka_bootstrap) via kafka-python.

Everything degrades gracefully: if Connect or Kafka is unreachable, it returns a
`reachable` flag + message instead of failing.

Both clusters are shared infrastructure — Connect may run connectors, and
Kafka may host topics, belonging to another DataConnect org or to an
application outside DataConnect entirely. `flow_summary` takes an `org_id`
and filters the raw connector/topic listing down to what
app/services/runtime_ownership.py resolves as this org's own, attaching the
owning `pipeline_id` to each connector/topic — never a raw, unattributed
dump of everything the shared cluster happens to host.
"""

import asyncio

import httpx
import structlog

from app.core.config import settings
from app.services.connect_runtime import pool_endpoint, pool_of
from app.services.runtime_ownership import pipeline_by_connector_name, pipeline_by_topic_name

logger = structlog.get_logger(__name__)

_INTERNAL_PREFIXES = ("_", "__", "connect-", "_confluent", "_schemas")


async def _connect_get(path: str, base_url: str | None = None):
    """GET against one Kafka Connect cluster (a worker pool's — default: the shared one)."""
    async with httpx.AsyncClient(base_url=base_url or settings.kafka_connect_url, timeout=8) as client:
        r = await client.get(path)
        r.raise_for_status()
        return r.json()


# One call for every connector's status AND config. The per-connector `GET /connectors/{name}` is queued on the
# worker's herder thread, which is busy for minutes after a restart (starting 100+ connectors, some blocking 30s on
# unreachable hosts) — measured at 40-70s EACH, so a per-connector loop never finished. The expanded listing is read
# from the worker's in-memory snapshot and answers in milliseconds regardless.
_BULK_PATH = "/connectors?expand=status&expand=info"


async def _read_cluster_connectors(url: str, expected: set[str]) -> list[tuple[str, dict, dict]]:
    """(name, status, config) of this cluster's connectors that are in `expected`."""
    bulk = await _connect_get(_BULK_PATH, url)
    if isinstance(bulk, dict):
        return [(n, v.get("status") or {}, (v.get("info") or {}).get("config") or {})
                for n, v in bulk.items() if n in expected]
    # Connect older than 2.3 ignores expand= and returns bare names: read each one (the slow path).
    out: list[tuple[str, dict, dict]] = []
    for n in bulk:
        if n not in expected:
            continue
        try:
            st = await _connect_get(f"/connectors/{n}/status", url)
            conf = (await _connect_get(f"/connectors/{n}", url)).get("config", {})
        except Exception:
            st, conf = {}, {}
        out.append((n, st, conf))
    return out


def _kafka_metrics(bootstrap: str, sinks: list[dict]) -> dict:
    """Blocking Kafka calls (run in a thread). Topic counts + per-sink consumed/lag.

    Sink progress is read via a group-scoped consumer's committed() offsets (works on
    every kafka-python build); each sink's topics come from its connector config, and
    its group is Kafka Connect's `connect-<connectorName>`.
    """
    from kafka import KafkaConsumer
    from kafka.structs import TopicPartition

    consumer = KafkaConsumer(bootstrap_servers=bootstrap, request_timeout_ms=8000,
                             enable_auto_commit=False, consumer_timeout_ms=4000)
    out = {"topics": {}, "sinks": {}}
    try:
        # topic message counts (independent — a sink failure must not lose these)
        try:
            for t in sorted(x for x in consumer.topics() if not x.startswith(_INTERNAL_PREFIXES)):
                parts = consumer.partitions_for_topic(t) or set()
                tps = [TopicPartition(t, p) for p in parts]
                if not tps:
                    out["topics"][t] = 0
                    continue
                end = consumer.end_offsets(tps)
                beg = consumer.beginning_offsets(tps)
                out["topics"][t] = int(sum(end[tp] - beg[tp] for tp in tps))
        except Exception as exc:
            logger.warning("runtime_metrics.topics_failed", error=str(exc))

        # per-sink progress via its consumer group's committed offsets
        out["sinks"] = _sink_progress(bootstrap, sinks, consumer)
        return out
    finally:
        try:
            consumer.close()
        except Exception:
            pass


def _sink_progress(bootstrap: str, sinks: list[dict], consumer) -> dict:
    """Per sink connector: records its consumer group has committed (`consumed`) and has NOT yet committed (`lag` = end offset −
    committed, summed over the partitions of its topics). This is the real backlog: Connect's own JMX `records-lag-max` is measured
    at fetch time, so it reads 0 whenever the consumer has fetched everything, even while those records are still being written,
    and it stops changing while the connector is paused.

    The lag is measured from the partition's log start when that is past the commit (or there is no commit yet): records retention
    has already deleted can never be delivered, so counting them showed a backlog that no running sink could ever drain."""
    from kafka import KafkaConsumer
    from kafka.structs import TopicPartition

    progress: dict = {}
    for s in sinks:
        name = s["name"]
        group = s.get("group") or f"connect-{name}"     # a sink connector's own group, unless the entry names another (a quality worker's)
        topics = s.get("topics") or []
        tps = [TopicPartition(t, p) for t in topics for p in (consumer.partitions_for_topic(t) or set())]
        if not tps:
            continue
        gc = KafkaConsumer(bootstrap_servers=bootstrap, group_id=group, enable_auto_commit=False, request_timeout_ms=8000)
        try:
            gc.assign(tps)
            end = consumer.end_offsets(tps)
            start = consumer.beginning_offsets(tps)
            consumed = lag = 0
            for tp in tps:
                committed = gc.committed(tp) or 0
                consumed += committed
                lag += max(0, end.get(tp, 0) - max(committed, start.get(tp) or 0))
            progress[name] = {"consumed": int(consumed), "lag": int(lag), "topics": sorted(topics)}
        except Exception as exc:
            logger.warning("runtime_metrics.sink_group_failed", group=group, error=str(exc))
        finally:
            try:
                gc.close()
            except Exception:
                pass
    return progress


_QUALITY_STAGE = "#quality"   # marks a sink's quality-worker entry; its lag is added to the sink's own


def _sink_backlogs_blocking(bootstrap: str, sinks: list[dict]) -> dict[str, int]:
    from kafka import KafkaConsumer

    consumer = KafkaConsumer(bootstrap_servers=bootstrap, request_timeout_ms=8000, enable_auto_commit=False)
    try:
        backlogs: dict[str, int] = {}
        for name, v in _sink_progress(bootstrap, sinks, consumer).items():
            sink = name.removesuffix(_QUALITY_STAGE)
            backlogs[sink] = backlogs.get(sink, 0) + v["lag"]
        return backlogs
    finally:
        try:
            consumer.close()
        except Exception:
            pass


_BACKLOG_CACHE: dict[str, tuple[float, dict[str, int]]] = {}
BACKLOG_CACHE_SECONDS = 5.0


def connector_input_topics(pipeline: dict) -> list[str]:
    """The topics a sink's CONNECTOR consumes, which is what its consumer group's offsets are about. An ENFORCE sink's connector reads the
    pipeline's prepared topic (connect_config._sink_topic), not the input topic in its settings — that one is read by the quality worker.
    Measuring the group against the input topic counted the whole input topic as unread: 2026-11-02, two complete quality sinks showed a
    lag of 141,001 each (the Dashboard's "Consumer lag" 282.0k) while their groups had committed every prepared record."""
    from app.services.pipeline_topics import input_topics
    from app.services.quality_mode import prepared_topic_for

    prepared = prepared_topic_for(pipeline)
    return [prepared] if prepared else input_topics(pipeline)


async def sink_backlogs(org_id: str) -> dict[str, int] | None:
    """The real backlog (records not yet committed) of each of this org's deployed SINK connectors, from Kafka committed offsets (for an
    ENFORCE sink: what its connector has not written from the prepared topic PLUS what its quality worker has not read from the input) —
    the same numbers the Pipelines page shows. A connector missing from the result has no readable group yet; None = Kafka could
    not be read at all. Cached for a few seconds: the Monitoring page and the Dashboard both poll it."""
    import time as _time

    cached = _BACKLOG_CACHE.get(org_id)
    if cached and _time.monotonic() - cached[0] < BACKLOG_CACHE_SECONDS:
        return cached[1]
    from app.services.pipeline_topics import input_topics
    from app.services.quality_mode import prepared_topic_for, quality_consumer_group

    owned = await pipeline_by_connector_name(org_id)
    sinks = []
    for name, p in owned.items():
        if (p.get("direction") or "").upper() != "SINK" or not connector_input_topics(p):
            continue
        sinks.append({"name": name, "topics": connector_input_topics(p)})
        if prepared_topic_for(p) and input_topics(p):
            # ENFORCE: a record reaches the table only after the quality worker has passed it on, so what the worker has not read yet
            # is backlog too (2026-11-02: up to ~140,000 records for 8 minutes that no lag figure showed). OBSERVE: the sink reads the
            # data itself and never waits for the worker, so its progress is not this sink's backlog.
            sinks.append({"name": name + _QUALITY_STAGE, "topics": input_topics(p), "group": quality_consumer_group(p)})
    if not sinks:
        return {}
    try:
        backlogs = await asyncio.to_thread(_sink_backlogs_blocking, settings.kafka_bootstrap, sinks)
    except Exception as exc:
        logger.warning("runtime_metrics.sink_backlog_failed", org_id=org_id, error=str(exc))
        return None
    _BACKLOG_CACHE[org_id] = (_time.monotonic(), backlogs)
    return backlogs


async def flow_summary(org_id: str) -> dict:
    result = {"reachable": False, "connect_url": settings.kafka_connect_url, "bootstrap": settings.kafka_bootstrap,
              "connectors": [], "topics": [], "sinks": [], "message": ""}

    owned_connectors = await pipeline_by_connector_name(org_id)
    owned_topics = await pipeline_by_topic_name(org_id)

    # 1) connector states from Kafka Connect — filtered to this org's own
    # pipelines before anything else touches the raw cluster-wide list. Each connector is read from the cluster its worker
    # pool runs on (app/services/connect_runtime.py); a pool that is down only drops ITS connectors from the answer.
    expected_by_cluster: dict[str, set[str]] = {}
    for name, pipeline in owned_connectors.items():
        expected_by_cluster.setdefault(pool_endpoint(pool_of(pipeline)), set()).add(name)
    expected_by_cluster = expected_by_cluster or {pool_endpoint(None): set()}  # nothing deployed: still probe reachability
    found: list[tuple[str, dict, dict]] = []
    unreachable: list[str] = []
    for url, expected in expected_by_cluster.items():
        try:
            found.extend(await _read_cluster_connectors(url, expected))
        except Exception as exc:
            unreachable.append(f"{url}: {exc}")
    if unreachable and not found and len(unreachable) == len(expected_by_cluster):
        result["message"] = f"Kafka Connect not reachable at {'; '.join(unreachable)}"
        return result

    connectors = [{
        "name": n,
        "pipeline_id": owned_connectors[n]["id"],
        "type": st.get("type", "unknown"),
        "state": st.get("connector", {}).get("state", "UNKNOWN"),
        "tasks": [t.get("state") for t in st.get("tasks", [])],
        "_topics_cfg": conf.get("topics"),          # sink: explicit topic list
        "_prefix": conf.get("topic.prefix"),        # source: topic name prefix
    } for n, st, conf in found]
    result["connectors"] = connectors
    result["reachable"] = not unreachable
    if unreachable:
        result["message"] = f"Kafka Connect not reachable at {'; '.join(unreachable)}"

    # 2) topic message counts + sink progress from Kafka — again filtered to
    # this org's own topics; _kafka_metrics itself still has to enumerate
    # every topic on the broker to compute counts, but nothing beyond an
    # owned topic name/count ever reaches the response.
    try:
        sink_info = [{"name": c["name"], "topics": [t.strip() for t in (c.get("_topics_cfg") or "").split(",") if t.strip()]}
                     for c in connectors if c.get("type") == "sink"]
        km = await asyncio.to_thread(_kafka_metrics, settings.kafka_bootstrap, sink_info)
        result["topics"] = [
            {"topic": t, "messages": c, "pipeline_id": owned_topics[t]["id"]}
            for t, c in km["topics"].items()
            if t in owned_topics
        ]
        result["sinks"] = [{"connector": n, **v} for n, v in km["sinks"].items() if n in owned_connectors]
    except Exception as exc:
        kafka_message = f"Connectors read OK, but Kafka metrics unavailable at {settings.kafka_bootstrap}: {exc}"
        # Keep a Connect-cluster-unreachable note from above rather than replacing it.
        result["message"] = f"{result['message']}; {kafka_message}" if result["message"] else kafka_message
        logger.warning("runtime_metrics.kafka_unavailable", error=str(exc))

    # 3) attach the topic list to each connector (source: prefix-match; sink: config) for lineage
    topic_names = [t["topic"] for t in result["topics"]]
    for c in connectors:
        if c.get("type") == "sink":
            c["topics"] = [t.strip() for t in (c.get("_topics_cfg") or "").split(",") if t.strip()]
        else:
            pref = c.get("_prefix") or ""
            c["topics"] = [t for t in topic_names if pref and t.startswith(pref)]
        c.pop("_topics_cfg", None)
        c.pop("_prefix", None)
    return result
