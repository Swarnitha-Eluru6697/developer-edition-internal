"""Agent asset library (skills, reference files) and node Env secrets."""
from __future__ import annotations

import asyncio
import copy
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlmodel import Session, select

from .. import agent_assets as aa
from ..auth import get_verified_identity
from ..db import get_session
from ..models import Agent, AgentFile, AgentSkill
from ..storage import get_storage_backend
from ..upload_security import MalwareScannerUnavailable, sanitize_filename, scan_for_malware

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["agent-assets"])
CHUNK = 1024 * 1024
_slots = asyncio.Semaphore(aa.UPLOAD_CONCURRENCY)


def _agent(session: Session, agent_id: str) -> Agent:
    a = session.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return a


async def _read_capped(file: UploadFile, limit: int, label: str) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        c = await file.read(CHUNK)
        if not c:
            break
        total += len(c)
        if total > limit:
            raise HTTPException(413, f"{label} is too large. The limit is {limit // (1024 * 1024)} MB.")
        chunks.append(c)
    return b"".join(chunks)


def _scan(data: bytes) -> None:
    try:
        flagged = scan_for_malware(data)
    except MalwareScannerUnavailable as e:
        raise HTTPException(503, f"Upload refused: malware scanning is unavailable ({e}).")
    if flagged:
        raise HTTPException(422, f"Malware scanning flagged this upload: {flagged}")


def _backend_name() -> str:
    return os.getenv("STORAGE_BACKEND", "local")


class _Slot:
    """Bounded concurrent uploads per process: a burst is refused with 429
    rather than buffering many large bodies in memory at once."""
    async def __aenter__(self):
        if _slots.locked():
            raise HTTPException(429, "Too many uploads are in progress. Try again in a moment.")
        await _slots.acquire()

    async def __aexit__(self, *exc):
        _slots.release()


def _refs(session: Session, agent_id: str, field: str, asset_id: str) -> dict:
    draft, versions = aa.asset_references(session, agent_id, field, asset_id)
    return {"draft_nodes": draft, "versions": len(versions)}


def _draft_ids(a: Agent, field: str) -> set:
    out = set()
    for n in (a.flow or {}).get("nodes", []) or []:
        if isinstance(n, dict) and n.get("type") == "agent":
            v = (n.get("config") or {}).get(field)
            if isinstance(v, list):
                out.update(i for i in v if isinstance(i, str))
    return out


def _visible(model, a: Agent, field: str):
    """Live assets, plus retired ones the current draft still points at (for
    example after an older version was restored), so the Configure panel can
    always show what a node uses."""
    ids = sorted(_draft_ids(a, field))
    cond = model.retired_at.is_(None)
    if ids:
        cond = or_(cond, model.id.in_(ids))
    return select(model).where(model.agent_id == a.id, cond)


def _skill_dict(s: AgentSkill, session: Session = None) -> dict:
    d = {"id": s.id, "name": s.name, "description": s.description, "size_bytes": s.size_bytes,
         "file_count": s.file_count, "created_at": s.created_at, "retired": s.retired_at is not None}
    if session is not None:
        d["used_by"] = _refs(session, s.agent_id, "skills", s.id)
    return d


def _file_dict(f: AgentFile, session: Session = None) -> dict:
    d = {"id": f.id, "filename": f.filename, "mime": f.mime, "kind": f.kind, "size_bytes": f.size_bytes,
         "status": f.status, "status_detail": f.status_detail, "has_text": bool(f.extracted_text),
         "created_at": f.created_at, "retired": f.retired_at is not None,
         "template_policy": f.template_policy or "require"}
    if session is not None:
        d["used_by"] = _refs(session, f.agent_id, "files", f.id)
    return d


# ── skills ──────────────────────────────────────────────────────────────────

@router.get("/skills", dependencies=[Depends(require_permission(P.STUDIO))])
def list_skills(agent_id: str, session: Session = Depends(get_session)) -> list:
    a = _agent(session, agent_id)
    rows = session.exec(_visible(AgentSkill, a, "skills").order_by(AgentSkill.created_at)).all()
    return [_skill_dict(r, session) for r in rows]


@router.post("/skills", dependencies=[Depends(require_permission(P.STUDIO))], status_code=201)
async def upload_skill(agent_id: str, request: Request, file: UploadFile = File(...),
                       session: Session = Depends(get_session)) -> dict:
    _agent(session, agent_id)
    fname = sanitize_filename(file.filename or "skill.zip")
    if not fname.lower().endswith((".zip", ".skill")):
        raise HTTPException(415, "A skill package must be a .zip or .skill file.")
    async with _Slot():
        data = await _read_capped(file, aa.MAX_SKILL_BYTES, "This skill package")
        try:
            name, desc, body, count = aa.parse_skill_package(data)
            aa.check_quota(session, agent_id, "skill", len(data))
        except aa.AssetError as e:
            raise HTTPException(e.status, e.detail())
        if session.exec(select(AgentSkill).where(AgentSkill.agent_id == agent_id, AgentSkill.name == name,
                                                 AgentSkill.retired_at.is_(None))).first():
            raise HTTPException(409, f"A skill named '{name}' is already in this agent's library.")
        _scan(data)
        sid = f"sk-{uuid.uuid4().hex[:16]}"
        key = f"agents/{agent_id}/skills/{sid}"
        backend = get_storage_backend()
        backend.put(key, data, "application/zip")
        row = AgentSkill(id=sid, agent_id=agent_id, name=name, description=desc, size_bytes=len(data), file_count=count,
                         content=body, sha256=aa.sha256_hex(data), object_key=key, storage_backend=_backend_name(),
                         created_by=get_verified_identity(request))
        try:
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            aa.delete_blobs([(_backend_name(), key)])  # no orphan blob if the row can't be saved
            raise
        session.refresh(row)
        return _skill_dict(row, session)


@router.delete("/skills/{skill_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_skill(agent_id: str, skill_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(AgentSkill, skill_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Skill not found")
    try:
        outcome = aa.remove_asset(session, row, "skills")
    except aa.AssetError as e:
        raise HTTPException(e.status, e.detail())
    return {"ok": True, "outcome": outcome}


# ── files ───────────────────────────────────────────────────────────────────

@router.get("/files", dependencies=[Depends(require_permission(P.STUDIO))])
def list_files(agent_id: str, session: Session = Depends(get_session)) -> list:
    a = _agent(session, agent_id)
    rows = session.exec(_visible(AgentFile, a, "files").order_by(AgentFile.created_at)).all()
    return [_file_dict(r, session) for r in rows]


@router.get("/files/limits", dependencies=[Depends(require_permission(P.STUDIO))])
def file_limits(agent_id: str, session: Session = Depends(get_session)) -> dict:
    return {"limits_bytes": aa.FILE_LIMITS, "skill_max_bytes": aa.MAX_SKILL_BYTES,
            "max_skills": aa.QUOTA_MAX_SKILLS, "max_files": aa.QUOTA_MAX_FILES, "max_library_bytes": aa.QUOTA_MAX_BYTES,
            "max_historical_bytes": aa.QUOTA_MAX_HISTORICAL_BYTES, "usage": aa.storage_usage(session, agent_id)}


@router.post("/files", dependencies=[Depends(require_permission(P.STUDIO))], status_code=201)
async def upload_agent_file(agent_id: str, request: Request, file: UploadFile = File(...),
                            session: Session = Depends(get_session)) -> dict:
    _agent(session, agent_id)
    fname = sanitize_filename(file.filename or "file")
    import os as _os
    ext = _os.path.splitext(fname.lower())[1]
    if ext in aa.UNSUPPORTED_DOC_EXT:
        raise HTTPException(415, f"{ext} files can't be read by the agent yet. Save it as PDF, DOCX, TXT, CSV or Markdown.")
    kind = aa.classify_file(fname)
    if not kind:
        raise HTTPException(415, "This file type is not supported. Use a document, image, audio or video file.")
    async with _Slot():
        data = await _read_capped(file, aa.FILE_LIMITS[kind], f"This {kind}")
        if not data:
            raise HTTPException(422, "This file is empty.")
        problem = aa.verify_content(fname, data)
        if problem:
            raise HTTPException(415, f"{fname}: {problem}.")
        try:
            aa.check_quota(session, agent_id, "file", len(data))
        except aa.AssetError as e:
            raise HTTPException(e.status, e.detail())
        _scan(data)
        if kind == "document":
            # PDF/DOCX parsing happens in a separate, resource-limited process.
            status, detail, text = await asyncio.get_running_loop().run_in_executor(
                None, aa.extract_text_isolated, fname, data)
        else:
            status, detail, text = "stored_only", "Stored, but images, audio and video are not sent to the model yet.", ""
        fid = f"af-{uuid.uuid4().hex[:16]}"
        key = f"agents/{agent_id}/files/{fid}"
        mime = file.content_type or "application/octet-stream"
        get_storage_backend().put(key, data, mime)
        row = AgentFile(id=fid, agent_id=agent_id, filename=fname, mime=mime, kind=kind, size_bytes=len(data),
                        status=status, status_detail=detail, extracted_text=text, sha256=aa.sha256_hex(data),
                        object_key=key, storage_backend=_backend_name(), created_by=get_verified_identity(request))
        try:
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            aa.delete_blobs([(_backend_name(), key)])
            raise
        session.refresh(row)
        return _file_dict(row, session)


@router.delete("/files/{file_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_agent_file(agent_id: str, file_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(AgentFile, file_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "File not found")
    try:
        outcome = aa.remove_asset(session, row, "files")
    except aa.AssetError as e:
        raise HTTPException(e.status, e.detail())
    return {"ok": True, "outcome": outcome}


@router.post("/assets/reconcile", dependencies=[Depends(require_permission(P.STUDIO))])
def reconcile_assets(agent_id: str, older_than_hours: float = aa.ORPHAN_GRACE_HOURS,
                     session: Session = Depends(get_session)) -> dict:
    """Remove library assets that no node and no version uses (older than the
    grace period), retired assets nothing pins any more, and unreferenced node
    secrets. The grace period can't be set below an hour, so an upload that is
    about to be attached is never collected. The same cleanup runs on a schedule."""
    _agent(session, agent_id)
    if older_than_hours < aa.MIN_RECONCILE_GRACE_HOURS:
        raise HTTPException(422, f"older_than_hours must be at least {aa.MIN_RECONCILE_GRACE_HOURS:g}.")
    return aa.reconcile_agent_assets(session, agent_id, older_than_hours)


class FilePolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_policy: str


@router.patch("/files/{file_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def set_file_template_policy(agent_id: str, file_id: str, body: FilePolicyIn, session: Session = Depends(get_session)) -> dict:
    """What "Save as template" does with this file: require | embed | exclude."""
    row = session.get(AgentFile, file_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "File not found")
    if body.template_policy not in aa.TEMPLATE_POLICIES:
        raise HTTPException(422, f"template_policy must be one of: {', '.join(aa.TEMPLATE_POLICIES)}.")
    row.template_policy = body.template_policy
    session.add(row)
    session.commit()
    session.refresh(row)
    return _file_dict(row, session)


# ── node Env secrets ────────────────────────────────────────────────────────

class EnvEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: Optional[str] = None  # omit to keep the stored value


class EnvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: List[EnvEntryIn]


def _agent_node(flow: dict, node_id: str) -> dict:
    node = next((n for n in (flow or {}).get("nodes", []) or []
                 if isinstance(n, dict) and n.get("id") == node_id and n.get("type") == "agent"), None)
    if node is None:
        raise HTTPException(404, "Agent node not found")
    return node


@router.get("/nodes/{node_id}/secrets", dependencies=[Depends(require_permission(P.STUDIO))])
def list_node_secrets(agent_id: str, node_id: str, session: Session = Depends(get_session)) -> list:
    """Key names only. Values are never returned."""
    _agent(session, agent_id)
    return [{"key": k, "configured": True} for k in sorted(aa.node_secret_keys(session, agent_id, node_id))]


@router.put("/nodes/{node_id}/env", dependencies=[Depends(require_permission(P.STUDIO))])
def put_node_env(agent_id: str, node_id: str, body: EnvIn, session: Session = Depends(get_session),
                 if_match: Optional[str] = Header(None, alias="If-Match")) -> dict:
    """Set the node's whole Env in one transaction: the key list on the draft and
    the encrypted values commit together, or neither does (a stale If-Match gives
    409 and nothing is written). `entries` is the full list of keys the node
    declares; an entry with a `value` writes/rotates it, one without keeps the
    stored value, and a key left out is detached (kept as a retired secret while
    a published version still declares it). The response never contains values."""
    from .agents import _atomic_save_agent, _check_if_match
    a = _agent(session, agent_id)
    expected = _check_if_match(a, if_match)
    flow = copy.deepcopy(a.flow or {"nodes": [], "edges": []})
    node = _agent_node(flow, node_id)
    try:
        result = aa.apply_env_entries(session, agent_id, node_id, [e.model_dump() for e in body.entries])
    except aa.AssetError as e:
        session.rollback()
        raise HTTPException(e.status, e.detail())
    node.setdefault("config", {})["env"] = [{"key": r["key"]} for r in result]
    saved = _atomic_save_agent(session, agent_id, expected, flow=flow)
    return {"entries": result, "draft_revision": saved.draft_revision, "agent": saved}


class BindIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str


@router.post("/nodes/{node_id}/requirements/{requirement_id}/bind", dependencies=[Depends(require_permission(P.STUDIO))])
def bind_file_requirement(agent_id: str, node_id: str, requirement_id: str, body: BindIn,
                          session: Session = Depends(get_session),
                          if_match: Optional[str] = Header(None, alias="If-Match")) -> dict:
    """Resolve a template's file requirement with a file from this agent's
    library: the file is attached to the node and the requirement is cleared, in
    one draft update. Publishing is blocked while a required file is unresolved."""
    from .agents import _atomic_save_agent, _check_if_match
    a = _agent(session, agent_id)
    expected = _check_if_match(a, if_match)
    f = session.get(AgentFile, body.file_id)
    if not f or f.agent_id != agent_id or f.retired_at is not None:
        raise HTTPException(404, "File not found")
    flow = copy.deepcopy(a.flow or {"nodes": [], "edges": []})
    try:
        aa.bind_requirement(flow, node_id, requirement_id, f)
    except aa.AssetError as e:
        raise HTTPException(e.status, e.detail())
    saved = _atomic_save_agent(session, agent_id, expected, flow=flow)
    cfg = _agent_node(saved.flow, node_id).get("config") or {}
    return {"files": cfg.get("files", []), "files_required": cfg.get("files_required", []),
            "draft_revision": saved.draft_revision, "agent": saved}
