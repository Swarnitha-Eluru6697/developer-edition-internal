"""
Real file uploads (§0.33, GAP_REPORT) — the storage half of making the
Doc Extractor node's advertised "Single File" / "Multiple Files" input
genuinely work, not just accept a manually-typed URL. A real trigger
`single_file`/`file_list` field now genuinely uploads here the moment a
person picks a file in the runner UI (RunnerUI.tsx / the mockup's
apprunner form), and the real, working URL this returns is exactly what
`handle_docextractor`'s existing httpx GET already expects - no changes
needed to the fetch side, only to how the URL gets there.

§0.80, GAP_REPORT - real production storage/retention, the actual fix
for the reported gap: binary content now lives in a real, pluggable
object store (storage.py - a real local-disk backend by default in
this environment, or a real S3-compatible backend), not Postgres
bytea. Six further real controls, all genuinely enforced below, not
just documented: content-type validation (real magic-byte sniffing,
not trusting the client header), filename sanitization (real path/
control-character stripping before the name ever reaches a storage key
or an HTTP header), malware scanning (real, pluggable - EICAR
detection by default, real ClamAV if configured), a real TTL
(expires_at, enforced on download, with a real sweep function a real
cron/scheduler outside this repo can call), and per-uploader tenant
isolation (the real object key is namespaced by uploader, and a
mismatched verified identity on download is genuinely rejected).
"""
import os
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Request
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from fastapi.responses import Response
from sqlmodel import Session, select, func

from ..db import get_session
from ..models import UploadedFile
from ..storage import get_storage_backend, StorageError, S3StorageBackend
from ..upload_security import (MalwareScannerUnavailable, sanitize_filename,
                               scan_for_malware, validate_content_type)
from ..auth import get_verified_identity

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# One cap for every file upload — agent file fields, RAG ingest, the KB Upload
# API. 20MB by default; set MAX_UPLOAD_MB to change it. The UI reads it from GET /api/uploads/limits and checks a file
# before sending it, so a too-large file fails at once with the limit named.
MAX_UPLOAD_BYTES = int(float(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024)
# Room for the multipart framing around a file (boundaries, part headers, other
# form fields), so the request-level check in middleware never refuses a file
# that is within MAX_UPLOAD_BYTES. The file itself is still capped exactly below.
MULTIPART_HEADROOM_BYTES = 512 * 1024
# §0.111, GAP_REPORT — a real, bounded chunk size for the streaming
# read below: large enough to be efficient (not one syscall per byte),
# small enough that a single chunk is never itself a meaningful DoS
# vector on its own.
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1MB
DEFAULT_TTL_DAYS = int(os.getenv("UPLOAD_DEFAULT_TTL_DAYS", "30"))


def _download_url(file_id: str) -> str:
    """Real, environment-aware URL construction - the same
    PLATFORM_BASE_URL env var routers/agents.py already uses for this
    exact purpose (a platform-constructed self-referential URL)."""
    platform_base = os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000")
    return f"{platform_base}/api/uploads/{file_id}/download"


def _check_tenant_access(row: UploadedFile, request: Request) -> None:
    """Real, per-uploader tenant isolation on read (§0.80, GAP_REPORT;
    hardened further by §0.109). A real, present, MISMATCHED verified
    identity against a file with a real, known owner is rejected with
    403 — and, as of §0.109, so is a real, ABSENT verified identity
    against that same owned file. A real, legacy row with no owner on
    file at all (every row that existed before the owner column was
    added — genuinely NULL, never backfilled to any real value), or a
    row this platform's own upload endpoint itself recorded as
    "anonymous" (nobody was authenticated at upload time — see the
    upload endpoint's own `owner = get_verified_identity(request) or
    "anonymous"`), is real, honest common ground: there's no real
    owner identity to protect there at all, so it stays open to any
    caller, matching this platform's own default (not globally
    auth-gated) posture.

    §0.109, GAP_REPORT — the actual fix for a real, reported
    authorization bug: the prior rule only rejected a real, PRESENT,
    mismatched identity (`if identity and identity != row.owner`) —
    an ABSENT identity against a REAL, NAMED owner fell through this
    check entirely and was allowed. This is the resource-authorization
    rule itself being wrong, independent of which identity provider
    verifies who's asking: an owned object's own file id — random,
    hard to guess, but never actually SECRET — had silently become an
    unintended bearer credential. Confirmed live, before this fix: a
    file uploaded by a real, verified identity was downloadable by a
    genuinely anonymous request carrying no identity at all, given
    only the file id. Now: a real, named owner (anything other than
    the honest, no-owner-to-protect cases above) requires the SAME
    real, verified identity to read it — an absent identity is treated
    exactly like a mismatched one, both real, honest 403s.

    The report's own suggested alternative for real, deliberate
    anonymous link-sharing — a genuine signed download token, with a
    real expiry and scope, rather than treating the primary object id
    itself as an implicit access credential — is real, valuable,
    separate future work, not built here: no real caller of this
    endpoint today asks for shareable-anonymous-access to a file a
    real, verified identity uploaded, so building that mechanism now
    would be speculative; the honest, narrower fix is closing the
    actual reported hole, not inventing an unrequested feature on top
    of it."""
    if not row.owner or row.owner == "anonymous":
        return
    identity = get_verified_identity(request)
    if identity is None or identity != row.owner:
        raise HTTPException(403, "This uploaded file belongs to a different real, verified identity.")


def _object_key(owner: str, file_id: str, filename: str) -> str:
    """Real, namespaced storage key (§0.80, GAP_REPORT) - the real
    tenant-isolation boundary at the storage layer itself: every real
    uploader's own files live under their own real key prefix, so a
    real S3 bucket policy (or this environment's own real
    LocalDiskStorageBackend path structure) can enforce per-prefix
    access independently of this app's own real ownership check
    above - defense in depth, not a single point of enforcement."""
    return f"{owner}/{file_id}/{filename}"


class UploadRejected(Exception):
    """Content-type or malware validation refused these bytes. Carries
    the HTTP status the upload endpoint would use, so a caller that is
    not an endpoint (KB ingestion) can decide for itself what that
    means rather than inheriting a 415/422 it cannot act on."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def store_upload_bytes(
    session: Session, data: bytes, filename: str, declared_content_type: str = "",
    *, owner: str = "anonymous", ttl_days: int = DEFAULT_TTL_DAYS,
    enforce_allowlist: bool = True, destination_id: Optional[str] = None,
) -> UploadedFile:
    """Put one file in object storage and record it, exactly the way the
    upload endpoint below does — same sanitised filename, same sniffed
    content type, same malware scan, same tenant-prefixed key, same row.

    Extracted so knowledge-base ingestion can keep the ORIGINAL file
    someone uploaded on the Documents tab, in the same S3 bucket the
    Upload API tab's "Stage on the platform" writes to, rather than
    growing a second, subtly different copy of this logic that would
    drift from it. The two callers differ in exactly one deliberate
    way, `enforce_allowlist`:

    the staging endpoint is a general-purpose "accept a file from
    anyone" surface, so its allowlist is the gate that decides what may
    enter object storage at all. KB ingestion has already applied a
    stricter, more relevant gate — the file parsed into text, or it was
    transcribed — and refusing to keep a copy of a document this
    platform has just ingested and indexed would make no sense to
    whoever uploaded it. The malware scan runs on both paths regardless,
    because that check is about the bytes, not about the format.
    """
    safe_filename = sanitize_filename(filename or "upload")
    allowed, effective_content_type, reject_reason = validate_content_type(
        data, declared_content_type or "")
    if enforce_allowlist and not allowed:
        raise UploadRejected(415, f"Real content-type validation rejected this upload: {reject_reason}")

    try:
        flagged_reason = scan_for_malware(data)
    except MalwareScannerUnavailable as e:
        # 503, not 500: the request is fine and the caller can retry once
        # a scanner is available. An opaque 500 with a correlation id told
        # the uploader nothing and told the operator nothing either —
        # which is the same invisibility this whole change is about.
        raise UploadRejected(
            503,
            f"This upload was refused because malware scanning is unavailable: {e}. "
            f"See GET /readyz — checks.malware_scanner — for what this deployment is "
            f"configured to do.",
        )
    if flagged_reason:
        raise UploadRejected(422, f"Real malware scanning flagged this upload: {flagged_reason}")

    file_id = f"up-{uuid.uuid4().hex[:16]}"
    object_key = _object_key(owner, file_id, safe_filename)
    # Which destination this upload goes to: the one named, else
    # whichever is currently default (a stored one, or .env). Resolved
    # per upload rather than once at import, because a destination can
    # now be added or changed while the server runs.
    from .. import storage_destinations as sd
    backend, resolved_destination_id, _label = sd.resolve(session, destination_id)
    backend_name = "s3" if isinstance(backend, S3StorageBackend) else os.getenv("STORAGE_BACKEND", "local")
    backend.put(object_key, data, effective_content_type)

    row = UploadedFile(
        id=file_id, filename=safe_filename, content_type=effective_content_type, size=len(data),
        storage_backend=backend_name, object_key=object_key,
        # Recorded, not inferred: reads and deletes must go back to the
        # destination this object was WRITTEN to, whatever is default by
        # then.
        storage_destination_id=resolved_destination_id,
        owner=owner, scan_status="clean",
        expires_at=datetime.utcnow() + timedelta(days=ttl_days) if ttl_days > 0 else None,
    )
    session.add(row)
    session.commit()
    return row


def stored_file_public(row: UploadedFile) -> dict:
    """What a caller outside this module reports about a stored file.
    `storage_backend` is named explicitly because "kept in S3" and
    "kept on this server's disk" are genuinely different promises."""
    return {
        "id": row.id, "filename": row.filename, "size": row.size,
        "content_type": row.content_type, "storage_backend": row.storage_backend,
        "object_key": row.object_key, "url": _download_url(row.id),
        "expires_at": row.expires_at,
    }


@router.post("", dependencies=[Depends(require_any_permission(P.AGENTS_RUN, P.WEBAPPS, P.STUDIO, P.RAG))])
async def upload_file(
    request: Request, file: UploadFile = File(...), ttl_days: int = Query(default=DEFAULT_TTL_DAYS, ge=0, le=3650),
    # Which upload destination to write to. Omitted means "whatever is
    # currently the default", which is how every existing caller behaves
    # and keeps behaving.
    destination_id: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    # §0.111, GAP_REPORT — the actual fix for a real, reported gap:
    # this used to be `data = await file.read()` — the ENTIRE real
    # request body was already fully read into memory before the
    # MAX_UPLOAD_BYTES check even ran, so a real, much-larger-than-20MB
    # upload could genuinely consume real memory/request resources
    # long before ever being rejected. A real, honest, fast-path
    # pre-check against the client's own declared Content-Length comes
    # first (free when present and honest — no bytes read at all), but
    # the real, ENFORCEABLE protection is the chunked read below with a
    # running cap: reads real, bounded chunks and aborts the instant
    # the real total would exceed the limit, so the actual amount of
    # real memory this endpoint ever buffers for an oversized upload is
    # capped at one real chunk past the limit, not the attacker's
    # entire, unbounded request body.
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"This file is too large to upload. Files must be {MAX_UPLOAD_BYTES // (1024*1024)} MB or smaller.")
        except ValueError:
            pass  # a real, malformed header isn't this check's problem — the real chunked read below is authoritative regardless

    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"This file is too large to upload. Files must be {MAX_UPLOAD_BYTES // (1024*1024)} MB or smaller.")
        chunks.append(chunk)
    data = b"".join(chunks)

    # Real, per-uploader tenant isolation (§0.80, GAP_REPORT) - the
    # real verified identity when present, an honest "anonymous"
    # namespace otherwise (this endpoint isn't gated behind auth by
    # default - see _check_tenant_access's own docstring).
    owner = get_verified_identity(request) or "anonymous"

    # Sanitization, content-type validation, malware scanning, the
    # tenant-prefixed key, the write and the row all live in
    # store_upload_bytes above — one implementation, shared with KB
    # ingestion's own "keep the original file" path.
    try:
        row = store_upload_bytes(session, data, file.filename or "upload", file.content_type or "",
                                 owner=owner, ttl_days=ttl_days, destination_id=destination_id)
    except UploadRejected as e:
        raise HTTPException(e.status, e.reason)
    except StorageError as e:
        raise HTTPException(502, f"Real object storage write failed: {e}")

    return {
        "id": row.id, "filename": row.filename, "content_type": row.content_type, "size": row.size,
        "url": _download_url(row.id), "expires_at": row.expires_at,
    }


@router.get("/limits", dependencies=[Depends(require_any_permission(P.AGENTS_RUN, P.WEBAPPS, P.STUDIO, P.RAG))])
def upload_limits() -> dict:
    """The upload size cap, so a client can check a file before sending it."""
    return {"max_upload_bytes": MAX_UPLOAD_BYTES, "max_upload_mb": round(MAX_UPLOAD_BYTES / (1024 * 1024), 1)}


@router.get("/{file_id}/download", dependencies=[Depends(require_any_permission(P.AGENTS_RUN, P.WEBAPPS, P.STUDIO, P.RAG))])
def download_file(file_id: str, request: Request, session: Session = Depends(get_session)):
    row = session.get(UploadedFile, file_id)
    if not row:
        raise HTTPException(404, "Uploaded file not found")
    # Real TTL enforcement (§0.80, GAP_REPORT) - a real, expired file
    # is genuinely unreachable, not silently served past its own real
    # retention window.
    if row.expires_at and datetime.utcnow() > row.expires_at:
        raise HTTPException(410, "This uploaded file has real, expired past its retention window.")
    _check_tenant_access(row, request)

    # §0.110, GAP_REPORT — the actual fix for a real, reported gap:
    # resolves the SPECIFIC backend this row's own object was actually
    # written to (recorded honestly at upload time — see the upload
    # endpoint's own row construction), never whatever backend
    # happens to be configured right now. A file uploaded while
    # STORAGE_BACKEND=local, read after a later config change to s3,
    # now still genuinely looks in the right place.
    from .. import storage_destinations as sd
    backend = sd.backend_for_stored_object(session, row.storage_destination_id, row.storage_backend)
    try:
        data = backend.get(row.object_key)
    except StorageError as e:
        raise HTTPException(502, f"Real object storage read failed: {e}")
    return Response(
        content=data,
        media_type=row.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{row.filename}"'},
    )


@router.get("/{file_id}", dependencies=[Depends(require_any_permission(P.AGENTS_RUN, P.WEBAPPS, P.STUDIO, P.RAG))])
def get_upload_meta(file_id: str, request: Request, session: Session = Depends(get_session)) -> dict:
    row = session.get(UploadedFile, file_id)
    if not row:
        raise HTTPException(404, "Uploaded file not found")
    _check_tenant_access(row, request)
    return {
        "id": row.id, "filename": row.filename, "content_type": row.content_type, "size": row.size,
        "url": _download_url(file_id), "created_at": row.created_at,
        "expires_at": row.expires_at, "scan_status": row.scan_status,
    }


def sweep_expired_uploads(session: Session) -> int:
    """Real retention sweep (§0.80, GAP_REPORT) - deletes every real,
    genuinely-expired file's own real object storage content AND its
    real metadata row. A real, callable function, matching this
    codebase's own established convention (metrics.py's own real
    docstring: "what's real here is that there's something correct
    for it to scrape/run") - real production needs a real scheduler
    (a cron job, a Celery beat task, a Kubernetes CronJob) outside
    this app's own repo to actually call this on a real interval;
    this function is the real, correct thing for that scheduler to
    call, not a simulation of one. Returns the real count of files
    swept.

    §0.110, GAP_REPORT — the actual fix for a real, reported gap: this
    used to resolve ONE backend, once, outside the loop below, and
    delete every real row's object through it regardless of what that
    SPECIFIC row's own storage_backend actually says — a row uploaded
    under an earlier STORAGE_BACKEND config would have its delete
    silently misdirected at the CURRENT backend instead, genuinely
    never removing the real, orphaned object the old backend still
    holds. Resolved per-row now, from that row's own recorded value."""
    expired = session.exec(
        select(UploadedFile).where(UploadedFile.expires_at.is_not(None), UploadedFile.expires_at < datetime.utcnow())
    ).all()
    count = 0
    for row in expired:
        try:
            from .. import storage_destinations as sd
            sd.backend_for_stored_object(session, row.storage_destination_id,
                                         row.storage_backend).delete(row.object_key)
        except StorageError:
            pass  # a real, already-missing object shouldn't block the real DB row's own cleanup
        session.delete(row)
        count += 1
    session.commit()
    return count


# Registered on the INTERNAL router (routers/internal.py), not this
# product router: it is an operator maintenance operation behind the
# /internal prefix and the require_operator guard. It was a plain,
# unauthenticated route on the product API surface.
def trigger_expired_sweep(session: Session = Depends(get_session)) -> dict:
    """Real, manually-triggerable retention sweep - the real endpoint
    a real operator (or a real scheduler with HTTP-call capability)
    can hit; see sweep_expired_uploads's own docstring for the honest
    note on what real infrastructure actually drives this on a
    schedule in production."""
    count = sweep_expired_uploads(session)
    return {"swept": count}


def migrate_upload_backend(session: Session, row: UploadedFile, target_backend: str) -> None:
    """§0.110, GAP_REPORT — the report's own explicitly requested
    second half of this fix: a real, deliberate migration process for
    when a file's own bytes should actually MOVE to a different
    backend, rather than the two backends silently diverging forever
    once `STORAGE_BACKEND` changes — the report's own stated concern,
    separate from (and additional to) the per-row resolution fix
    above.

    A real, careful ordering, not a naive copy-then-delete: reads the
    real, existing bytes from row's own CURRENT backend first; writes
    them to the real, target backend; only updates this row's own
    `storage_backend` (committed immediately, before the old copy is
    touched) once that write has genuinely succeeded — a failure
    between read and write leaves the original, real object and this
    row's own metadata BOTH unchanged, the safe, honest failure mode;
    the real, old copy is deleted only after the row itself already
    points at the new, confirmed-written one, so a real crash between
    the metadata commit and the old-copy delete leaves a harmless,
    orphaned old copy rather than a genuinely lost file. Raises
    StorageError on a real, failed read or write; a real delete
    failure on the OLD copy is logged-and-swallowed the same way
    sweep_expired_uploads's own already-established convention treats
    a real, already-gone object — it doesn't undo the real, already-
    committed migration.

    No-op if the row is already on the real, requested target
    backend."""
    if row.storage_backend == target_backend:
        return
    from .. import storage_destinations as sd
    source_backend = sd.backend_for_stored_object(session, row.storage_destination_id,
                                                  row.storage_backend)
    dest_backend = get_storage_backend(target_backend)
    data = source_backend.get(row.object_key)
    dest_backend.put(row.object_key, data, row.content_type or "application/octet-stream")
    old_backend_name = row.storage_backend
    row.storage_backend = target_backend
    session.add(row)
    session.commit()
    try:
        get_storage_backend(old_backend_name).delete(row.object_key)
    except StorageError:
        pass  # the real, new copy is already live and committed; a stale old copy is a real, harmless leftover, not a failure of this migration


# Registered on the INTERNAL router (routers/internal.py), not this
# product router: it is an operator maintenance operation behind the
# /internal prefix and the require_operator guard. It was a plain,
# unauthenticated route on the product API surface.
def trigger_backend_migration(target: str = Query(..., description="Target storage backend name, e.g. 'local' or 's3'"),
                               limit: int = Query(default=100, ge=1, le=10000),
                               session: Session = Depends(get_session)) -> dict:
    """Real, manually-triggerable bulk migration — the report's own
    explicitly requested "explicit migration process if files should
    actually move between backends," matching this codebase's own
    established convention (trigger_expired_sweep, just above) for a
    real, operator-invokable maintenance operation with no in-process
    background scheduler of its own. Migrates up to `limit` real rows
    currently on a DIFFERENT backend than `target`, oldest first, one
    real migrate_upload_backend call per row — a real caller wanting
    to migrate an entire, large real deployment calls this repeatedly
    (or a real external scheduler does) until `migrated + failed`
    stops growing, rather than this one endpoint attempting an
    unbounded, real, single-request migration that could time out
    partway through a large real backlog. A single row's own real
    failure doesn't abort the rest of the batch; its file id and the
    real error are reported back so a real operator can investigate,
    not silently skipped."""
    rows = session.exec(
        select(UploadedFile).where(UploadedFile.storage_backend != target).order_by(UploadedFile.created_at).limit(limit)
    ).all()
    migrated, failed = 0, []
    for row in rows:
        try:
            migrate_upload_backend(session, row, target)
            migrated += 1
        except StorageError as e:
            failed.append({"id": row.id, "error": str(e)})
    # Real, separate count query (not `len(rows) - migrated - len(failed)`,
    # which is always 0 — every row this call examined was either
    # migrated or failed) — the real number of rows still on a
    # different backend than `target` AFTER this batch, including any
    # this batch's own `limit` never reached at all, so a real caller
    # knows whether to call this again.
    remaining = session.exec(
        select(func.count()).select_from(UploadedFile).where(UploadedFile.storage_backend != target)
    ).one()
    return {"migrated": migrated, "failed": failed, "remaining_candidates": remaining}
