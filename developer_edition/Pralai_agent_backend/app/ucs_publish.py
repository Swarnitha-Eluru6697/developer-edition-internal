"""What this platform tells UCS about an agent, built from the agent's own row.

UCS catalogues agents but does not know what they ARE — an agent's type, plane, owner,
version, status, model and mode live here, in this service's database. Rather than have
UCS call back into this platform (a runtime dependency that makes the catalog go blank
whenever this service is down), the platform PUSHES what it knows onto the catalog object
whenever it changes. UCS then reads its own store.

Two channels, because UCS models them differently:

  * attributes → the Gravitino MODEL's properties, via /admin/models/properties. These are
    facts about the object, so they belong on the object.
  * lineage + quality → an OpenLineage RunEvent to /api/v1/lineage/events, the same door
    every other producer in the platform uses. Emitting a real event means the agent shows
    up in the lineage graph and its health lands in the quality store, instead of UCS having
    an agent-shaped hole in both.

Every call is best-effort: publishing an attribute must never fail a publish. UCS being
down makes the catalog stale, not this platform broken.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def operational_attributes(session, a) -> dict:
    """The operational facts behind the drawer's API access, traces, logs
    and live-jobs tabs, as catalogue properties.

    UCS has exactly two telemetry sinks — /lineage/events and
    /quality/results — and neither takes log lines, trace spans or a
    count of what is running right now. Properties on the model object
    are the channel that does exist, so these are published as rollups:
    the catalogue carries the shape of the agent's operations, and this
    platform stays the place you go for the individual rows.

    Deliberately rollups and not records. Copying every trace and log
    line into the catalogue would duplicate a growing table into a
    system that has no view for it.
    """
    from sqlalchemy import text as _text
    from .ucs_client import agent_fqn

    row = session.exec(_text("""
        SELECT
          COUNT(*) FILTER (WHERE status IN ('running','queued')) AS live,
          COUNT(*) FILTER (WHERE status = 'paused')              AS paused,
          COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days') AS runs_7d,
          COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days'
                             AND status = 'failed')              AS failed_7d
        FROM run WHERE agent_id = :aid
    """), params={"aid": a.id}).first()
    live, paused, runs_7d, failed_7d = (row or (0, 0, 0, 0))

    traces = session.exec(_text(
        "SELECT COUNT(*) FROM trace WHERE agent_id = :aid"), params={"aid": a.id}).first()
    errors = session.exec(_text(
        "SELECT COUNT(*) FROM agentlog WHERE agent_id = :aid AND level = 'ERROR'"),
        params={"aid": a.id}).first()

    features = getattr(a, "features", None) or {}
    return {
        # API access — how this agent can be invoked at all.
        "agent.api.run_endpoint": f"/api/agents/{a.id}/run",
        "agent.api.mcp_enabled": bool(getattr(a, "mcp_enabled", False)),
        "agent.api.requires_auth": bool(features.get("require_authenticated_user")),
        "agent.api.fqn": agent_fqn(a.id, a.name),
        # Live jobs — a gauge, so it is stamped with when it was taken.
        "agent.live_jobs.running": int(live or 0),
        "agent.live_jobs.paused": int(paused or 0),
        "agent.live_jobs.observed_at": datetime.now(timezone.utc).isoformat(),
        # Traces and logs — counts, not contents.
        "agent.traces.total": int((traces or (0,))[0]),
        "agent.runs.last_7d": int(runs_7d or 0),
        "agent.runs.failed_7d": int(failed_7d or 0),
        "agent.logs.errors_total": int((errors or (0,))[0]),
    }


def agent_attributes(a) -> dict:
    """The agent.* property map for one agent row.

    Mirrors the fields the agent's own object view shows, so the two read identically.
    `type` is composed the way the product writes it ("App · Custom"): the row keeps type
    and subtype apart, and showing "App" alone loses which kind of App it is.
    """
    subtype = (getattr(a, "app_type", None) or getattr(a, "subtype", "") or "").strip()
    typ = getattr(a, "type", "") or ""
    return {
        "agent.type": f"{typ} · {subtype}" if typ and subtype else (typ or subtype or ""),
        "agent.plane": getattr(a, "plane", "") or "",
        "agent.owner": getattr(a, "owner", "") or "",
        "agent.version": getattr(a, "version", "") or "",
        "agent.status": getattr(a, "status", "") or "",
        "agent.model": getattr(a, "model", "") or "",
        "agent.mode": getattr(a, "mode", "") or "",
        # Not shown as a field today, but the catalog is where someone asks "how is this
        # agent doing" — cheap to carry, and it makes the overview answerable later.
        "agent.health": getattr(a, "health_status", "") or "",
        "agent.runs_7d": getattr(a, "runs_7d", 0),
        "agent.cost_7d": getattr(a, "cost_7d", "") or "",
    }


def publish_attributes(a, session=None) -> dict:
    """Push this agent's attributes onto its catalog object. Never raises.

    With a session, the operational rollups go up too; without one the
    call still works and publishes only what the agent row itself knows,
    so existing callers are unaffected.
    """
    from .ucs_sync import _admin_post, UCSSyncError, UCS_AGENT_SCHEMA
    from .ucs_client import agent_slug

    properties = agent_attributes(a)
    if session is not None:
        try:
            properties.update(operational_attributes(session, a))
        except Exception as e:      # noqa: BLE001
            # A failed rollup must not cost the attributes that were
            # already computed.
            logger.warning("Could not compute operational attributes for %s: %s", a.id, e)

    body = {
        "catalog": agent_slug(a.name),
        "schema_name": UCS_AGENT_SCHEMA,
        "name": a.id,
        "properties": properties,
    }
    try:
        return _admin_post("/api/v1/admin/models/properties", body)
    except (UCSSyncError, Exception) as e:      # noqa: BLE001 — deliberately swallowed
        logger.warning("Could not publish attributes for agent %s to UCS: %s", a.id, e)
        return {"status": "skipped", "error": str(e)[:200]}


def emit_quality_and_lineage(a, session=None, run_id: str = "", state: str = "COMPLETE") -> dict:
    """Publish this agent's MEASURED quality and its lineage, via agent_catalog.

    Deliberately a delegation, not an implementation. agent_catalog.agent_quality already
    measures three dimensions from real tables — accuracy from the latest completed run of
    each eval set, reliability from the success rate over the window, drift from the latest
    eval run — and reports "unknown" for anything it cannot measure.

    This module previously emitted its own `agent_health` assertion derived from the
    agent's health_status column, plus rowCount=runs_7d. That was a SECOND writer into the
    same UCS quality store, so the catalogue showed numbers that disagreed with the agent's
    own Quality tab: 89 / 83.3% / 95.2% here, a single pass-or-fail there. One measurement,
    one publisher — this one.
    """
    from . import agent_catalog

    own = session is None
    if own:
        from .db import get_session
        session = next(get_session())
    try:
        out = {"quality": agent_catalog.publish_quality(session, a)}
        if run_id:
            # Lineage is per RUN, so it is only meaningful when a run produced it.
            out["lineage"] = agent_catalog.publish_lineage(session, a, run_id, state)
        return out
    except Exception as e:                       # noqa: BLE001 — publishing must not fail a publish
        logger.warning("Could not publish quality/lineage for agent %s: %s", a.id, e)
        return {"status": "skipped", "error": str(e)[:200]}


def publish_all(a, run_url: str = "", session=None) -> dict:
    """Everything UCS should know about an agent, for the places that finish a change.

    Attributes are this module's own job — nothing else publishes them. Quality and lineage
    are agent_catalog's, and are delegated so there is exactly one measurement behind each.
    """
    return {
        "attributes": publish_attributes(a),
        **emit_quality_and_lineage(a, session=session),
    }
