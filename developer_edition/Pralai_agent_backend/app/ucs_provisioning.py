"""Making a newly created knowledge base and agent publishable.

Publishing an agent runs two UCS entitlement checks — the agent's own
service principal and the invoking user — against every catalog object
the flow touches. Both must pass. Getting a NEW agent to that point
required five manual steps, four of them outside the product:

  1. create the fileset object for each KB in Gravitino
  2. create a UCS role granting read on it
  3. bind that role to a group
  4. insert a row in the local `role` table (no API existed — SQL only)
  5. publish as a user who is in that group

Steps 1 and 4 are not governance decisions at all. This platform CREATES
the knowledge base, so registering its catalog object is its own job;
and an agent's group binding is plumbing the product simply never
exposed. Both are automated here.

Steps 2 and 3 ARE governance decisions — they say who may read the data
— so they are opt-in, not automatic. With UCS_AUTO_GRANT_GROUP set, a
newly registered KB is also granted to that one group, which is what
makes "publish anything I create" work in a single-tenant deployment.
Left unset, the object is still registered (so the check is answerable
rather than unanswerable) and the grant stays a deliberate act.

Nothing here weakens the check itself: every grant it creates is a real
UCS role, visible in the console and revocable there. That is the
difference between provisioning and a bypass.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .ucs_client import UCS_KB_CATALOG, UCS_KB_SCHEMA, kb_fqn
from .ucs_sync import UCSSyncError, _admin_post, _already_exists

logger = logging.getLogger(__name__)

# Gravitino is reachable only by UCS (B03: it authenticates every call and has no
# published port). Schemas and filesets this platform owns are created through the UCS
# admin API, which audits them like any other catalog change.


def _flag(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def auto_register_enabled() -> bool:
    return _flag("UCS_KB_AUTO_REGISTER", "true").lower() in ("1", "true", "yes", "on")


def auto_grant_group() -> Optional[str]:
    """The group a newly registered KB is granted to, if any.

    Deliberately a group NAME rather than a boolean: "grant to everyone"
    is not a thing this should be able to express, and naming the group
    forces whoever sets it to decide who that is.
    """
    return _flag("UCS_AUTO_GRANT_GROUP") or None


def agent_default_group() -> Optional[str]:
    """The group a newly created agent's service principal is bound to.

    The agent-principal half of the entitlement check reads groups from
    this platform's own `role` table, which only the seeder ever wrote —
    so every agent created through the product had none, and that half
    could never pass. This is the default it gets instead.
    """
    return _flag("UCS_AGENT_DEFAULT_GROUP") or auto_grant_group()


# ----------------------------------------------------- catalog objects

def _create_schema(catalog: str, name: str, comment: str) -> dict:
    from . import ucs_sync  # resolved per call so a patched/reloaded _admin_post is used
    return ucs_sync._admin_post("/api/v1/admin/schemas", {"catalog": catalog, "name": name,
                                                 "comment": comment, "properties": {}})


def _create_fileset(catalog: str, schema: str, name: str, location: str, comment: str) -> dict:
    from . import ucs_sync
    return ucs_sync._admin_post("/api/v1/admin/filesets", {
        "catalog": catalog, "schema_name": schema, "name": name,
        "storage_location": location, "comment": comment, "type": "EXTERNAL"})


def ensure_kb_catalog_object(kb_id: str, kb_name: str = "") -> dict:
    """Register this KB as a fileset under app_datasets.docs.

    A fileset and not a table: a KB is a collection of ingested
    documents, and it can be backed by pgvector, Pinecone, Qdrant,
    Weaviate or Milvus — so modelling it as a relational table would be
    wrong for most of them. app_datasets is a fileset catalog and is the
    convention this platform's own seed data already used
    (`app_datasets.docs.support_kb`).

    Idempotent: an object that already exists is success, not an error.
    """
    result = {"fqn": kb_fqn(kb_id), "schema": "exists", "fileset": "exists"}
    try:
        _create_schema(UCS_KB_CATALOG, UCS_KB_SCHEMA,
                       "Knowledge bases managed by the InaiEra Agent Platform.")
        result["schema"] = "created"
    except Exception as e:
        if not _looks_like_already_exists(e):
            logger.warning("could not ensure schema %s.%s: %s", UCS_KB_CATALOG, UCS_KB_SCHEMA, e)
            result["schema"] = _error_detail(e)

    try:
        _create_fileset(UCS_KB_CATALOG, UCS_KB_SCHEMA, kb_id, f"file:/var/inaiera-kb/{kb_id}",
                        (f"Knowledge base '{kb_name or kb_id}' in the InaiEra Agent Platform, "
                         f"kb id '{kb_id}'. Registered for governance only — grant READ_FILESET "
                         f"to let an agent or user retrieve from this KB. The documents are not "
                         f"stored at storageLocation."))
        result["fileset"] = "created"
    except Exception as e:
        if not _looks_like_already_exists(e):
            logger.warning("could not register KB catalog object %s: %s", kb_id, e)
            result["fileset"] = _error_detail(e)
    return result


def _looks_like_already_exists(error: Exception) -> bool:
    """Whether this failure means the object is already registered.

    The RESPONSE BODY has to be read, not just str(error): Gravitino
    reports a duplicate as `code 1004 ... already exists`, but httpx's
    HTTPStatusError stringifies to "Client error '409 Conflict' for url
    …" with no body in it — so checking the message alone made every
    idempotent re-run report an error for something that was fine. Found
    by re-running the backfill and watching it "fail" against objects it
    had just created successfully.
    """
    text = str(error).lower()
    response = getattr(error, "response", None)
    if response is not None:
        try:
            text += " " + (response.text or "").lower()
        except Exception:
            pass
    # UCSSyncError carries UCS's status as "-> 409"; a 409 from the create
    # endpoints means the object is there, whatever the body's wording.
    return ("already exist" in text or "1004" in text or "alreadyexists" in text
            or "-> 409" in text)


def _error_detail(error: Exception) -> str:
    """A failure as the result reports it: UCS's status and message, not just the type.
    UCSSyncError's text is already "POST <path> -> <status>: <body>"."""
    text = str(error).strip() or type(error).__name__
    return f"error: {type(error).__name__}: {text[:300]}"


# -------------------------------------------------------------- grants

# UCS's fileset tiers (app/core/privileges.py, family "fileset"), as Gravitino accepts
# them on a FILESET securable.
FILESET_TIER_PRIVILEGES = {
    "select": ["READ_FILESET"],
    "writer": ["READ_FILESET", "WRITE_FILESET"],
    "owner": ["READ_FILESET", "WRITE_FILESET"],
}


def grant_kb_access(kb_id: str, group_name: str, tier: str = "select",
                    group_source: str = "custom") -> dict:
    """Create the UCS role for this KB object and bind a group to it.

    Both calls treat "already exists" as the desired state, so this is
    safe to run on every KB creation and as a backfill.
    """
    role_name = f"kb-{kb_id}-{tier}"
    out = {"role": role_name, "group": group_name}
    try:
        _admin_post("/api/v1/admin/rbac/roles", {
            "role_name": role_name, "catalog": UCS_KB_CATALOG, "schema_name": UCS_KB_SCHEMA,
            "object_name": kb_id, "object_type": "fileset", "access_level": tier,
            # Named explicitly rather than left to UCS's tier expansion: UCS picks the
            # privilege family from the catalog's provider and falls back to relational
            # when it can't tell, which turned `select` on this fileset into SELECT_TABLE
            # and Gravitino refused the role. These are the fileset tier's own privileges.
            "privileges": FILESET_TIER_PRIVILEGES.get(tier, FILESET_TIER_PRIVILEGES["select"]),
        })
        out["role_status"] = "created"
    except UCSSyncError as e:
        if not _already_exists(e):
            raise
        out["role_status"] = "exists"

    try:
        _admin_post("/api/v1/admin/rbac/groups/bind", {
            "role_name": role_name, "group_name": group_name, "group_source": group_source,
        })
        out["bind_status"] = "bound"
    except UCSSyncError as e:
        if not _already_exists(e):
            raise
        out["bind_status"] = "exists"
    return out


def provision_knowledge_base(kb_id: str, kb_name: str = "") -> dict:
    """Everything a new KB needs to be entitlement-checkable.

    Best-effort by design: a KB must still be created when UCS or
    Gravitino is unreachable, because ingestion and retrieval do not
    depend on them. The outcome is returned and logged so the gap is
    visible rather than silent, and `POST /api/integrations/
    knowledge-bases/{id}/provision` re-runs it.
    """
    outcome: dict = {"kb_id": kb_id, "registered": None, "granted": None}
    if not auto_register_enabled():
        outcome["registered"] = "skipped (UCS_KB_AUTO_REGISTER is off)"
        return outcome
    try:
        outcome["registered"] = ensure_kb_catalog_object(kb_id, kb_name)
    except Exception as e:
        outcome["registered"] = f"error: {type(e).__name__}: {e}"
        logger.warning("KB %s was created but not registered in the catalog: %s", kb_id, e)

    group = auto_grant_group()
    if not group:
        outcome["granted"] = ("skipped — set UCS_AUTO_GRANT_GROUP to a custom group name to "
                              "grant read on new knowledge bases automatically")
        return outcome
    try:
        outcome["granted"] = grant_kb_access(kb_id, group)
    except Exception as e:
        outcome["granted"] = f"error: {type(e).__name__}: {e}"
        logger.warning("KB %s was registered but not granted to %s: %s", kb_id, group, e)
    return outcome


# ------------------------------------------------- agent group binding

def bind_agent_group(session, agent_id: str, agent_name: str, group: str,
                     tier: str = "select") -> dict:
    """Bind an agent's service principal to a group, locally.

    This is the row the agent-principal check reads. There was no API
    for it at all — only seed.py wrote these — so every agent created
    through the product failed that half of the check until someone ran
    SQL by hand.
    """
    from sqlmodel import select

    from .models import Role

    existing = session.exec(
        select(Role).where(Role.agent_id == agent_id, Role.group == group, Role.tier == tier)
    ).first()
    if existing:
        return {"status": "exists", "agent_id": agent_id, "group": group, "tier": tier}
    session.add(Role(agent_id=agent_id, agent_name=agent_name, tier=tier, group=group))
    session.commit()
    return {"status": "bound", "agent_id": agent_id, "group": group, "tier": tier}


def bind_agent_to_default_group(session, agent_id: str, agent_name: str) -> Optional[dict]:
    group = agent_default_group()
    if not group:
        return None
    try:
        return bind_agent_group(session, agent_id, agent_name, group)
    except Exception as e:
        # Never fail agent creation over this: the agent is still usable
        # and the binding can be added afterwards.
        logger.warning("could not bind agent %s to %s: %s", agent_id, group, e)
        return {"status": f"error: {type(e).__name__}"}
