"""
Real flow-compilation helpers (§0.84, GAP_REPORT) - genuinely shared,
runtime-independent logic for deriving governance-relevant facts from
a flow's own `nodes` list. Moved out of harness.py on the report's own
"ideally" suggestion: what catalog objects a flow touches is a real
property of the flow's own configuration, not of any one particular
execution path - it shouldn't have lived inside the sync-execution
runtime module in the first place, and every real caller (publish,
sync execution, durable execution, Sub-Agent resolution, eval) should
depend on this one, small, dependency-free module rather than on
harness.py's own, much larger runtime surface just to reach one
function.

The real, reported bug this fix closes: objects_to_check already
existed (harness.py) and was already correctly multi-KB aware, but two
real, independent call sites - the publish endpoint (routers/agents.py)
and the durable-path executor (routers/execution.py's own
_run_durable) - never used it, instead each keeping their own
duplicate, older, single-KB-only computation
(catalog_object or knowledge_base). A Retrieval node configured with
knowledge_bases: ["kb.finance", "kb.legal", "kb.hr"] could publish, or
run on the durable path, with an entitlement plan checking NONE of
those three real objects - a real governance hole, not a display bug.
"""
from typing import List


def retrieval_kb_refs(cfg: dict) -> List[str]:
    """A retrieval node selects zero or more knowledge bases (the
    mockup's multi-select "Select Knowledge to Search"), replacing the
    old single knowledge_base string. Both shapes are accepted so an
    older-saved node config keeps working unchanged."""
    kbs = cfg.get("knowledge_bases")
    if kbs:
        return list(kbs)
    single = cfg.get("knowledge_base")
    return [single] if single else []


def objects_to_check(nodes: List[dict]) -> List[str]:
    """Every catalog object a flow's tool/retrieval nodes touch - the
    one real, shared source of truth every real governance-relevant
    call site (publish, sync execution, durable execution, Sub-Agent
    resolution, eval) now depends on, so none of them can silently
    diverge on what gets entitlement-checked. Real multi-KB aware: a
    retrieval node lists zero or more knowledge bases (see
    retrieval_kb_refs above), not just one."""
    from .tool_connectors import needs_entitlement

    objs: List[str] = []
    for n in nodes:
        cfg = n.get("config") or {}
        if (n["type"] == "tool" and cfg.get("catalog_object")
                and needs_entitlement(cfg["catalog_object"])):
            objs.append(cfg["catalog_object"])
        elif n["type"] == "retrieval":
            objs.extend(retrieval_kb_refs(cfg))
    return objs


def kb_slug(ref: str) -> str:
    """The slugification KnowledgeBase creation applies to a new KB's
    name to derive its id (routers/knowledge_bases.py). Kept here so
    the retrieval runtime and the governance path share ONE resolver
    rather than each carrying their own copy — harness.py's own
    _kb_lookup_id was that second copy, and it is what let a display
    name work for retrieval while silently failing every entitlement
    check."""
    return ref.lower().replace(" ", "_").replace(".", "_")


def resolve_kb(session, ref: str):
    """The registered KnowledgeBase a retrieval node's reference points
    at, or None if the reference isn't one of this platform's KBs.

    Three ways, because saved configs genuinely contain all three: the
    id itself, the display name the Builder used to store (see
    NodeConfigPanel's own multi-select), and the slug of that name —
    which is the same as the id for any KB that has never been renamed,
    and is the only form harness.py used to try. Matching the name
    directly is what makes a RENAMED KB keep working: after a rename,
    slug(new name) no longer equals the id, so the slug-only lookup
    silently fell through to "no embedding pipeline configured" and
    retrieval returned a placeholder chunk instead of an error."""
    # Function-scoped import on purpose: this module is depended on by
    # publish, both execution paths, Sub-Agent resolution and eval
    # precisely because it is small and import-light, and models.py
    # pulls in the whole SQLModel table surface. Nothing calls this
    # without a live session anyway.
    from sqlmodel import select
    from .models import KnowledgeBase

    if not ref:
        return None
    kb = session.get(KnowledgeBase, ref)
    if kb:
        return kb
    kb = session.exec(select(KnowledgeBase).where(KnowledgeBase.name == ref)).first()
    if kb:
        return kb
    return session.get(KnowledgeBase, kb_slug(ref))


def kb_catalog_object(session, ref: str) -> str:
    """The catalog object that governs a flow reference.

    A reference that names one of this platform's own knowledge bases
    resolves to that KB's governing FQN; anything else is returned
    UNCHANGED, so a genuine external catalog object (a Tool node's
    `pg_sales.public.orders`, or a retrieval node pointed straight at
    `lakehouse_aws.demo.orders`) is still checked exactly as before.
    That pass-through is what keeps §0.84 intact — the point of this
    function is to make an unmatchable reference checkable, never to
    quietly drop anything out of the entitlement plan."""
    from .ucs_client import kb_fqn

    kb = resolve_kb(session, ref)
    if kb is None:
        return ref
    declared = (kb.config or {}).get("catalog_object")
    return declared or kb_fqn(kb.id)


def governed_objects(session, nodes: List[dict]) -> List[str]:
    """What every entitlement-enforcing call site must check: the same
    objects objects_to_check derives, with each one resolved to the
    catalog identifier UCS can actually decide on.

    Deliberately a thin wrapper over the one shared walk rather than a
    second walk of its own — a duplicate traversal here would be the
    exact divergence §0.84 was written to close."""
    return [kb_catalog_object(session, obj) for obj in objects_to_check(nodes)]
