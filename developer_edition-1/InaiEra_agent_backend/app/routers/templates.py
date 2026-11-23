from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from pydantic import BaseModel, Field
from sqlalchemy import update as sql_update, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth import get_verified_identity
from ..permissions import TEMPLATES_PLATFORM, principal_has
from ..db import get_session
from ..environment import is_production
from ..models import Agent, Template, TemplateStatus, TemplateVersion
from ..agent_assets import AssetError, embed_agent_assets as _embed_agent_assets


def embed_agent_assets(session, agent_id, flow):
    """Capture an agent's skills/files for a template. A stale reference or a
    file that can't be embedded fails the save (422 with a code naming the node
    and asset) instead of silently dropping assets."""
    try:
        return _embed_agent_assets(session, agent_id, flow)
    except AssetError as e:
        raise HTTPException(e.status, e.detail())
from ..template_packages import (
    CATEGORIES,
    PackageError,
    build_package,
    compute_requirements,
    parse_package,
    sanitize_flow,
    slugify,
)

router = APIRouter(prefix="/api/templates", tags=["templates"])


# ---------------------------------------------------------------------------
# Identity / policy helpers
# ---------------------------------------------------------------------------


def _manages_platform(request: Request) -> bool:
    """Whether the caller may author Organization-managed (platform) templates."""
    return principal_has(request, TEMPLATES_PLATFORM)


def _actor(request: Request, supplied: Optional[str] = None) -> str:
    """Author identity.

    Production never trusts the body for identity. Local/dev keeps a small
    convenience fallback because the reference UI can run without the north-
    south gateway in front of it.
    """
    verified = get_verified_identity(request)
    if verified:
        return verified
    if is_production():
        raise HTTPException(401, "Saving or publishing templates requires a verified identity.")
    return (supplied or "you").strip() or "you"


def _require_platform_author(request: Request) -> str:
    actor = get_verified_identity(request)
    if actor and _manages_platform(request):
        return actor
    raise HTTPException(403, f"Saving or publishing Organization-managed templates requires the "
                             f"permission {TEMPLATES_PLATFORM}.")


def _require_mutable(t: Template, request: Request, action: str) -> str:
    if t.release_managed:
        raise HTTPException(
            403,
            f"'{t.name}' is release-managed and read-only in Studio. Duplicate it to create an editable template.",
        )
    if t.source == "platform":
        return _require_platform_author(request)

    actor = _actor(request)
    # Private means organization-private, not globally published. The author
    # owns mutation rights; a holder of agent:templates:manage-platform can
    # manage it if needed.
    if is_production() and t.owner and actor != t.owner and not _manages_platform(request):
        raise HTTPException(403, f"Only the owner of '{t.name}' can {action} it.")
    return actor


# ---------------------------------------------------------------------------
# DTOs and helpers
# ---------------------------------------------------------------------------


class TemplateIssue(BaseModel):
    severity: str
    message: str
    node_id: Optional[str] = None


class TemplateRead(BaseModel):
    id: str
    key: str
    name: str
    type: str
    app_type: Optional[str] = None
    desc: str
    first_party: bool
    flow: Dict[str, Any]
    source: str
    # Human-facing origin: release-managed Platform packages are distinct from
    # organization-managed templates authored locally in Studio.
    origin: str
    status: str
    version: str
    category: Optional[str] = None
    vertical: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    requirements: Dict[str, Any] = Field(default_factory=dict)
    owner: Optional[str] = None
    release_managed: bool = False
    hidden: bool = False
    draft_revision: int = 1
    published_revision: Optional[int] = None
    current_version_id: Optional[str] = None
    published_at: Optional[datetime] = None
    published_by: Optional[str] = None
    has_unpublished_changes: bool = False
    created_at: datetime
    updated_at: datetime


class TemplateVersionRead(BaseModel):
    id: str
    template_id: str
    version: str
    name: str
    type: str
    app_type: Optional[str] = None
    desc: str
    first_party: bool
    flow: Dict[str, Any]
    category: Optional[str] = None
    vertical: Optional[str] = None
    tags: List[str]
    requirements: Dict[str, Any]
    published_at: datetime
    published_by: Optional[str] = None


class SaveTemplateResult(BaseModel):
    template: TemplateRead
    redactions: List[str]
    issues: List[TemplateIssue]


class PublishTemplateResult(BaseModel):
    template: TemplateRead
    version: TemplateVersionRead
    issues: List[TemplateIssue]


def _get_template_or_404(session: Session, template_id: str) -> Template:
    """Template API identity is the stable id. Display names are mutable and
    are never used as resource identifiers."""
    t = session.get(Template, template_id)
    if not t:
        raise HTTPException(404, f"Template '{template_id}' not found")
    return t


def _current_version(session: Session, t: Template) -> Optional[TemplateVersion]:
    return session.get(TemplateVersion, t.current_version_id) if t.current_version_id else None


def _template_read(t: Template, version: Optional[TemplateVersion] = None) -> TemplateRead:
    """Management view uses the mutable draft; gallery view passes a published
    version so unpublished edits cannot leak into the installable surface."""
    src = version or t
    return TemplateRead(
        id=t.id,
        key=t.key,
        name=src.name,
        type=src.type,
        app_type=getattr(src, "app_type", None),
        desc=src.desc,
        first_party=bool(src.first_party),
        flow=copy.deepcopy(src.flow or {"nodes": [], "edges": []}),
        source=t.source,
        origin=("release" if t.release_managed else "organization" if t.source == "platform" else "private"),
        status=t.status,
        version=(version.version if version else t.version),
        category=src.category,
        vertical=src.vertical,
        tags=list(src.tags or []),
        requirements=copy.deepcopy(src.requirements or {}),
        owner=t.owner,
        release_managed=t.release_managed,
        hidden=t.hidden,
        draft_revision=t.draft_revision,
        published_revision=t.published_revision,
        current_version_id=t.current_version_id,
        published_at=t.published_at,
        published_by=t.published_by,
        has_unpublished_changes=(
            t.current_version_id is not None and t.published_revision != t.draft_revision
        ),
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _version_read(v: TemplateVersion) -> TemplateVersionRead:
    return TemplateVersionRead.model_validate(v, from_attributes=True)


def _name_taken(session: Session, name: str, *, except_id: Optional[str] = None) -> bool:
    """Exact, case-insensitive active-name match.

    `ILIKE(name)` is wrong here because `_` and `%` in a user's literal name
    become SQL wildcards. Stable ids are the resource identity; this helper is
    only the display-name uniqueness rule for non-archived templates.
    """
    q = select(Template).where(
        func.lower(Template.name) == name.lower(),
        Template.status != TemplateStatus.ARCHIVED.value,
    )
    if except_id is not None:
        q = q.where(Template.id != except_id)
    return session.exec(q).first() is not None


def _unique_name(session: Session, base: str) -> str:
    if not _name_taken(session, base):
        return base
    n = 2
    while _name_taken(session, f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def _check_category(category: Optional[str]) -> None:
    if category is not None and category not in CATEGORIES:
        raise HTTPException(422, f"category must be one of: {', '.join(CATEGORIES)}")


def _flow_issues(flow: dict, app_type: Optional[str], *, strict: bool = False) -> List[TemplateIssue]:
    from ..flow_validator import validate_flow

    try:
        res = validate_flow(flow.get("nodes", []), flow.get("edges", []), app_type)
    except Exception as exc:
        if strict:
            raise HTTPException(422, f"Template flow could not be validated: {exc}") from exc
        return [TemplateIssue(severity="error", message=f"Flow validation failed: {exc}")]
    return [TemplateIssue(severity=i.severity, message=i.message, node_id=i.node_id) for i in res.issues]


def _assert_publishable(t: Template) -> List[TemplateIssue]:
    if not (t.flow or {}).get("nodes"):
        raise HTTPException(422, "A template needs at least one node before it can be published.")
    issues = _flow_issues(t.flow or {}, t.app_type, strict=True)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise HTTPException(
            422,
            {
                "message": "Template is saved as a draft but cannot be published until validation errors are fixed.",
                "issues": [i.model_dump() for i in errors],
            },
        )
    return issues


def _next_version(current: str) -> str:
    if not current or current == "0.0.0":
        return "1.0.0"
    try:
        major, minor, patch = (int(x) for x in current.split("."))
    except Exception as exc:
        raise HTTPException(409, f"Template has invalid version {current!r}; expected semantic version x.y.z") from exc
    return f"{major}.{minor}.{patch + 1}"


def _parse_if_match(raw: str) -> int:
    """Accept normal HTTP entity-tag spelling as well as the legacy bare int.

    Browsers/proxies commonly send `If-Match: "7"`; FastAPI coercing the
    header directly to int rejected that perfectly ordinary form. Weak tags
    are accepted too because this revision number is an optimistic-lock token,
    not a byte-for-byte representation validator.
    """
    value = (raw or "").strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"If-Match must be an integer revision such as 7 or \"7\"; got {raw!r}") from exc
    if revision < 1:
        raise HTTPException(422, "If-Match revision must be at least 1")
    return revision


def _published_gallery_rows(
    session: Session,
    *,
    source: Optional[str] = None,
    category: Optional[str] = None,
    vertical: Optional[str] = None,
) -> List[TemplateRead]:
    """Shared gallery query used by both the list and facets endpoints.

    Keeping this as ordinary Python parameters avoids accidentally passing
    FastAPI `Query(...)` descriptor objects when one route calls another
    directly.
    """
    rows = session.exec(
        select(Template).where(
            Template.hidden == False,  # noqa: E712
            Template.status == TemplateStatus.PUBLISHED.value,
            Template.current_version_id.is_not(None),
        )
    ).all()
    out: List[TemplateRead] = []
    for t in rows:
        if source and t.source != source:
            continue
        v = _current_version(session, t)
        if not v:
            continue
        if category and v.category != category:
            continue
        if vertical and v.vertical != vertical:
            continue
        out.append(_template_read(t, v))
    return sorted(out, key=lambda x: (x.source != "platform", x.category or "~", x.name.lower()))


def _new_template(
    session: Session,
    *,
    name: str,
    desc: str,
    ttype: str,
    app_type: Optional[str],
    flow: dict,
    category: Optional[str],
    vertical: Optional[str],
    tags: List[str],
    owner: str,
    source: str,
) -> Template:
    prefix = "platform-custom" if source == "platform" else "private"
    key = f"{prefix}-{slugify(name)[:42]}-{uuid4().hex[:8]}"
    t = Template(
        id=f"tpl-{uuid4().hex[:20]}",
        key=key,
        name=name,
        type=ttype,
        app_type=app_type,
        desc=desc,
        first_party=(source == "platform"),
        flow=flow,
        source=source,
        status=TemplateStatus.DRAFT.value,
        version="0.0.0",
        category=category or "General",
        vertical=vertical or None,
        tags=tags,
        requirements=compute_requirements(flow),
        owner=owner,
        release_managed=False,
        hidden=False,
        draft_revision=1,
    )
    session.add(t)
    return t


def _published_package_subject(t: Template, v: TemplateVersion) -> SimpleNamespace:
    return SimpleNamespace(
        key=t.key,
        name=v.name,
        version=v.version,
        type=v.type,
        app_type=v.app_type,
        category=v.category,
        vertical=v.vertical,
        tags=v.tags,
        desc=v.desc,
        first_party=v.first_party,
        flow=v.flow,
    )


# ---------------------------------------------------------------------------
# Browse and management
# ---------------------------------------------------------------------------


@router.get("", dependencies=[Depends(require_any_permission(P.TEMPLATES_VIEW, P.STUDIO))], response_model=List[TemplateRead])
def list_templates(
    source: Optional[str] = Query(None, pattern="^(platform|private)$"),
    category: Optional[str] = None,
    vertical: Optional[str] = None,
    session: Session = Depends(get_session),
) -> List[TemplateRead]:
    """Published gallery only. Draft edits never alter this result until the
    author explicitly publishes them."""
    return _published_gallery_rows(
        session, source=source, category=category, vertical=vertical
    )


@router.get("/manage", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=List[TemplateRead])
def list_managed_templates(
    source: Optional[str] = Query(None, pattern="^(platform|private)$"),
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> List[TemplateRead]:
    q = select(Template)
    if source:
        q = q.where(Template.source == source)
    if not include_archived:
        q = q.where(Template.status != TemplateStatus.ARCHIVED.value)
    rows = session.exec(q).all()
    return sorted((_template_read(t) for t in rows), key=lambda x: (x.source != "platform", x.name.lower()))


@router.get("/facets", dependencies=[Depends(require_any_permission(P.TEMPLATES_VIEW, P.STUDIO))])
def template_facets(session: Session = Depends(get_session)) -> dict:
    rows = _published_gallery_rows(session)
    cats: Dict[str, int] = {}
    verts: Dict[str, int] = {}
    for t in rows:
        cats[t.category or "General"] = cats.get(t.category or "General", 0) + 1
        if t.vertical:
            verts[t.vertical] = verts.get(t.vertical, 0) + 1
    return {
        "categories": [{"name": c, "count": cats[c]} for c in CATEGORIES if c in cats],
        "verticals": [{"name": v, "count": n} for v, n in sorted(verts.items())],
        "sources": {
            "platform": sum(1 for t in rows if t.source == "platform"),
            "private": sum(1 for t in rows if t.source == "private"),
        },
        "origins": {
            "release": sum(1 for t in rows if t.origin == "release"),
            "organization": sum(1 for t in rows if t.origin == "organization"),
            "private": sum(1 for t in rows if t.origin == "private"),
        },
    }


@router.get("/{template_ref}/versions", dependencies=[Depends(require_any_permission(P.TEMPLATES_VIEW, P.STUDIO))], response_model=List[TemplateVersionRead])
def template_versions(template_ref: str, session: Session = Depends(get_session)) -> List[TemplateVersionRead]:
    t = _get_template_or_404(session, template_ref)
    rows = session.exec(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == t.id)
        .order_by(TemplateVersion.published_at.desc())
    ).all()
    return [_version_read(v) for v in rows]


# ---------------------------------------------------------------------------
# Save/edit/publish/archive/duplicate/export/import
# ---------------------------------------------------------------------------


class SaveTemplateIn(BaseModel):
    agent_id: str
    name: str = Field(min_length=1, max_length=120)
    source: str = Field(default="private", pattern="^(platform|private)$")
    desc: str = Field(default="", max_length=1000)
    category: Optional[str] = None
    vertical: Optional[str] = Field(default=None, max_length=60)
    tags: List[str] = Field(default_factory=list, max_length=20)
    owner: Optional[str] = None


@router.post("", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=SaveTemplateResult)
def save_as_template(
    body: SaveTemplateIn,
    request: Request,
    session: Session = Depends(get_session),
) -> SaveTemplateResult:
    """Capture an Agent as a *draft* template. Platform and Private templates
    now share the same explicit draft -> publish lifecycle."""
    _check_category(body.category)
    name = body.name.strip()
    if _name_taken(session, name):
        raise HTTPException(409, f"A template named '{name}' already exists. Choose a different name.")
    agent = session.get(Agent, body.agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    actor = _require_platform_author(request) if body.source == "platform" else _actor(request, body.owner)
    flow, redactions = sanitize_flow(embed_agent_assets(session, agent.id, agent.flow or {"nodes": [], "edges": []}))
    if not flow.get("nodes"):
        raise HTTPException(422, "This agent has no nodes to save as a template.")
    t = _new_template(
        session,
        name=name,
        desc=body.desc.strip(),
        ttype=agent.type or "App",
        app_type=agent.app_type,
        flow=flow,
        category=body.category,
        vertical=(body.vertical or "").strip() or None,
        tags=[x.strip() for x in body.tags if x.strip()],
        owner=actor,
        source=body.source,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # Covers the real concurrent-create race that can occur after the
        # friendly pre-check but before INSERT reaches the unique index.
        raise HTTPException(409, f"A template named '{name}' already exists. Choose a different name.") from exc
    session.refresh(t)
    return SaveTemplateResult(
        template=_template_read(t), redactions=redactions, issues=_flow_issues(flow, agent.app_type)
    )


class UpdateTemplateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    desc: Optional[str] = Field(default=None, max_length=1000)
    category: Optional[str] = None
    vertical: Optional[str] = Field(default=None, max_length=60)
    tags: Optional[List[str]] = Field(default=None, max_length=20)
    refresh_from_agent_id: Optional[str] = None


@router.put("/{template_ref}", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=SaveTemplateResult)
def update_template(
    template_ref: str,
    body: UpdateTemplateIn,
    request: Request,
    if_match: str = Header(..., alias="If-Match"),
    session: Session = Depends(get_session),
) -> SaveTemplateResult:
    expected_revision = _parse_if_match(if_match)
    t = _get_template_or_404(session, template_ref)
    _require_mutable(t, request, "edit")
    if t.status == TemplateStatus.ARCHIVED.value:
        raise HTTPException(409, "Archived templates cannot be edited.")
    _check_category(body.category)

    if t.draft_revision != expected_revision:
        raise HTTPException(
            409,
            f"Template changed while you were editing it. Expected revision {expected_revision}; "
            f"current revision is {t.draft_revision}. Reload and retry.",
        )

    candidate: Dict[str, Any] = {}
    redactions: List[str] = []
    if body.name is not None:
        name = body.name.strip()
        if name != t.name and _name_taken(session, name, except_id=t.id):
            raise HTTPException(409, f"A template named '{name}' already exists.")
        candidate["name"] = name
    if body.desc is not None:
        candidate["desc"] = body.desc.strip()
    if body.category is not None:
        candidate["category"] = body.category
    if body.vertical is not None:
        candidate["vertical"] = body.vertical.strip() or None
    if body.tags is not None:
        candidate["tags"] = [x.strip() for x in body.tags if x.strip()]
    if body.refresh_from_agent_id:
        agent = session.get(Agent, body.refresh_from_agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        flow, redactions = sanitize_flow(embed_agent_assets(session, agent.id, agent.flow or {}))
        if not flow.get("nodes"):
            raise HTTPException(422, "That agent has no nodes to capture.")
        candidate.update(
            flow=flow,
            requirements=compute_requirements(flow),
            type=agent.type or "App",
            app_type=agent.app_type,
        )

    # Saving an identical draft is a no-op. This makes Save & publish
    # idempotent for unchanged content instead of manufacturing a patch
    # version just because the Save button was clicked twice.
    changed = {k: v for k, v in candidate.items() if getattr(t, k) != v}
    if not changed:
        return SaveTemplateResult(
            template=_template_read(t),
            redactions=redactions,
            issues=_flow_issues(t.flow, t.app_type),
        )

    updates: Dict[str, Any] = {
        **changed,
        "updated_at": datetime.utcnow(),
        "draft_revision": expected_revision + 1,
    }
    result = session.exec(
        sql_update(Template)
        .where(Template.id == t.id, Template.draft_revision == expected_revision)
        .values(**updates)
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(Template, t.id)
        raise HTTPException(
            409,
            f"Template changed while you were editing it. Expected revision {expected_revision}; "
            f"current revision is {current.draft_revision if current else 'unknown'}. Reload and retry.",
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "A template with that active name already exists.") from exc
    updated = session.get(Template, t.id)
    return SaveTemplateResult(
        template=_template_read(updated),
        redactions=redactions,
        issues=_flow_issues(updated.flow, updated.app_type),
    )


@router.post("/{template_ref}/publish", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=PublishTemplateResult)
def publish_template(
    template_ref: str,
    request: Request,
    if_match: str = Header(..., alias="If-Match"),
    session: Session = Depends(get_session),
) -> PublishTemplateResult:
    expected_revision = _parse_if_match(if_match)
    # Row lock makes publication/version increment single-writer and prevents
    # two concurrent publish clicks creating the same semantic version.
    t = session.exec(select(Template).where(Template.id == template_ref).with_for_update()).first()
    if not t:
        # Name fallback is not lock-safe and new clients use ids; retain only a
        # clear 404 rather than silently publishing a different row.
        raise HTTPException(404, f"Template '{template_ref}' not found")
    _require_mutable(t, request, "publish")
    if t.status == TemplateStatus.ARCHIVED.value or t.hidden:
        raise HTTPException(409, "Archived/hidden templates cannot be published.")
    if t.draft_revision != expected_revision:
        raise HTTPException(
            409,
            f"Template changed while you were publishing it. Expected revision {expected_revision}; current revision is {t.draft_revision}.",
        )
    issues = _assert_publishable(t)

    # Publishing an unchanged revision is idempotent.
    if t.current_version_id and t.published_revision == t.draft_revision:
        current = _current_version(session, t)
        if not current:
            raise HTTPException(409, "Template publication metadata is inconsistent: current version is missing.")
        return PublishTemplateResult(template=_template_read(t), version=_version_read(current), issues=issues)

    actor = _require_platform_author(request) if t.source == "platform" else _actor(request)
    version = _next_version(t.version)
    now = datetime.utcnow()
    snap = TemplateVersion(
        id=f"tplv-{uuid4().hex[:24]}",
        template_id=t.id,
        version=version,
        name=t.name,
        type=t.type,
        app_type=t.app_type,
        desc=t.desc,
        first_party=t.first_party,
        flow=copy.deepcopy(t.flow),
        category=t.category,
        vertical=t.vertical,
        tags=list(t.tags or []),
        requirements=compute_requirements(t.flow or {}),
        published_at=now,
        published_by=actor,
    )
    session.add(snap)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "That template version already exists. Reload and publish again.") from exc

    t.status = TemplateStatus.PUBLISHED.value
    t.version = version
    t.current_version_id = snap.id
    t.published_revision = t.draft_revision
    t.published_at = now
    t.published_by = actor
    t.requirements = copy.deepcopy(snap.requirements)
    t.updated_at = now
    session.add(t)
    session.commit()
    session.refresh(t)
    return PublishTemplateResult(template=_template_read(t), version=_version_read(snap), issues=issues)


@router.delete("/{template_ref}", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))])
def archive_template(
    template_ref: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    t = _get_template_or_404(session, template_ref)
    _require_mutable(t, request, "archive")
    t.status = TemplateStatus.ARCHIVED.value
    t.hidden = True
    t.updated_at = datetime.utcnow()
    session.add(t)
    session.commit()
    return {"deleted": t.id, "archived": True}


class DuplicateTemplateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    owner: Optional[str] = None
    state: str = Field(default="published", pattern="^(published|draft)$")


@router.post("/{template_ref}/duplicate", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=TemplateRead)
def duplicate_template(
    template_ref: str,
    request: Request,
    body: Optional[DuplicateTemplateIn] = None,
    session: Session = Depends(get_session),
) -> TemplateRead:
    body = body or DuplicateTemplateIn()
    src = _get_template_or_404(session, template_ref)
    snap = _current_version(session, src) if body.state == "published" else None
    if body.state == "published" and not snap:
        raise HTTPException(409, "This template has not been published yet; duplicate its draft instead.")
    source_obj = snap or src
    name = body.name.strip() if body.name and body.name.strip() else _unique_name(session, f"{source_obj.name} (copy)")
    if _name_taken(session, name):
        raise HTTPException(409, f"A template named '{name}' already exists.")
    t = _new_template(
        session,
        name=name,
        desc=source_obj.desc,
        ttype=source_obj.type,
        app_type=getattr(source_obj, "app_type", None),
        flow=copy.deepcopy(source_obj.flow),
        category=source_obj.category,
        vertical=source_obj.vertical,
        tags=list(source_obj.tags or []),
        owner=_actor(request, body.owner),
        source="private",
    )
    session.commit()
    session.refresh(t)
    return _template_read(t)


@router.get("/{template_ref}/export", dependencies=[Depends(require_any_permission(P.TEMPLATES_VIEW, P.STUDIO))])
def export_template(
    template_ref: str,
    state: str = Query("published", pattern="^(published|draft)$"),
    session: Session = Depends(get_session),
) -> dict:
    t = _get_template_or_404(session, template_ref)
    if state == "published":
        v = _current_version(session, t)
        if not v:
            raise HTTPException(409, "This template has no published version. Export the draft instead.")
        return build_package(_published_package_subject(t, v))
    return build_package(t)


class ImportPackageIn(BaseModel):
    package: Dict[str, Any]
    name: Optional[str] = Field(default=None, max_length=120)
    owner: Optional[str] = None


@router.post("/import", dependencies=[Depends(require_any_permission(P.STUDIO, P.TEMPLATES_PLATFORM))], response_model=SaveTemplateResult)
def import_package(
    body: ImportPackageIn,
    request: Request,
    session: Session = Depends(get_session),
) -> SaveTemplateResult:
    try:
        p = parse_package(body.package, source_label="package")
    except PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    name = (body.name or p["name"]).strip()
    if _name_taken(session, name):
        raise HTTPException(409, f"A template named '{name}' already exists. Pass a different name to import it.")
    flow, redactions = sanitize_flow(p["flow"])
    t = _new_template(
        session,
        name=name,
        desc=p["desc"],
        ttype=p["type"],
        app_type=p.get("app_type"),
        flow=flow,
        category=p["category"],
        vertical=p["vertical"],
        tags=p["tags"],
        owner=_actor(request, body.owner),
        source="private",
    )
    session.commit()
    session.refresh(t)
    return SaveTemplateResult(
        template=_template_read(t), redactions=redactions, issues=_flow_issues(flow, t.app_type)
    )


# ---------------------------------------------------------------------------
# Install / use
# ---------------------------------------------------------------------------


class InstantiateIn(BaseModel):
    name: Optional[str] = None
    owner: str = "you"
    invoking_user: str = "user:demo"
    publish: bool = False


class InstantiateResult(BaseModel):
    agent: Agent
    published: bool
    publish_error: Optional[str] = None


@router.post("/{template_ref}/instantiate", dependencies=[Depends(require_permission(P.STUDIO))], response_model=InstantiateResult)
def instantiate_template(
    template_ref: str,
    body: InstantiateIn,
    session: Session = Depends(get_session),
) -> InstantiateResult:
    t = _get_template_or_404(session, template_ref)
    if t.hidden or t.status != TemplateStatus.PUBLISHED.value:
        raise HTTPException(404, f"Template '{template_ref}' is not published")
    v = _current_version(session, t)
    if not v:
        raise HTTPException(409, "Template says it is published but its immutable version is missing.")

    from .agents import _resolve_and_publish

    new_id = f"a{uuid4().hex[:10]}"
    agent = Agent(
        id=new_id,
        name=body.name or v.name,
        type=v.type,
        subtype=v.name,
        plane=v.type,
        owner=body.owner,
        status="draft",
        version="v1",
        health_status="—",
        app_type=v.app_type,
        flow=copy.deepcopy(v.flow),
        metrics={},
        template_name=v.name,
        template_id=t.id,
        template_version_id=v.id,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    # Preserve the existing product behavior for environments that still use
    # the default agent-group binding; template publishing itself is unrelated.
    try:
        from ..ucs_provisioning import bind_agent_to_default_group
        bind_agent_to_default_group(session, agent.id, agent.name)
        session.refresh(agent)
    except Exception:
        # Do not make template instantiation depend on optional UCS wiring.
        pass

    if not body.publish:
        return InstantiateResult(agent=agent, published=False)

    try:
        _resolve_and_publish(agent, body.invoking_user, session)
        session.refresh(agent)
        return InstantiateResult(agent=agent, published=True)
    except HTTPException as exc:
        return InstantiateResult(agent=agent, published=False, publish_error=str(exc.detail))
    except Exception as exc:
        # Existing one-click behavior reports publish failure but leaves the
        # newly created Agent as a draft the user can repair in Builder.
        return InstantiateResult(agent=agent, published=False, publish_error=str(exc))
