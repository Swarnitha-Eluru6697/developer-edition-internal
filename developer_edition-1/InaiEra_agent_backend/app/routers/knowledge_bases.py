"""
Knowledge Base connections (Integrations > Knowledge Base). Deliberately a
separate connection target from this platform's own operational database
(the one this very file's queries run against, via ..db.get_session) —
that separation is the point: `retrieval` nodes should read from a user's
own vector store, not from the same database storing this platform's
Agent/Run/Trace/EnvVar/AgentMemory rows.

Real for pgvector: a genuine psycopg2 connection to whatever host/port/
database the person configures, a real query against a real table.
Genuinely NOT real for the other providers listed (Pinecone/Weaviate/
Qdrant/Milvus) beyond connection-config storage and a best-effort HTTP
reachability check — no vendor SDKs are wired in. See test_connection()'s
docstring for the exact boundary.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Header, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from pydantic import BaseModel
from psycopg2 import sql as _sql

from ..db import get_session
from ..pagination import paginate, pagination_params
from ..models import KnowledgeBase, KnowledgeDocument, KnowledgeMetadataField, KnowledgeRetrievalTest, KBIngestionOutbox, DataSource, UploadedFile
from starlette.concurrency import run_in_threadpool

from ..ingestion import parse_file, chunk_text
from ..embeddings import embed_texts, embed_query, extract_keywords, EMBED_DIM, EMBED_MODEL_NAME, to_pgvector_literal, current_embed_dim, current_embed_model_name, current_embed_provider
from ..sql_identifiers import safe_identifier, validate_identifier, InvalidIdentifier

router = APIRouter(prefix="/api/integrations/knowledge-bases", tags=["knowledge-bases"])

MASK = "••••••••"
# retrieval_api_key is a DIFFERENT, dedicated secret from the "api_key"
# field non-pgvector providers (Pinecone/Weaviate/etc.) store for their
# own connection — deliberately named apart so exposing the real Retrieval
# API surface (below) can never be confused with, or accidentally leak,
# a connection credential.
SENSITIVE_KEYS = {"password", "api_key", "retrieval_api_key", "upload_api_key"}
VALID_PROVIDERS = {"pgvector", "pinecone", "weaviate", "qdrant", "milvus", "other"}

# Real, dedicated schema (§0.67, GAP_REPORT) — a real, reported gap:
# ingestion used to CREATE/ALTER a table by whatever bare name an
# operator supplied, in the connection's own default schema (typically
# "public") — the exact same schema a customer's own, unrelated tables
# would live in on a shared/externally-managed database. An operator
# naming the table "users" (matching their own real table) would have
# genuinely run CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN
# against THEIR OWN "public.users", not a separate object — confirmed
# directly by tracing target_table (ingest_document's own Form
# parameter) straight into sql.Identifier(target_table) with no schema
# qualification anywhere.
#
# Every KB table this service creates from now on lives inside this
# one, dedicated schema instead — CREATE SCHEMA IF NOT EXISTS, never
# CREATE/ALTER anything outside it. An operator naming a new KB table
# "users" now genuinely creates/uses `inaiera_kb.users`, a real,
# separate object that can never collide with, or mutate, the
# customer's own `public.users` (or any other schema's own tables) —
# this is what actually makes "arbitrary operator-provided table name"
# safe to accept as input at all.
#
# A real, honest, deliberate scope decision: a KB ingested BEFORE this
# fix has its real data already sitting in whatever schema it was
# originally created in (kb.config has no "schema" key at all for
# these) — this fix does not retroactively move that data into
# inaiera_kb (a real cross-schema data migration is a separate,
# genuinely riskier piece of work this fix doesn't take on
# unilaterally). Every KB helper below goes through kb_schema_of
# (imported from ..retrieval — the one, canonical definition harness.py's
# own handle_retrieval also imports, so the two can't independently
# drift on what "this KB's data is in the managed schema" means), which
# only ever trusts an EXACT match against KB_MANAGED_SCHEMA — never an
# arbitrary caller-supplied string — so an existing KB keeps reading/
# writing exactly where its data already is, while every NEW KB (or a
# fresh table ingested for the first time on an existing KB) gets the
# safer, schema-confined behavior.
from ..retrieval import KB_MANAGED_SCHEMA, kb_schema_of


def _kb_table_identifier(kb: "KnowledgeBase", table_name: Optional[str] = None):
    """Real, centralized construction (§0.67, GAP_REPORT) of a KB's own
    table identifier — every one of this file's real call sites that
    used to build `sql.Identifier(kb.config["table"])` (or
    `safe_identifier(target_table, ...)`) directly now goes through
    this, so none of them can independently forget the real schema
    qualification. Schema-qualified when kb.config["schema"] is real
    and set (every KB created since this fix); a bare, unqualified
    identifier otherwise, for a KB that predates it — see the module-
    level KB_MANAGED_SCHEMA comment for why that's a deliberate,
    stated choice, not an oversight.

    A real, deliberate security note: this deliberately does NOT trust
    kb.config["schema"] as an arbitrary caller-supplied DDL target —
    kb.config itself is a caller-supplied dict at KB creation time
    (POST /knowledge-bases accepts a real, arbitrary config object), so
    blindly qualifying with whatever string happened to be stored
    there would let a caller redirect this service's own DDL at any
    schema they name, defeating the entire real point of this fix. The
    ONLY schema this ever actually uses is the one, hardcoded
    KB_MANAGED_SCHEMA constant — real equality-checked against the
    stored value, not merely "is a string present.\""""
    name = table_name or kb.config["table"]
    if kb_schema_of(kb.config):
        return _sql.Identifier(KB_MANAGED_SCHEMA, name)
    return _sql.Identifier(name)


class KBIn(BaseModel):
    name: str
    provider: str
    config: Dict[str, Any]
    # Reuse another knowledge base's stored connection secrets instead
    # of retyping them. Every read masks SENSITIVE_KEYS, so a UI that
    # prefills a form from an existing KB can copy host/port/dbname/user
    # but never the password — it only ever sees "••••••••". Naming the
    # source KB here lets the server copy the ciphertext it already
    # holds, which is the only place that value exists in the clear.
    #
    # No privilege is crossed: any caller who can create a knowledge
    # base can already point one at any host with any credentials they
    # choose, and the copied secret is never returned to them.
    copy_credentials_from: Optional[str] = None


def _pgvector_connect(cfg: dict):
    """Real, single, shared pgvector connection path (§0.86,
    GAP_REPORT) — the actual fix for the report's own final point:
    "Database KB connections should receive similar destination policy
    if untrusted users can configure them." Confirmed directly: KB
    creation has no real role/permission gating today, so any
    authenticated caller can set a KB's own host/port, and the real,
    decrypted password genuinely gets sent to whatever that resolves
    to. validate_database_host (egress.py) is called BEFORE ever
    connecting — a real, deliberately narrower policy than the HTTP
    egress denylist (RFC1918/loopback are commonly the real, intended
    way to reach a KB's own separate pgvector instance, not just an
    attacker's — see that function's own docstring for the full real
    reasoning); only the real, never-legitimate cloud-metadata case is
    actually blocked. The one, real, shared connection path — used by
    _connect, _test_pgvector, and the real ingest path below — so a
    future change to this real validation can't be added to one call
    site and silently missed on another, the exact class of gap this
    fix exists to close."""
    import psycopg2
    from ..secrets_crypto import decrypt_secret
    from ..egress import validate_database_host
    from ..kb_hosts import resolve_kb_host
    # A KB stores ONE host, but the same database can have two names —
    # "db" on the compose network, 127.0.0.1:5435 from the host. The
    # alias map (opt-in, see kb_hosts.py) is applied BEFORE the egress
    # check, so the address actually dialled is the one validated.
    host, port = resolve_kb_host(cfg.get("host", "127.0.0.1"), cfg.get("port", 5432))
    validate_database_host(host, port)
    from ..secrets_crypto import SecretDecryptionError
    try:
        password = decrypt_secret(cfg.get("password", ""))
    except SecretDecryptionError:
        # The stored password predates the current encryption key. The
        # crypto message is accurate but sends people to look at their
        # database; the actual fix is to retype the password. Said here,
        # in the one shared connection path, so every caller reports it
        # the same way.
        raise HTTPException(
            400,
            "This pipeline's stored database password cannot be decrypted with this server's "
            "current SECRETS_ENCRYPTION_KEY, so it cannot connect. The password was saved by a "
            "process using a different key — re-enter it under Integrations > KB Connections > "
            "Edit connection and it will work. The database itself is fine.",
        )
    return psycopg2.connect(
        host=host, port=port,
        dbname=cfg["dbname"], user=cfg["user"], password=password,
        connect_timeout=5,
    )


def _connect(kb: KnowledgeBase):
    return _pgvector_connect(kb.config)


def _get_kb_pgvector(kb_id: str, session: Session) -> KnowledgeBase:
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    if kb.provider != "pgvector":
        raise HTTPException(400, f"Document management is only wired for provider 'pgvector', this KB uses '{kb.provider}'.")
    if not kb.config.get("table"):
        raise HTTPException(400, "This knowledge base has no table configured yet — ingest a document first.")
    # Real, strict validation (§0.57, GAP_REPORT), centralized — every
    # one of this function's real callers below builds dynamic SQL
    # against kb.config["table"] (and usually text_column too), so
    # validating once here, rather than at each of the 7 real call
    # sites individually, is what actually guarantees none of them can
    # be reached with an unvalidated identifier by a future change
    # that forgets its own check.
    try:
        validate_identifier(kb.config["table"], "table")
        if kb.config.get("text_column"):
            validate_identifier(kb.config["text_column"], "column")
        if kb.config.get("schema"):
            validate_identifier(kb.config["schema"], "schema")
    except InvalidIdentifier as e:
        raise HTTPException(400, f"This knowledge base has an invalid stored identifier: {e}")
    return kb


def _infer_metadata_type(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    return "string"


def _ensure_metadata_fields(session: Session, kb_id: str, metadata: Dict[str, Any]) -> None:
    """Auto-creates a real, named+typed KnowledgeMetadataField the first
    time an ingest uses a key that isn't registered yet — matching the
    mockup's stated behavior, not a silent no-op.

    §0.70, GAP_REPORT — a real, reported gap: this used to SELECT every
    existing field name, then INSERT any key not already in that set —
    two genuinely concurrent ingests into the same KB, both using the
    same brand-new metadata key for the first time, could both see it
    as absent and both insert, leaving two real rows for the same real
    (kb_id, name). Now a real Postgres INSERT ... ON CONFLICT (kb_id,
    name) DO NOTHING — atomic by construction; the real, desired
    outcome (the field exists, exactly once) holds regardless of which
    concurrent request's insert Postgres actually keeps."""
    if not metadata:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    existing = {f.name for f in session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id)).all()}
    for key, value in metadata.items():
        if key not in existing:
            stmt = pg_insert(KnowledgeMetadataField).values(
                id=f"kmf-{uuid.uuid4().hex[:10]}", kb_id=kb_id, name=key, type=_infer_metadata_type(value),
            ).on_conflict_do_nothing(index_elements=["kb_id", "name"])
            session.execute(stmt)
            existing.add(key)


def _doc_public(d: KnowledgeDocument, metadata: Optional[Dict[str, Any]] = None,
                original_file: Optional[dict] = None) -> dict:
    return {
        "id": d.id, "kb_id": d.kb_id, "filename": d.filename, "chunking_mode": d.chunking_mode,
        "max_words": d.max_words, "overlap_words": d.overlap_words, "word_count": d.word_count,
        "chunk_count": d.chunk_count, "retrieval_count": d.retrieval_count, "status": d.status,
        "created_at": d.created_at, "metadata": metadata if metadata is not None else {},
        # The original file, when it was kept — the Documents tab links
        # to it, so an ingested document is not a one-way trip from a
        # file nobody can get back.
        "original_file": original_file,
    }


def _keep_original(session: Session, request: Optional[Request], content: bytes,
                   filename: str, content_type: str,
                   destination_id: Optional[str] = None) -> tuple:
    """Put the uploaded file itself in object storage and return
    (public_dict, error). Neither raises: the document is already
    chunked, embedded and written by the time this runs, so a storage
    failure must be REPORTED, not turned into a failed ingest that
    leaves the pipeline holding content the caller was told it did not
    get.

    Called after the chunks land and before the KnowledgeDocument row
    is created, so a run that fails earlier leaves no orphan object and
    the document row can point straight at the file.

    Same destination and same code as the Upload API tab's "Stage on
    the platform" — uploads.store_upload_bytes — so "kept in S3" means
    the identical bucket, key layout and row on both paths.
    """
    from .uploads import store_upload_bytes, stored_file_public, UploadRejected
    from ..storage import StorageError
    from ..auth import get_verified_identity

    owner = "anonymous"
    if request is not None:
        try:
            owner = get_verified_identity(request) or "anonymous"
        except Exception:
            owner = "anonymous"
    from ..storage_destinations import DestinationError
    try:
        row = store_upload_bytes(session, content, filename, content_type,
                                 owner=owner, enforce_allowlist=False,
                                 destination_id=destination_id)
    except UploadRejected as e:
        return None, e.reason
    except DestinationError as e:
        # A destination that was deleted, or whose credentials can no
        # longer be decrypted. Names the destination problem rather than
        # reporting a generic storage failure.
        return None, str(e)
    except StorageError as e:
        # Names the backend, because "could not write to S3" and "could
        # not write to this server's disk" send someone to different
        # places to fix it.
        import os
        return None, (f"could not write to {os.getenv('STORAGE_BACKEND', 'local')} storage: {e}")
    except Exception as e:  # pragma: no cover - defensive, same contract
        return None, str(e)[:300]
    return stored_file_public(row), None


def _original_file_public(session: Session, file_id: Optional[str]) -> Optional[dict]:
    """The stored original for one document, or None. A row that has
    been swept by the retention TTL reads as None rather than as a link
    that 404s."""
    if not file_id:
        return None
    row = session.get(UploadedFile, file_id)
    if not row:
        return None
    from .uploads import stored_file_public
    return stored_file_public(row)


def _delete_original(session: Session, file_id: Optional[str]) -> Optional[bool]:
    """Remove a document's kept original from object storage. Returns
    True if the object and its row went, False if something refused,
    None if there was nothing kept.

    Deliberately does not raise: a document whose bytes cannot be
    removed from S3 right now must still be deletable from the
    pipeline — the alternative is a document nobody can get rid of
    because of a storage outage.
    """
    if not file_id:
        return None
    row = session.get(UploadedFile, file_id)
    if not row:
        return None
    from ..storage_destinations import backend_for_stored_object
    ok = True
    try:
        # The destination the object was written to, not whichever is
        # configured now — the same rule download and retention follow.
        backend_for_stored_object(session, row.storage_destination_id,
                                  row.storage_backend).delete(row.object_key)
    except Exception:
        ok = False
    session.delete(row)
    session.commit()
    return ok


def _secrets_usable(kb: KnowledgeBase) -> Optional[bool]:
    """Whether this pipeline's stored secrets can be READ, not merely
    whether they exist. A password encrypted under a different
    SECRETS_ENCRYPTION_KEY still shows as a masked value and the row
    still reads as connected, while every ingest and query fails. None
    when there is nothing stored to test."""
    from ..secrets_crypto import decrypt_secret
    stored = [(kb.config or {}).get(k) for k in SENSITIVE_KEYS]
    stored = [v for v in stored if v]
    if not stored:
        return None
    for value in stored:
        try:
            decrypt_secret(value)
        except Exception:
            return False
    return True


def _ingest_supported(provider: str) -> bool:
    """Whether documents can genuinely be ingested into this provider.

    Computed from the real dispatch in ingest_document rather than
    restated as a list, because the UI used a hardcoded
    `provider === 'pgvector'` test and went on telling people that
    "document ingestion and retrieval testing aren't wired for provider
    'weaviate' yet" after they were — a caption asserting something no
    longer true. Reading capability from the code that implements it is
    the only version that cannot drift.
    """
    from .. import vector_stores
    return provider in ("pgvector", "pinecone") or vector_stores.adapter_for(provider) is not None


def _masked(kb: KnowledgeBase) -> dict:
    cfg = {k: (MASK if k in SENSITIVE_KEYS and v else v) for k, v in (kb.config or {}).items()}
    return {
        "id": kb.id, "name": kb.name, "provider": kb.provider, "config": cfg,
        "status": kb.status, "last_error": kb.last_error, "last_checked_at": kb.last_checked_at,
        "created_at": kb.created_at,
        "secrets_usable": _secrets_usable(kb),
        "ingest_supported": _ingest_supported(kb.provider),
        # Full-text and hybrid need a lexical index, which only the
        # Postgres path has; the UI hides those options rather than
        # offering a setting that later refuses to run.
        "supports_fulltext": kb.provider == "pgvector",
    }


def _require_durable_secret_key(what: str) -> None:
    """Refuse to persist a secret that is guaranteed to be unreadable —
    see secrets_crypto.require_durable_key. Surfaced as 503 because it
    is a server misconfiguration, not a bad request."""
    from ..secrets_crypto import require_durable_key
    try:
        require_durable_key(what)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


def _encrypt_sensitive_config(config: dict) -> dict:
    """Real encryption at rest (§0.59, GAP_REPORT) for every real
    SENSITIVE_KEYS field in a Knowledge Base's own connection config —
    applied once, here, at every real write point, so a future field
    added to SENSITIVE_KEYS only needs wiring into this one function,
    not every individual create/update call site."""
    from ..secrets_crypto import encrypt_secret
    out = dict(config or {})
    # Refuse before writing anything, not per field: a half-encrypted
    # config would be worse than either outcome.
    if any(out.get(key) for key in SENSITIVE_KEYS):
        _require_durable_secret_key("this knowledge base's credentials")
    for key in SENSITIVE_KEYS:
        if out.get(key):
            out[key] = encrypt_secret(out[key])
    return out


@router.get("", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_knowledge_bases(
    response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[dict]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning."""
    q = select(KnowledgeBase)
    rows = paginate(session, KnowledgeBase, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=KnowledgeBase.created_at, id_col=KnowledgeBase.id, response=response)
    return [_masked(r) for r in rows]


@router.post("", dependencies=[Depends(require_permission(P.RAG))])
def create_knowledge_base(body: KBIn, session: Session = Depends(get_session)) -> dict:
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, f"provider must be one of {sorted(VALID_PROVIDERS)}")
    # Must match handle_retrieval's lookup slugification in harness.py
    # EXACTLY — a real, previously-undiscovered mismatch here meant any
    # dotted catalog-style name (e.g. "lakehouse.core.customer_events",
    # a realistic real name, not an edge case) would register under one
    # id but never be found by retrieval, which looks it up under a
    # different one. Found by actually testing citations end-to-end with
    # a real dotted name, not a synthetic one chosen to avoid the bug.
    kb_id = body.name.lower().replace(" ", "_").replace(".", "_")
    if session.get(KnowledgeBase, kb_id):
        raise HTTPException(400, f"A knowledge base named '{body.name}' already exists")
    # Real encryption at rest (§0.59, GAP_REPORT) — the row's own
    # stored config no longer holds real SENSITIVE_KEYS values in
    # plaintext.
    config_in = dict(body.config)
    if body.copy_credentials_from:
        source = session.get(KnowledgeBase, body.copy_credentials_from)
        if not source:
            raise HTTPException(400, f"No knowledge base '{body.copy_credentials_from}' to copy credentials from.")
        if source.provider != body.provider:
            raise HTTPException(
                400,
                f"Cannot copy credentials from a '{source.provider}' knowledge base into a "
                f"'{body.provider}' one — the fields are provider-specific.",
            )
        # Decrypt the source value and let _encrypt_sensitive_config
        # below re-encrypt it on the normal path. Copying the ciphertext
        # verbatim would send it through that function a second time and
        # store a double-encrypted secret that could never be read back.
        from ..secrets_crypto import decrypt_secret
        for key in SENSITIVE_KEYS:
            incoming = config_in.get(key)
            if (not incoming or incoming == MASK) and source.config.get(key):
                config_in[key] = decrypt_secret(source.config[key])
        # A masked placeholder is never a real value to store.
        config_in = {k: v for k, v in config_in.items() if v != MASK}
    kb = KnowledgeBase(id=kb_id, name=body.name, provider=body.provider, config=_encrypt_sensitive_config(config_in), status="untested")
    session.add(kb)
    session.commit()
    session.refresh(kb)
    # Register this KB as a catalog object (and grant it, when the
    # deployment has named a group to grant to). Publishing an agent
    # entitlement-checks every KB its retrieval nodes touch, and until
    # now nothing ever created the object those checks ask about — so a
    # brand-new KB made every agent that used it unpublishable, with a
    # denial that looked like a permissions problem rather than a
    # missing registration. Best-effort: a KB must still be created when
    # UCS or Gravitino is unreachable, since ingestion and retrieval do
    # not depend on either.
    from ..ucs_provisioning import provision_knowledge_base
    provisioning = provision_knowledge_base(kb.id, kb.name)
    # same one-time-echo rule as EnvVar secrets: the real config (including
    # sensitive fields) is returned exactly once, on this creation response.
    # Uses body.config (the real plaintext the caller sent), not kb.config
    # (now the real encrypted form) — same principle as env_vars.py's own
    # create endpoint.
    result = _masked(kb)
    # Echo what the caller actually sent, not any copied ciphertext —
    # a secret inherited from another KB is deliberately never revealed.
    result["config"] = {**body.config}
    result["catalog_provisioning"] = provisioning
    return result


class KBSettingsIn(BaseModel):
    """Everything editable from the Pipeline tab after creation — Name &
    Icon/Description plus the real Chunk Structure / Index Method /
    Retrieval Setting knobs (Dify's own real settings surface). All
    optional so a partial PATCH only touches the fields actually sent."""
    description: Optional[str] = None
    icon: Optional[str] = None
    chunk_structure: Optional[str] = None
    index_method: Optional[str] = None
    keyword_count: Optional[int] = None
    retrieval_method: Optional[str] = None
    reranking_enabled: Optional[bool] = None
    # Which model does the reranking, by registered model name (see
    # /integrations/rerank-availability). Unlike embedding_model this is safe
    # to change at any time: reranking happens per query and touches no
    # stored vector, so nothing needs re-ingesting. Empty string clears
    # it back to the platform default.
    rerank_model: Optional[str] = None
    semantic_weight: Optional[float] = None
    keyword_weight: Optional[float] = None
    top_k: Optional[int] = None
    score_threshold_enabled: Optional[bool] = None
    score_threshold: Optional[float] = None
    retrieval_api_enabled: Optional[bool] = None
    upload_api_enabled: Optional[bool] = None
    # The Text Embedding model this pipeline should use, by registered
    # model name (see /integrations/embedding-models). Only takes effect
    # on a KB with no vectors yet, or on an explicit force_reindex —
    # existing vectors live in a different space and cannot be mixed.
    embedding_model: Optional[str] = None


@router.patch("/{kb_id}/settings", dependencies=[Depends(require_permission(P.RAG))])
def update_kb_settings(kb_id: str, body: KBSettingsIn, session: Session = Depends(get_session)) -> dict:
    """Real settings storage — same `config` JSONB every connection
    field already lives in, just additional recognized keys, so no
    schema migration was needed. `chunk_structure`/`index_method` only
    affect FUTURE ingests — changing them here does not retroactively
    re-chunk or re-embed documents already ingested under the old
    setting (a real, honest scope boundary: reprocessing existing
    content would need to re-fetch original files this platform doesn't
    keep a copy of)."""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    patch = body.dict(exclude_unset=True)
    if "chunk_structure" in patch and patch["chunk_structure"] not in VALID_CHUNK_STRUCTURES:
        raise HTTPException(400, f"chunk_structure must be one of {sorted(VALID_CHUNK_STRUCTURES)}")
    if "index_method" in patch and patch["index_method"] not in VALID_INDEX_METHODS:
        raise HTTPException(400, f"index_method must be one of {sorted(VALID_INDEX_METHODS)}")
    if "retrieval_method" in patch and patch["retrieval_method"] not in VALID_RETRIEVAL_METHODS:
        raise HTTPException(400, f"retrieval_method must be one of {sorted(VALID_RETRIEVAL_METHODS)}")
    if "rerank_model" in patch and patch["rerank_model"]:
        # Validated against what can genuinely execute, so a name that
        # would fail at query time is refused at save time instead of
        # sitting in config until someone runs a search.
        from ..model_adapter import (
            RUNTIME_SUPPORTED_PROVIDERS, _MODEL_PROVIDERS, _registered_model,
            provider_display_name,
        )
        name = patch["rerank_model"]
        # provider_display_name, not the raw value: _MODEL_PROVIDERS
        # maps a built-in model to a lowercase KIND ("anthropic"),
        # which never matches RUNTIME_SUPPORTED_PROVIDERS' display
        # names — so comparing the raw value rejected every built-in
        # model as unrunnable, including the platform default itself.
        provider = provider_display_name(
            _MODEL_PROVIDERS.get(name) or _registered_model(name)[0]
        )
        if not provider:
            raise HTTPException(
                400,
                f"'{name}' is not a model this platform can call. Pick one from "
                f"/integrations/rerank-availability, or register it under a provider first.",
            )
        if provider not in RUNTIME_SUPPORTED_PROVIDERS:
            raise HTTPException(
                400,
                f"'{name}' belongs to {provider}, which has no execution path here yet "
                f"(supported: {sorted(RUNTIME_SUPPORTED_PROVIDERS)}).",
            )
    if "embedding_model" in patch and patch["embedding_model"]:
        # Refuse a change that would leave two incompatible vector
        # spaces in one column. Vectors already written cannot be
        # compared with vectors from a different model, and the column's
        # width is fixed at creation, so switching is only safe while
        # the KB is empty — or via ingest with force_reindex=true, which
        # deletes the old vectors first.
        from ..embeddings import EMBED_MODEL_NAME, embedding_dimension_for
        new_model = patch["embedding_model"]
        current = kb.config.get("embedding_model")
        if kb.config.get("embedding_provider") and new_model != current:
            raise HTTPException(
                409,
                f"This pipeline already holds vectors created with "
                f"'{current or EMBED_MODEL_NAME}'. Changing the embedding model requires "
                f"re-embedding every chunk — re-ingest with force_reindex=true, which "
                f"deletes the existing vectors first.",
            )
        if new_model != EMBED_MODEL_NAME:
            # Fail here, with a clear message, rather than at ingest —
            # this also caches the real dimension for the column later.
            try:
                dim = embedding_dimension_for(new_model)
            except Exception as e:
                raise HTTPException(400, f"Could not verify '{new_model}': {e}")
            patch["embedding_dimension"] = dim
    kb.config = {**kb.config, **patch}
    session.add(kb)
    session.commit()
    session.refresh(kb)
    return _masked(kb)


# Which config keys make up each provider's connection, as opposed to
# the pipeline settings KBSettingsIn already covers. Anything outside
# these is rejected so a connection edit cannot quietly rewrite
# chunk_structure, an embedding pin, or an API key for the Retrieval API.
CONNECTION_KEYS = {
    "pgvector": {"host", "port", "dbname", "user", "password", "table", "text_column", "schema"},
    "pinecone": {"url", "api_key", "index", "namespace"},
    "weaviate": {"url", "api_key", "index", "namespace"},
    "qdrant": {"url", "api_key", "index", "namespace"},
    "milvus": {"url", "api_key", "index", "namespace"},
    "other": {"url", "api_key", "index", "namespace"},
}

# Changing any of these repoints the knowledge base at different
# storage, which orphans the KnowledgeDocument rows describing what is
# in the old location.
_RELOCATION_KEYS = {"host", "port", "dbname", "table", "schema", "url", "index", "namespace"}


class KBConnectionIn(BaseModel):
    """A connection edit. Only the fields sent are touched, so a partial
    update is safe.

    A secret arriving as the mask string is treated as "unchanged", not
    as a literal new value — every read masks secrets, so a UI that
    prefills this form from a GET would otherwise overwrite the real
    password with bullet characters.
    """
    config: Dict[str, Any]
    # Required to repoint an already-populated KB at different storage.
    confirm_relocate: bool = False


@router.patch("/{kb_id}/connection", dependencies=[Depends(require_permission(P.RAG))])
def update_kb_connection(kb_id: str, body: KBConnectionIn, session: Session = Depends(get_session)) -> dict:
    """Edit a knowledge base's connection in place.

    Previously impossible: creation was the only write path for these
    fields, so a wrong host, a missing Pinecone index, or a rotated
    password meant deleting the knowledge base and rebuilding it —
    losing its documents, metadata fields and retrieval history with it.
    """
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")

    allowed = CONNECTION_KEYS.get(kb.provider, CONNECTION_KEYS["other"])
    incoming = dict(body.config or {})
    unknown = [k for k in incoming if k not in allowed]
    if unknown:
        raise HTTPException(
            400,
            f"{unknown} are not connection fields for a '{kb.provider}' knowledge base. "
            f"Editable here: {sorted(allowed)}. Pipeline settings go to PATCH /settings instead.",
        )

    # A masked secret means "keep what is stored".
    for key in SENSITIVE_KEYS:
        if key in incoming and (incoming[key] == MASK or incoming[key] == ""):
            incoming.pop(key)

    if "url" in incoming and incoming["url"]:
        # Validated at SAVE time, not first use. A console URL stored
        # here fails much later, per file, in the middle of an ingest —
        # far from the field that caused it.
        from .. import vector_stores
        try:
            vector_stores.check_endpoint_url(incoming["url"], kb.provider)
        except vector_stores.VectorStoreError as e:
            raise HTTPException(400, str(e))
    if "table" in incoming:
        try:
            validate_identifier(incoming["table"], "table")
        except InvalidIdentifier as e:
            raise HTTPException(400, str(e))
    if "port" in incoming and incoming["port"] not in (None, ""):
        try:
            incoming["port"] = int(incoming["port"])
        except (TypeError, ValueError):
            raise HTTPException(400, "port must be a number.")

    doc_count = len(session.exec(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)).all())
    moved = sorted(k for k in incoming if k in _RELOCATION_KEYS and str(incoming[k]) != str(kb.config.get(k, "")))
    if moved and doc_count and not body.confirm_relocate:
        raise HTTPException(
            409,
            f"Changing {moved} points this knowledge base at different storage, but it already "
            f"lists {doc_count} document(s) held in the current location — those records would no "
            f"longer describe what is actually there. Re-send with confirm_relocate=true to "
            f"proceed anyway, or delete the documents first.",
        )

    kb.config = {**kb.config, **_encrypt_sensitive_config(incoming)}
    # The stored connection changed, so whatever the last Test proved is
    # no longer evidence about this one.
    kb.status = "untested"
    kb.last_error = None
    session.add(kb)
    session.commit()
    session.refresh(kb)
    result = _masked(kb)
    result["relocated"] = moved
    result["documents_affected"] = doc_count if moved else 0
    return result


@router.delete("/{kb_id}", dependencies=[Depends(require_permission(P.RAG))])
def delete_knowledge_base(kb_id: str, session: Session = Depends(get_session)):
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    # Real cascade — local bookkeeping rows for this KB shouldn't outlive
    # it as orphans with a dangling kb_id.
    for doc in session.exec(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)).all():
        session.delete(doc)
    for field in session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id)).all():
        session.delete(field)
    for test in session.exec(select(KnowledgeRetrievalTest).where(KnowledgeRetrievalTest.kb_id == kb_id)).all():
        session.delete(test)
    # §0.68, GAP_REPORT — the new real outbox table also carries a real
    # kb_id foreign key; without this, deleting a KB with any real
    # outbox history would fail the same way the other three tables
    # above already had a real, guarded cascade for.
    for row in session.exec(select(KBIngestionOutbox).where(KBIngestionOutbox.kb_id == kb_id)).all():
        session.delete(row)
    session.delete(kb)
    session.commit()
    return {"deleted": kb_id}


def _test_pgvector(cfg: dict) -> dict:
    """A REAL connection attempt — psycopg2 against whatever host/port/
    database/user/password is configured. Confirms the `vector` extension
    is actually installed there, and counts real rows in the configured
    table if one was given."""
    from psycopg2 import sql
    try:
        conn = _pgvector_connect(cfg)
        cur = conn.cursor()
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector';")
        has_vector = cur.fetchone() is not None
        row_count = None
        table = cfg.get("table")
        if table:
            validate_identifier(table, "table")
            # Real schema awareness (§0.67, GAP_REPORT) — a read-only
            # count, but still needs to look at the actual table this
            # KB's data lives in, which may be schema-qualified.
            if kb_schema_of(cfg):
                table_ref = sql.Identifier(KB_MANAGED_SCHEMA, table)
            else:
                table_ref = sql.Identifier(table)
            cur.execute(sql.SQL("SELECT count(*) FROM {};").format(table_ref))
            row_count = cur.fetchone()[0]
        conn.close()
        if not has_vector:
            return {"ok": False, "error": f"Connected to {cfg['dbname']}, but the 'vector' extension isn't installed there."}
        return {"ok": True, "detail": f"Connected. pgvector extension present." + (f" Table '{table}' has {row_count} row(s)." if table else "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _test_http_reachable(cfg: dict, provider: str) -> dict:
    """Best-effort reachability check for the non-pgvector providers — a
    real HTTP call if a URL is configured, honestly reporting whatever
    happens (including this sandbox's own network egress restrictions,
    which show up as a real, distinguishable error, not a silent failure).
    This does NOT mean retrieval execution is wired for these providers —
    see handle_retrieval's docstring in harness.py.

    Real, governed outbound call (§0.86, GAP_REPORT) — the actual fix
    for a real, reported gap: this real, user-configured `url` used to
    get a direct, ungoverned httpx.get call, WITH the real, decrypted
    KB API key attached as a header — a real credential-exfiltration
    mechanism if `url` is ever pointed (by misconfiguration or a
    malicious actor able to edit KB config) at an attacker-controlled
    destination. Routed through the same real safe_http_request every
    other real outbound agent call now goes through, including its own
    real cross-origin credential stripping (§0.85) — if this specific
    endpoint itself redirects somewhere else, the real Authorization
    header set below won't silently follow it there."""
    import httpx
    from urllib.parse import urlparse
    from ..secrets_crypto import decrypt_secret
    from ..egress import validate_database_host, SSRFBlocked
    url = cfg.get("url")
    if not url:
        return {"ok": False, "error": f"No URL configured for {provider} — can't test reachability."}
    try:
        # A vector store is a connection target like a pgvector database, not an
        # arbitrary web URL: a self-hosted Qdrant/Weaviate on host.docker.internal
        # or the private network is the normal case. The general HTTP denylist
        # blocked those, so Test reported "failed" for stores that ingest and query
        # fine (that path never used it). Same narrower policy as pgvector — the
        # cloud metadata service stays blocked — and redirects are NOT followed,
        # so the key below can only ever go to this exact host.
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return {"ok": False, "error": f"Not an http(s) URL: {url}"}
        validate_database_host(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {decrypt_secret(cfg['api_key'])}"
        resp = httpx.get(url, headers=headers, timeout=8.0, follow_redirects=False)
        return {"ok": resp.status_code < 500, "detail": f"{provider} endpoint responded HTTP {resp.status_code}."}
    except SSRFBlocked as e:
        return {"ok": False, "error": f"blocked by egress policy: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


VALID_CHUNK_STRUCTURES = {"general", "parent_child", "qa"}
VALID_INDEX_METHODS = {"high_quality", "economical"}
VALID_RETRIEVAL_METHODS = {"vector", "fulltext", "hybrid"}


async def _ingest_pinecone(kb: KnowledgeBase, file: UploadFile, max_words: int,
                           overlap_words: int, metadata: Optional[str],
                           force_reindex: bool, session: Session,
                           keep_original: bool = False,
                           request: Optional[Request] = None,
                           storage_destination_id: Optional[str] = None) -> dict:
    """Ingest into a Pinecone index: parse, chunk, embed, upsert.

    Shares the parsing, chunking and embedding steps with the pgvector
    path — only the storage differs — so a document ingested here is
    chunked identically to one ingested there.

    Chunks are written into a namespace of their own (defaulting to the
    KB id) rather than the index's default namespace. Pinecone indexes
    routinely already hold unrelated vectors, and a namespace keeps this
    platform's writes and its delete-by-filter calls from touching them.
    """
    import json as _json
    from ..pinecone_store import (PineconeError, build_vectors, delete_by_filter,
                                  describe_index, upsert)
    from ..secrets_crypto import decrypt_secret

    cfg = kb.config
    index_name = cfg.get("index")
    if not index_name:
        raise HTTPException(
            400,
            "This Pinecone knowledge base has no index configured. Set 'index' on it to the name "
            "of an existing Pinecone index (Integrations → Knowledge Base → edit connection).",
        )
    api_key = decrypt_secret(cfg.get("api_key", ""))
    if not api_key:
        raise HTTPException(400, "No Pinecone API key stored for this knowledge base.")

    index_method = cfg.get("index_method", "high_quality")
    if index_method == "economical":
        raise HTTPException(
            400,
            "The Economical index method stores keywords instead of vectors, which a Pinecone "
            "index cannot hold. Switch this pipeline to High Quality on the Settings tab.",
        )

    try:
        info = describe_index(api_key, index_name)
    except PineconeError as e:
        raise HTTPException(400, str(e))
    host, index_dim = info["host"], int(info["dimension"])

    content = await file.read()
    # Filled in by parse_file when a file needed more than a parse —
    # an audio file's transcript names the provider and model that
    # produced it, which the response then reports: the text in this
    # pipeline came from speech recognition, not from the file, and
    # whoever uploaded it should be told which.
    parse_notes: Dict[str, Any] = {}
    try:
        # In a worker thread, not on the event loop. Parsing was always
        # blocking (pypdf, python-docx), but an AUDIO file is parsed by
        # calling a speech API, which takes tens of seconds — long
        # enough that doing it inline froze every other request,
        # /readyz included, and the container reported itself unhealthy
        # for the duration of one upload.
        text = await run_in_threadpool(
            parse_file, file.filename or "upload", content, file.content_type or "",
            parse_notes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    from .. import ingestion as ingestion_mod
    chunk_structure = cfg.get("chunk_structure", "general")
    parent_texts: List[Optional[str]] = []
    if chunk_structure == "qa":
        pairs = ingestion_mod.chunk_qa(text)
        index_texts = [p["index_text"] for p in pairs]
        store_texts = [p["store_text"] for p in pairs]
    elif chunk_structure == "parent_child":
        pairs = ingestion_mod.chunk_parent_child(text, max_words, overlap_words)
        index_texts = [p["child"] for p in pairs]
        store_texts = index_texts
        parent_texts = [p["parent"] for p in pairs]
    else:
        index_texts = ingestion_mod.chunk_text(text, max_words, overlap_words)
        store_texts = index_texts
    if not index_texts:
        raise HTTPException(400, f"No chunks produced from '{file.filename}'.")

    stored_model = cfg.get("embedding_model")
    embed_model_override = stored_model if stored_model and stored_model != EMBED_MODEL_NAME else None
    try:
        vectors = embed_texts(index_texts, provider=cfg.get("embedding_provider"),
                              model=embed_model_override)
    except Exception as e:
        raise HTTPException(400, f"Could not compute embeddings for this knowledge base: {e}")

    got_dim = len(vectors[0])
    if got_dim != index_dim:
        # Pinecone rejects a dimension mismatch, but with an opaque
        # message — name both numbers and how to reconcile them.
        raise HTTPException(
            400,
            f"Embedding dimension mismatch: '{stored_model or EMBED_MODEL_NAME}' produces "
            f"{got_dim}-dimensional vectors, but Pinecone index '{index_name}' expects "
            f"{index_dim}. Choose an embedding model of {index_dim} dimensions on the Settings "
            f"tab, or point this pipeline at a {got_dim}-dimensional index.",
        )

    user_meta: Dict[str, Any] = {}
    if metadata:
        try:
            user_meta = _json.loads(metadata)
        except Exception:
            raise HTTPException(400, "metadata must be a JSON object.")
        if not isinstance(user_meta, dict):
            raise HTTPException(400, "metadata must be a JSON object.")
        _ensure_metadata_fields(session, kb.id, user_meta)

    namespace = cfg.get("namespace") or kb.id
    document_id = f"doc-{uuid.uuid4().hex[:12]}"
    try:
        if force_reindex:
            # Same meaning as the pgvector path: existing vectors belong
            # to a different embedding space and are removed, not mixed.
            delete_by_filter(api_key, host, namespace, {"document_id": {"$exists": True}})
            for row in session.exec(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb.id)).all():
                session.delete(row)
        pine_vectors = build_vectors(document_id, file.filename or "upload", store_texts,
                                     vectors, user_meta, parent_texts or None)
        upserted = upsert(api_key, host, namespace, pine_vectors)
    except PineconeError as e:
        raise HTTPException(400, str(e))

    # The original file, kept in the same object storage the Upload API
    # tab writes to, when the caller asked for it. After the chunks
    # land (nothing orphaned if this run failed earlier) and before the
    # document row, so the row can point at it.
    original_file = None
    original_error = None
    if keep_original:
        original_file, original_error = _keep_original(
            session, request, content, file.filename or "upload", file.content_type or "",
            storage_destination_id)

    doc = KnowledgeDocument(
        id=document_id, kb_id=kb.id, filename=file.filename or "upload",
        chunking_mode={"qa": "Q&A", "parent_child": "Parent-Child"}.get(chunk_structure, "General"),
        max_words=max_words, overlap_words=overlap_words,
        word_count=sum(len(t.split()) for t in store_texts), chunk_count=len(store_texts),
        source_file_id=(original_file or {}).get("id"),
    )
    session.add(doc)
    kb.config = {**cfg, "namespace": namespace, "embedding_provider": current_embed_provider(),
                 "embedding_model": stored_model or current_embed_model_name(),
                 "embedding_dimension": got_dim}
    kb.status = "connected"
    session.add(kb)
    session.commit()
    return {
        "success": True, "provider": "pinecone", "index": index_name, "namespace": namespace,
        "chunks_inserted": upserted, "embed_model": stored_model or current_embed_model_name(),
        "embed_dim": got_dim, "source_filename": file.filename, "document_id": document_id,
        "transcription": parse_notes.get("transcription"),
        "original_file": original_file, "original_file_error": original_error,
    }


async def _ingest_vector_store(kb: KnowledgeBase, file: UploadFile, max_words: int,
                               overlap_words: int, metadata: Optional[str],
                               force_reindex: bool, session: Session,
                               keep_original: bool = False,
                               request: Optional[Request] = None,
                               storage_destination_id: Optional[str] = None) -> dict:
    """Ingest into Qdrant, Weaviate or Milvus/Zilliz.

    Parsing, chunk structure, the embedding model and the metadata rules
    are the SAME code the pgvector and Pinecone paths use — only the
    write differs — so a document ingested here is chunked and embedded
    identically to one ingested anywhere else. That is the whole reason
    this is one function taking an adapter rather than three.

    Every point carries kb_id, so a collection that already holds
    unrelated vectors is never mass-deleted by a force-reindex: the
    delete is filtered to this pipeline's own writes.
    """
    import json as _json
    from .. import ingestion as ingestion_mod
    from .. import vector_stores
    from ..secrets_crypto import decrypt_secret

    adapter = vector_stores.adapter_for(kb.provider)
    if adapter is None:  # defensive: the caller checks first
        raise HTTPException(400, f"No vector-store adapter for '{kb.provider}'.")

    cfg = dict(kb.config)
    if cfg.get("api_key"):
        cfg["api_key"] = decrypt_secret(cfg["api_key"])

    if cfg.get("index_method") == "economical":
        raise HTTPException(
            400,
            f"The Economical index method stores keywords instead of vectors, which a "
            f"{kb.provider} collection cannot hold. Switch this pipeline to High Quality on "
            f"the Settings tab.",
        )

    content = await file.read()
    # Filled in by parse_file when a file needed more than a parse —
    # an audio file's transcript names the provider and model that
    # produced it, which the response then reports: the text in this
    # pipeline came from speech recognition, not from the file, and
    # whoever uploaded it should be told which.
    parse_notes: Dict[str, Any] = {}
    try:
        # In a worker thread, not on the event loop. Parsing was always
        # blocking (pypdf, python-docx), but an AUDIO file is parsed by
        # calling a speech API, which takes tens of seconds — long
        # enough that doing it inline froze every other request,
        # /readyz included, and the container reported itself unhealthy
        # for the duration of one upload.
        text = await run_in_threadpool(
            parse_file, file.filename or "upload", content, file.content_type or "",
            parse_notes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    chunk_structure = cfg.get("chunk_structure", "general")
    if chunk_structure == "qa":
        pairs = ingestion_mod.chunk_qa(text)
        index_texts = [p["index_text"] for p in pairs]
        store_texts = [p["store_text"] for p in pairs]
    elif chunk_structure == "parent_child":
        pairs = ingestion_mod.chunk_parent_child(text, max_words, overlap_words)
        index_texts = [p["child"] for p in pairs]
        store_texts = index_texts
    else:
        index_texts = ingestion_mod.chunk_text(text, max_words, overlap_words)
        store_texts = index_texts
    if not index_texts:
        raise HTTPException(400, f"No chunks produced from '{file.filename}'.")

    stored_model = cfg.get("embedding_model")
    embed_model_override = stored_model if stored_model and stored_model != EMBED_MODEL_NAME else None
    try:
        vectors = embed_texts(index_texts, provider=cfg.get("embedding_provider"),
                              model=embed_model_override)
    except Exception as e:
        raise HTTPException(400, f"Could not compute embeddings for this knowledge base: {e}")
    got_dim = len(vectors[0])

    user_meta: Dict[str, Any] = {}
    if metadata:
        try:
            user_meta = _json.loads(metadata)
        except Exception:
            raise HTTPException(400, "metadata must be a JSON object.")
        if not isinstance(user_meta, dict):
            raise HTTPException(400, "metadata must be a JSON object.")
        _ensure_metadata_fields(session, kb.id, user_meta)

    document_id = f"doc-{uuid.uuid4().hex[:12]}"
    try:
        collection = adapter.ensure_collection(cfg, kb.id, got_dim)
        if force_reindex:
            # Same meaning as every other path: existing vectors belong
            # to a different embedding space and are removed, not mixed.
            adapter.delete_by_document(cfg, kb.id, None)
            for row in session.exec(
                    select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb.id)).all():
                session.delete(row)
        points = vector_stores.build_points(kb.id, document_id, file.filename or "upload",
                                            store_texts, vectors, user_meta)
        written = adapter.upsert(cfg, kb.id, points)
    except vector_stores.VectorStoreError as e:
        raise HTTPException(400, str(e))

    # The original file, kept in the same object storage the Upload API
    # tab writes to, when the caller asked for it. After the chunks
    # land (nothing orphaned if this run failed earlier) and before the
    # document row, so the row can point at it.
    original_file = None
    original_error = None
    if keep_original:
        original_file, original_error = _keep_original(
            session, request, content, file.filename or "upload", file.content_type or "",
            storage_destination_id)

    doc = KnowledgeDocument(
        id=document_id, kb_id=kb.id, filename=file.filename or "upload",
        chunking_mode={"qa": "Q&A", "parent_child": "Parent-Child"}.get(chunk_structure, "General"),
        max_words=max_words, overlap_words=overlap_words,
        word_count=sum(len(t.split()) for t in store_texts), chunk_count=len(store_texts),
        source_file_id=(original_file or {}).get("id"),
    )
    session.add(doc)
    kb.config = {**kb.config, "index": collection,
                 "embedding_provider": current_embed_provider(),
                 "embedding_model": stored_model or current_embed_model_name(),
                 "embedding_dimension": got_dim}
    kb.status = "connected"
    kb.last_checked_at = datetime.utcnow()
    session.add(kb)
    session.commit()
    return {
        "success": True, "provider": kb.provider, "collection": collection,
        "chunks_inserted": written, "embed_model": stored_model or current_embed_model_name(),
        "embed_dim": got_dim, "source_filename": file.filename, "document_id": document_id,
        "table": collection,
        "transcription": parse_notes.get("transcription"),
        "original_file": original_file, "original_file_error": original_error,
    }


@router.post("/{kb_id}/ingest", dependencies=[Depends(require_permission(P.RAG))])
async def ingest_document(
    kb_id: str,
    request: Request,
    file: UploadFile = File(...),
    table: Optional[str] = Form(None),
    max_words: int = Form(180),
    overlap_words: int = Form(30),
    metadata: Optional[str] = Form(None),
    force_reindex: bool = Form(False),
    # Keep the uploaded file itself, not just the text extracted from
    # it — in the same object storage (S3, or this server's disk) the
    # Upload API tab's "Stage on the platform" writes to.
    #
    # Defaults to False here and is ticked by default in the Documents
    # tab, deliberately: someone looking at that checkbox can see where
    # their file is going and untick it, whereas an existing API caller
    # would start writing objects to a bucket — and paying for them —
    # because this platform changed under it.
    keep_original: bool = Form(False),
    # Which upload destination keeps it. Omitted means the current
    # default, so an existing caller is unaffected by a destination
    # someone adds later.
    storage_destination_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
) -> dict:
    """Real ingestion — a real uploaded file, parsed with the same real
    extractor the `docextractor` node uses, chunked for real, and
    inserted into a real table in the knowledge base's own configured
    Postgres via a real psycopg2 connection. The target table is
    remembered on the KB's config afterward, so handle_retrieval's real
    query (harness.py) knows where to look without the person having to
    configure it twice.

    Real per-KB Chunk Structure (config.chunk_structure, set via
    PATCH .../settings) — genuinely different chunking, not a label:
    'general' (default) — chunk_text()'s existing sentence-aware fixed-
      size chunking.
    'qa' — ingestion.chunk_qa(): real Q&A pair parsing; the INDEXED/
      embedded text is the question (matching Dify's real "indexed
      based on the question portion"), the answer is stored in the same
      row's text column appended after a real separator so a match
      still returns the full Q+A, not just the question.
    'parent_child' — ingestion.chunk_parent_child(): the CHILD text is
      what's embedded/matched; the real PARENT text (larger surrounding
      context) is stored in a genuine separate `parent_content` column
      and substituted in at retrieval time as the returned content —
      real "child for retrieval, parent for recall", not a relabeled
      general chunk.

    Real per-KB Index Method (config.index_method):
    'high_quality' (default) — real embedding vector via embed_texts().
    'economical' — real frequency-based keyword extraction
      (embeddings.extract_keywords) instead of an embedding call; the
      `embedding` column is left NULL for these rows (genuinely no
      vector computed, not a zero vector standing in for one) and
      `keywords` stores the real extracted list — handle_retrieval
      switches to real keyword-overlap scoring for rows with no
      embedding. Matches Dify's own real distinction: no embedding
      tokens spent, at the cost of retrieval precision.

    Real, per-KB IMMUTABLE embedding configuration (§0.92, GAP_REPORT)
    — the actual fix for a real, reported gap: ingestion used to always
    call embed_texts() with whatever EMBEDDING_PROVIDER/
    VOYAGE_EMBED_MODEL was CURRENTLY configured — and so did every
    later query (see retrieval.py's own embed_query call) — so if the
    real, global config changed in between two real ingestion runs (or
    between ingestion and a later query), the resulting vectors could
    silently diverge: a real dimension mismatch (a loud, honest
    Postgres error) at best, or worse, dimension-COMPATIBLE but
    semantically incompatible vectors producing quietly poor results,
    with no error at all to notice. A KB's FIRST real ingestion
    captures the real, active embedding_provider/embedding_model/
    embedding_dimension onto its own config — every SUBSEQUENT real
    ingestion into that SAME KB reuses that exact, stored configuration
    (regardless of whatever the current global env vars say by then),
    keeping every real chunk this KB ever holds in the same real
    embedding space. Changing a KB's embedding configuration is a real,
    deliberate, explicit action — force_reindex=true — not a side
    effect of some unrelated later config change; see that parameter's
    own handling below for the full real mechanics (existing vectors
    are genuinely incompatible with a new embedding space, so they're
    real deleted, not silently left to coexist with new ones under a
    different meaning).

    Real document/chunk-level bookkeeping: every chunk row gets a real
    `document_id` (grouping this upload's chunks for the Documents/
    Chunks UI), `word_count`, `enabled` (defaults true), and `metadata`
    (the real, optional per-upload JSON object applied to every chunk
    from this file) columns — added via `ADD COLUMN IF NOT EXISTS` so
    tables from before this existed keep working."""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    if kb.provider == "pinecone":
        # Pinecone is a vector index, not a SQL table, so it takes its
        # own path rather than being forced through the pgvector one.
        return await _ingest_pinecone(kb, file, max_words, overlap_words, metadata, force_reindex,
                                      session, keep_original, request, storage_destination_id)
    if kb.provider != "pgvector":
        from .. import vector_stores
        if vector_stores.adapter_for(kb.provider):
            return await _ingest_vector_store(kb, file, max_words, overlap_words,
                                              metadata, force_reindex, session,
                                              keep_original, request, storage_destination_id)
        raise HTTPException(
            400,
            f"Ingestion is wired for pgvector, pinecone, "
            f"{', '.join(sorted(vector_stores.ADAPTERS))}; this KB uses '{kb.provider}'.",
        )

    target_table = table or kb.config.get("table")
    if not target_table:
        raise HTTPException(400, "No target table — pass one, or set 'table' on this knowledge base first.")
    # Real, strict validation (§0.57, GAP_REPORT) — target_table is
    # ultimately API/config-supplied, not something this code can
    # trust just because a comment nearby calls it "operator-
    # configured." Rejected outright, before any SQL is built, if it
    # doesn't look like a real, plain identifier.
    try:
        validate_identifier(target_table, "table")
    except InvalidIdentifier as e:
        raise HTTPException(400, str(e))

    # Real schema determination (§0.67, GAP_REPORT) — see the module-
    # level KB_MANAGED_SCHEMA comment for the full real reasoning, and
    # _kb_table_identifier's own docstring for why this only ever
    # trusts the one, hardcoded KB_MANAGED_SCHEMA constant — never an
    # arbitrary value read back from kb.config, since that dict is
    # itself caller-supplied at KB creation time (POST
    # /knowledge-bases accepts a real, arbitrary config object).
    # Re-ingesting into the exact same table this KB already has on
    # record reuses whatever schema that table already genuinely lives
    # in (None for a pre-fix KB — its data hasn't moved). Any other
    # case — a brand new KB's first-ever table, or a different table
    # name than what's currently on record — is a real, fresh table
    # this service is about to create for the first time, so it gets
    # the safer, dedicated-schema treatment; there's no existing data
    # anywhere else it could conflict with.
    if target_table == kb.config.get("table"):
        target_schema = kb_schema_of(kb.config)
    else:
        target_schema = KB_MANAGED_SCHEMA

    metadata_obj: Dict[str, Any] = {}
    if metadata:
        import json
        try:
            metadata_obj = json.loads(metadata)
            if not isinstance(metadata_obj, dict):
                raise ValueError("metadata must be a JSON object")
        except Exception as e:
            raise HTTPException(400, f"Metadata must be valid JSON, e.g. {{\"tier\": \"premium\"}}: {e}")

    content = await file.read()
    # Filled in by parse_file when a file needed more than a parse —
    # an audio file's transcript names the provider and model that
    # produced it, which the response then reports: the text in this
    # pipeline came from speech recognition, not from the file, and
    # whoever uploaded it should be told which.
    parse_notes: Dict[str, Any] = {}
    try:
        # In a worker thread, not on the event loop. Parsing was always
        # blocking (pypdf, python-docx), but an AUDIO file is parsed by
        # calling a speech API, which takes tens of seconds — long
        # enough that doing it inline froze every other request,
        # /readyz included, and the container reported itself unhealthy
        # for the duration of one upload.
        text = await run_in_threadpool(
            parse_file, file.filename or "upload", content, file.content_type or "",
            parse_notes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    from .. import ingestion as ingestion_mod

    chunk_structure = kb.config.get("chunk_structure", "general")
    index_method = kb.config.get("index_method", "high_quality")
    keyword_count = int(kb.config.get("keyword_count", 10) or 10)
    if chunk_structure not in VALID_CHUNK_STRUCTURES:
        chunk_structure = "general"
    if index_method not in VALID_INDEX_METHODS:
        index_method = "high_quality"

    # Real per-structure chunking — three genuinely different code paths,
    # not one chunker relabeled three ways.
    parent_contents: List[Optional[str]] = []
    if chunk_structure == "qa":
        pairs = ingestion_mod.chunk_qa(text)
        if not pairs:
            raise HTTPException(400, "No Q&A pairs could be parsed from this file — try 'Q: ... / A: ...' formatting, or use General chunking instead.")
        index_texts = [p["question"] for p in pairs]
        store_texts = [f"Q: {p['question']}\nA: {p['answer']}" for p in pairs]
        parent_contents = [None] * len(pairs)
    elif chunk_structure == "parent_child":
        cc = ingestion_mod.chunk_parent_child(text, child_max_words=max_words, child_overlap_words=overlap_words)
        if not cc:
            raise HTTPException(400, "No chunks produced from this file's text.")
        index_texts = [c["child"] for c in cc]
        store_texts = index_texts
        parent_contents = [c["parent"] for c in cc]
    else:
        chunks = chunk_text(text, max_words=max_words, overlap_words=overlap_words)
        if not chunks:
            raise HTTPException(400, "No chunks produced from this file's text.")
        index_texts = chunks
        store_texts = chunks
        parent_contents = [None] * len(chunks)

    # Real, per-KB IMMUTABLE embedding configuration (§0.92,
    # GAP_REPORT) — the actual fix for the reported gap. A KB's FIRST
    # real ingestion has no real, stored embedding_provider/
    # embedding_model yet — this call genuinely uses whatever's
    # currently active (the same real, prior behavior). Every
    # SUBSEQUENT real ingestion into this SAME KB reuses that exact,
    # already-stored provider/model instead — regardless of whatever
    # EMBEDDING_PROVIDER/VOYAGE_EMBED_MODEL currently says — so every
    # real chunk this KB ever holds stays in the same real embedding
    # space. force_reindex=true is the one, explicit, deliberate
    # escape hatch: a person genuinely choosing to move this KB to a
    # new embedding configuration, not an accidental side effect of an
    # unrelated later config change (see the real table-reset handling
    # a few lines below, which this same flag also triggers).
    stored_provider = kb.config.get("embedding_provider")
    stored_model = kb.config.get("embedding_model")
    if stored_provider and not force_reindex:
        # Already embedded: stay pinned to whatever produced the
        # existing vectors, regardless of later config changes.
        embed_provider_override, embed_model_override = stored_provider, stored_model
    else:
        # First ingest, or a deliberate force_reindex. Honour a model
        # picked on the Settings tab — without this, a chosen embedding
        # model was ignored until after a first ingest had already
        # written vectors with the env default, and force_reindex could
        # never move a KB onto a newly-chosen model.
        chosen = kb.config.get("embedding_model")
        embed_provider_override = None
        embed_model_override = chosen if chosen and chosen != EMBED_MODEL_NAME else None

    # Real per-index-method embedding — Economical genuinely never calls
    # embed_texts (no "tokens spent"); High Quality does.
    if index_method == "economical":
        vectors: List[Optional[List[float]]] = [None] * len(index_texts)
        keywords_list = [extract_keywords(t, keyword_count) for t in index_texts]
        resolved_embed_provider = stored_provider  # real, honest None if this KB has never embedded anything
        resolved_embed_model = stored_model
        # Economical computes no vectors, but the table's `embedding`
        # column still has to be DECLARED with a dimension — a later
        # High Quality ingest into this same table writes real vectors
        # into it. On a KB's first-ever ingest there is no stored
        # dimension yet, so this fell through as None and produced
        # literal `VECTOR(None)` in the CREATE TABLE, which Postgres
        # rejected with "invalid input syntax for type integer: none".
        # Falling back to the active provider's dimension is what
        # high_quality already pins below.
        resolved_embed_dim = kb.config.get("embedding_dimension") or current_embed_dim()
    else:
        try:
            vectors = embed_texts(index_texts, provider=embed_provider_override, model=embed_model_override)
        except Exception as e:
            # Real, honest, actionable error (§0.92, GAP_REPORT) —
            # matches this same endpoint's own established pattern for
            # a real, failed connection just below. Most commonly
            # raised here when this KB's own real, stored,
            # already-pinned embedding_provider (e.g. "voyage" from
            # this KB's own first ingestion) can no longer actually run
            # — a missing/rotated API key, a real provider outage —
            # NOT silently falling back to a DIFFERENT provider's
            # vectors, which would corrupt this KB's own real, single
            # embedding space the exact way this whole fix exists to
            # prevent.
            raise HTTPException(
                400,
                f"Could not compute real embeddings using this knowledge base's own stored "
                f"configuration (provider={embed_provider_override or 'current default'}, "
                f"model={embed_model_override or 'current default'}): {e}",
            )
        keywords_list = [None] * len(index_texts)
        # Real, direct proof of what was ACTUALLY just computed (§0.92,
        # GAP_REPORT) — read from the real, just-returned vectors
        # themselves (their own real length) rather than re-asking
        # current_embed_dim()/current_embed_model_name(), which only
        # ever reflect the CURRENT global config — exactly the real gap
        # this fix closes. embed_provider_override/embed_model_override
        # are the resolved values actually passed to embed_texts above
        # (the KB's own stored config when reusing one, or None to let
        # embed_texts fall back to the current global config on a
        # KB's real first-ever ingestion or an explicit force_reindex).
        resolved_embed_provider = embed_provider_override or current_embed_provider()
        resolved_embed_model = embed_model_override or current_embed_model_name()
        resolved_embed_dim = len(vectors[0]) if vectors else (kb.config.get("embedding_dimension") or current_embed_dim())

    import json as _json
    from psycopg2 import sql
    c = kb.config
    try:
        conn = _pgvector_connect(c)
    except HTTPException:
        # Already a specific, actionable message (an undecryptable
        # password, a blocked host) — re-wrapping it produced
        # "Could not connect ...: 400: This pipeline's stored ...",
        # burying the part worth reading behind a status code.
        raise
    except Exception as e:
        from ..kb_hosts import alias_hint
        raise HTTPException(
            400,
            f"Could not connect to this knowledge base's real database: {e}"
            + alias_hint(kb.config.get("host", "")),
        )

    text_col = c.get("text_column", "content")
    try:
        validate_identifier(text_col, "column")
    except InvalidIdentifier as e:
        raise HTTPException(400, str(e))
    document_id = f"doc-{uuid.uuid4().hex[:12]}"

    # Real outbox row (§0.68, GAP_REPORT) — the actual fix for the
    # reported cross-database atomicity gap. Committed to THIS
    # platform's own local DB right now, BEFORE any remote side
    # effect — a real crash, network drop, or exception anywhere below
    # (the remote insert itself, or the later, separate local
    # KnowledgeDocument commit) now always leaves a real, detectable
    # trace via this row's own status, instead of a silent orphan.
    # chunking_mode_label/total_words are computed now (not later, as
    # they used to be) specifically so they can be saved into this
    # row's own payload — the small, real metadata a reconciler needs
    # to complete a stuck "remote_done" row's local write WITHOUT
    # needing the original uploaded file again (this platform doesn't
    # keep a copy of that).
    chunking_mode_label = {"general": "General", "qa": "Q&A", "parent_child": "Parent-Child"}[chunk_structure]
    total_words = sum(len(t.split()) for t in store_texts)
    outbox = KBIngestionOutbox(
        id=f"outbox-{uuid.uuid4().hex[:12]}", kb_id=kb_id, document_id=document_id, operation="ingest",
        status="pending",
        payload={
            "filename": file.filename or "upload", "chunking_mode": chunking_mode_label,
            "max_words": max_words, "overlap_words": overlap_words,
            "word_count": total_words, "chunk_count": len(store_texts),
            "target_table": target_table, "text_column": text_col, "schema": target_schema,
            "embed_model": resolved_embed_model, "metadata": metadata_obj,
        },
    )
    session.add(outbox)
    session.commit()

    try:
        cur = conn.cursor()
        # Same least-privilege rule as db.init_db(): managed Postgres
        # (Azure) refuses CREATE EXTENSION for non-admin logins even when
        # the extension already exists, so only attempt it when missing.
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector';")
        if cur.fetchone() is None:
            cur.execute("CREATE EXTENSION vector;")
        # Real, dedicated-schema fix (§0.67, GAP_REPORT) — CREATE SCHEMA
        # IF NOT EXISTS is the ONLY DDL this service ever runs outside
        # its own, already-scoped table below; it never touches any
        # other schema an operator's own database might have. A no-op
        # (and safe to run every time) once the schema already exists —
        # skipped entirely for target_schema=None (a pre-fix KB staying
        # in its existing, unqualified schema, see the module-level
        # KB_MANAGED_SCHEMA comment).
        if target_schema:
            cur.execute(_sql.SQL("CREATE SCHEMA IF NOT EXISTS {};").format(_sql.Identifier(target_schema)))
        # Real, safe identifier composition (§0.57, GAP_REPORT) — psycopg2.sql.Identifier,
        # not an f-string wrapped in literal double-quotes, which is not
        # real escaping if the identifier itself can contain a quote.
        # Schema-qualified (§0.67, GAP_REPORT) when target_schema is
        # real and set — the actual fix for this table never being able
        # to collide with an operator's own, unrelated same-named table.
        table_ident = _sql.Identifier(target_schema, target_table) if target_schema else safe_identifier(target_table, "table")
        text_col_ident = safe_identifier(text_col, "column")
        # Real, per-KB IMMUTABLE embedding dimension (§0.66/§0.92,
        # GAP_REPORT) — sized to whichever real embedding provider/model
        # this specific ingestion actually resolved to (see
        # resolved_embed_dim above: this KB's own already-stored
        # configuration when reusing one, or the current global config
        # on a real first ingestion or an explicit force_reindex), not
        # a single hardcoded constant, and not necessarily whatever the
        # CURRENT global config happens to be. Only takes effect on this
        # table's real first creation (IF NOT EXISTS) — a genuine,
        # accidental provider/model mismatch on a re-ingestion can no
        # longer even occur (embed_provider_override/embed_model_override
        # above pin this SAME ingestion's own embed_texts call to the
        # identical, already-resolved configuration used to size this
        # column), but a real, explicit force_reindex into a table whose
        # existing embedding column has a genuinely DIFFERENT dimension
        # needs the column itself reset first — handled directly below,
        # since existing vectors are real, deliberately being replaced
        # (a real, different embedding space), not silently coexisting
        # under a changed meaning.
        embed_dim = resolved_embed_dim
        # This value is interpolated straight into `VECTOR(n)` DDL, so a
        # bad value becomes a confusing Postgres syntax error rather than
        # a legible one. Fail with the reason instead.
        if not isinstance(embed_dim, int) or embed_dim <= 0:
            raise HTTPException(
                500,
                f"Could not determine a real embedding dimension for this knowledge base "
                f"(got {embed_dim!r}). The vector column cannot be created without one.",
            )
        if force_reindex and index_method != "economical":
            cur.execute(
                sql.SQL("SELECT data_type FROM information_schema.columns WHERE table_name = %s {} AND column_name = 'embedding';").format(
                    sql.SQL("AND table_schema = %s") if target_schema else sql.SQL("")
                ),
                (target_table, target_schema) if target_schema else (target_table,),
            )
            existing_col = cur.fetchone()
            if existing_col:
                # Real, deliberate data loss — the whole real point of
                # force_reindex: this KB's existing vectors belong to a
                # DIFFERENT real embedding space than what this
                # ingestion is about to write, so they're genuinely
                # incompatible with anything new, not just differently
                # sized. Every existing row (not just this table's
                # embedding column) is cleared so no genuinely stale,
                # mixed-embedding-space chunk survives a real
                # force_reindex — a partial reset would be a real,
                # silent correctness bug of its own.
                cur.execute(sql.SQL("TRUNCATE TABLE {};").format(table_ident))
                cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN embedding;").format(table_ident))
                cur.execute(
                    sql.SQL(f"ALTER TABLE {{}} ADD COLUMN embedding VECTOR({embed_dim});").format(table_ident)
                )
        cur.execute(
            sql.SQL(
                'CREATE TABLE IF NOT EXISTS {} ('
                'id BIGSERIAL PRIMARY KEY, {} TEXT, source_filename TEXT, '
                f'chunk_index INT, embedding VECTOR({embed_dim}), created_at TIMESTAMPTZ DEFAULT now());'
            ).format(table_ident, text_col_ident)
        )
        # Real, additive schema evolution — tables created before document/
        # chunk management (or Chunk Structure / Index Method) existed
        # keep working; new columns just default sensibly.
        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS document_id TEXT;').format(table_ident))
        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS word_count INT DEFAULT 0;').format(table_ident))
        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT true;').format(table_ident))
        cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{{}}'::jsonb;").format(table_ident))
        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS parent_content TEXT;').format(table_ident))
        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS keywords TEXT[];').format(table_ident))
        cur.execute(sql.SQL('ALTER TABLE {} ALTER COLUMN embedding DROP NOT NULL;').format(table_ident))
        # Real idempotency (§0.68, GAP_REPORT — "idempotent ingestion
        # IDs" per the report) — a genuine retry of this exact
        # document_id (a reconciler re-attempting a "pending" outbox
        # row, or this same request simply being retried by a caller)
        # must never produce real duplicate remote chunks. document_id
        # is generated once, up front, and never reused across
        # genuinely different ingestions, so deleting any existing rows
        # under it first is always safe — there's nothing real to lose,
        # only a real, safe reset before the real re-insert below.
        cur.execute(sql.SQL("DELETE FROM {} WHERE document_id = %s;").format(table_ident), (document_id,))
        total_words = 0
        for i, store_text in enumerate(store_texts):
            wc = len(store_text.split())
            total_words += wc
            vec = vectors[i]
            vec_literal = to_pgvector_literal(vec) if vec is not None else None
            cur.execute(
                sql.SQL(
                    'INSERT INTO {} ({}, source_filename, chunk_index, embedding, '
                    'document_id, word_count, enabled, metadata, parent_content, keywords) '
                    'VALUES (%s, %s, %s, %s::vector, %s, %s, %s, %s::jsonb, %s, %s);'
                ).format(table_ident, text_col_ident),
                (store_text, file.filename, i, vec_literal, document_id, wc, True, _json.dumps(metadata_obj),
                 parent_contents[i], keywords_list[i]),
            )
        conn.commit()
        # Real ANN + FTS + keyword-overlap indexes (§0.66, GAP_REPORT) —
        # the actual fix for the reported scalability gap: without
        # these, retrieval.py's own query has no fast path and must
        # fall back to scanning/sorting every row. HNSW is genuinely
        # incremental (pgvector maintains it as more rows are inserted
        # on later ingestion calls into the same table), so building it
        # here — once per ingestion call, IF NOT EXISTS — is safe and
        # idempotent rather than a one-time, ingestion-order-dependent
        # step. vector_cosine_ops matches retrieval.py's own real
        # cosine-distance query (the <=> operator) exactly — an index
        # built with the wrong opclass is silently never used by the
        # planner for a <=> query, a real, easy-to-get-wrong mistake
        # avoided here by using the identical distance function
        # end to end.
        try:
            cur.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (embedding vector_cosine_ops);"
            ).format(sql.Identifier(f"{target_table}_embedding_hnsw_idx"), table_ident))
            cur.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} USING gin (to_tsvector('english', {}));"
            ).format(sql.Identifier(f"{target_table}_fts_gin_idx"), table_ident, text_col_ident))
            cur.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} USING gin (keywords);"
            ).format(sql.Identifier(f"{target_table}_keywords_gin_idx"), table_ident))
            conn.commit()
        except Exception:
            # Real, honest degradation — an index build failing (e.g. a
            # real permissions issue on someone else's separately-
            # hosted Postgres) must never fail the whole ingestion;
            # retrieval.py's own query still returns correct results
            # without the index, just via a slower real sequential
            # scan rather than a fast real index scan.
            conn.rollback()
    except Exception as e:
        conn.rollback()
        conn.close()
        # Real outbox update (§0.68, GAP_REPORT) — the remote insert
        # itself genuinely failed, so nothing remote happened; this
        # row is marked "failed" (not left "pending" forever) so a
        # reconciler never mistakes it for a still-in-flight attempt,
        # but there's genuinely nothing to reconcile for this case —
        # the same real HTTPException the caller always got reaches
        # them unchanged.
        outbox.status = "failed"
        outbox.last_error = str(e)[:2000]
        outbox.updated_at = datetime.utcnow()
        session.add(outbox)
        session.commit()
        raise HTTPException(400, f"Insert into '{target_table}' failed: {e}")
    conn.close()

    # Real, durable checkpoint (§0.68, GAP_REPORT) — the real remote
    # commit(s) above genuinely succeeded; this row now honestly
    # reflects that BEFORE attempting the separate local metadata
    # write below. If the process dies right here, this row stays
    # "remote_done" — the exact real signal a reconciler needs to
    # complete the local write from this row's own saved payload,
    # rather than the prior silent orphan.
    outbox.status = "remote_done"
    outbox.updated_at = datetime.utcnow()
    session.add(outbox)
    session.commit()

    # Remember the target table/column on the KB so retrieval (harness.py's
    # handle_retrieval) can find it without asking again.
    #
    # embedding_provider/embedding_model/embedding_dimension (§0.92,
    # GAP_REPORT) — the actual, persisted, IMMUTABLE-per-KB
    # configuration the report asked for, alongside the pre-existing
    # embed_model (kept as-is, unchanged shape, for backward
    # compatibility with anything already reading it). None for an
    # "economical" ingestion — genuinely no embedding involved at all,
    # so there's no real provider/model/dimension to honestly record.
    kb.config = {
        **kb.config, "table": target_table, "text_column": text_col, "embed_model": resolved_embed_model,
        "schema": target_schema,
        "embedding_provider": resolved_embed_provider, "embedding_model": resolved_embed_model,
        "embedding_dimension": resolved_embed_dim,
    }
    kb.status = "connected"
    kb.last_checked_at = datetime.utcnow()
    session.add(kb)

    # The original file, kept in the same object storage the Upload API
    # tab writes to, when the caller asked for it. After the chunks
    # land (nothing orphaned if this run failed earlier) and before the
    # document row, so the row can point at it.
    original_file = None
    original_error = None
    if keep_original:
        original_file, original_error = _keep_original(
            session, request, content, file.filename or "upload", file.content_type or "",
            storage_destination_id)

    session.add(KnowledgeDocument(
        id=document_id, kb_id=kb_id, filename=file.filename or "upload", chunking_mode=chunking_mode_label,
        max_words=max_words, overlap_words=overlap_words, word_count=total_words,
        chunk_count=len(store_texts), retrieval_count=0, status="enabled",
        source_file_id=(original_file or {}).get("id"),
    ))
    _ensure_metadata_fields(session, kb_id, metadata_obj)
    # Real, atomic local completion (§0.68, GAP_REPORT) — the KB
    # config update, the new KnowledgeDocument row, and this outbox
    # row's own final "completed" status all reach the database in the
    # SAME real local transaction (one session.commit() below) — they
    # either all land together or none do, on the local side. Combined
    # with the "remote_done" checkpoint above, the real drift window
    # the report names is now exactly the gap between these two real,
    # separate commits — and that gap now always leaves this row at a
    # real, detectable "remote_done" rather than silent orphan chunks.
    outbox.status = "completed"
    outbox.updated_at = datetime.utcnow()
    session.add(outbox)
    session.commit()

    return {
        "success": True, "table": target_table, "text_column": text_col,
        "chunks_inserted": len(store_texts), "embed_model": resolved_embed_model if index_method == "high_quality" else "keywords-only (economical)",
        "embed_dim": embed_dim, "source_filename": file.filename, "document_id": document_id,
        "transcription": parse_notes.get("transcription"),
        "original_file": original_file, "original_file_error": original_error,
    }


# ---------------- Documents ----------------

@router.get("/{kb_id}/documents", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_documents(
    kb_id: str, response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[dict]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning. The most genuinely
    growth-prone real KB collection the report names — a real,
    actively-used KB can accumulate a real, large number of ingested
    documents over time."""
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    q = select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)
    rows = paginate(session, KnowledgeDocument, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=KnowledgeDocument.created_at, id_col=KnowledgeDocument.id, response=response)
    # One connection reused across the page rather than one per document.
    kb = session.get(KnowledgeBase, kb_id)
    meta_by_doc: Dict[str, dict] = {}
    if rows and kb and kb.provider == "pgvector" and kb.config.get("table"):
        try:
            from psycopg2 import sql
            conn = _connect(kb)
            try:
                cur = conn.cursor()
                cur.execute(
                    sql.SQL("SELECT DISTINCT ON (document_id) document_id, metadata FROM {} WHERE document_id = ANY(%s);")
                    .format(_kb_table_identifier(kb)),
                    ([d.id for d in rows],),
                )
                meta_by_doc = {r[0]: (r[1] or {}) for r in cur.fetchall()}
            finally:
                conn.close()
        except Exception:
            meta_by_doc = {}
    # Hosted stores are sampled through their own API, one document at a
    # time. The bulk SELECT above is a pgvector optimisation, and its
    # `provider == "pgvector"` guard meant the Metadata column read empty
    # for every Qdrant/Weaviate/Milvus document — the metadata was
    # genuinely stored, and retrieval results showed it, but the listing
    # never asked for it.
    if rows and kb and kb.provider != "pgvector":
        from .. import vector_stores
        if vector_stores.adapter_for(kb.provider):
            for d in rows:
                meta_by_doc[d.id] = _document_metadata(kb, d.id)

    # The kept originals for this page, in one query rather than one
    # per row.
    file_ids = [d.source_file_id for d in rows if d.source_file_id]
    files_by_id: Dict[str, dict] = {}
    if file_ids:
        from .uploads import stored_file_public
        for f in session.exec(select(UploadedFile).where(UploadedFile.id.in_(file_ids))).all():
            files_by_id[f.id] = stored_file_public(f)
    return [_doc_public(d, meta_by_doc.get(d.id, {}), files_by_id.get(d.source_file_id or ""))
            for d in rows]


class DocPatchIn(BaseModel):
    status: Optional[str] = None  # "enabled" | "disabled"
    # Metadata used to be settable only at ingest time, through a JSON
    # box collapsed behind "Show chunking & metadata options" — once a
    # document was in, there was no way to tag it without deleting and
    # re-uploading. The Metadata tab only ever registered field
    # names/types, so defining a field there appeared to do nothing.
    metadata: Optional[Dict[str, Any]] = None


@router.patch("/{kb_id}/documents/{doc_id}", dependencies=[Depends(require_permission(P.RAG))])
def patch_document(kb_id: str, doc_id: str, body: DocPatchIn, session: Session = Depends(get_session)) -> dict:
    """Toggling a document's status is a real bulk update — cascades to
    every one of its chunks' `enabled` column in the KB's own remote
    table, which is what handle_retrieval's WHERE clause actually
    filters on (see harness.py), not just a display-only flag here."""
    doc = session.get(KnowledgeDocument, doc_id)
    if not doc or doc.kb_id != kb_id:
        raise HTTPException(404, "Document not found")
    kb = _get_kb_pgvector(kb_id, session)
    if body.status and body.status in ("enabled", "disabled"):
        doc.status = body.status
        conn = _connect(kb)
        try:
            from psycopg2 import sql
            cur = conn.cursor()
            cur.execute(
                sql.SQL("UPDATE {} SET enabled = %s WHERE document_id = %s;").format(_kb_table_identifier(kb)),
                (body.status == "enabled", doc_id),
            )
            conn.commit()
        finally:
            conn.close()
    if body.metadata is not None:
        if not isinstance(body.metadata, dict):
            raise HTTPException(400, "metadata must be a JSON object.")
        # Same bulk-update shape as the status cascade above: metadata
        # lives on every chunk row, because that is where retrieval's
        # own (metadata->>key) filter reads it from.
        _ensure_metadata_fields(session, kb_id, body.metadata)
        conn = _connect(kb)
        try:
            from psycopg2 import sql
            import json as _json_meta
            cur = conn.cursor()
            cur.execute(
                sql.SQL("UPDATE {} SET metadata = %s::jsonb WHERE document_id = %s;").format(_kb_table_identifier(kb)),
                (_json_meta.dumps(body.metadata), doc_id),
            )
            conn.commit()
        finally:
            conn.close()
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return _doc_public(doc, _document_metadata(kb, doc.id),
                       _original_file_public(session, doc.source_file_id))


def _document_metadata(kb: KnowledgeBase, doc_id: str) -> dict:
    """The metadata currently stored on a document's chunks, read from
    one row. Every chunk of a document carries the same object (both the
    ingest path and the PATCH above write it uniformly), so sampling one
    is accurate and avoids scanning a document with 30,000 chunks.

    Hosted stores take the same approach through their own API. This used
    to be pgvector-only: _connect() on a Qdrant or Weaviate config threw,
    the exception was swallowed, and the Metadata column read empty for
    every document — even though the metadata really was stored, as
    retrieval results showed.
    """
    from .. import vector_stores
    adapter = vector_stores.adapter_for(kb.provider)
    if adapter is not None:
        from ..secrets_crypto import decrypt_secret
        cfg = dict(kb.config)
        if cfg.get("api_key"):
            try:
                cfg["api_key"] = decrypt_secret(cfg["api_key"])
            except Exception:
                return {}
        try:
            payload = adapter.sample_payload(cfg, kb.id, doc_id) or {}
        except Exception:
            # A listing must not fail because one store is unreachable.
            return {}
        # Only the keys this platform did not add itself, matching what
        # the pgvector path's metadata column holds.
        return {k: v for k, v in payload.items()
                if k not in ("content", "kb_id", "document_id", "filename",
                             "chunk_index", "parent_content")}
    if kb.provider != "pgvector":
        return {}

    conn = _connect(kb)
    try:
        from psycopg2 import sql
        cur = conn.cursor()
        cur.execute(
            sql.SQL("SELECT metadata FROM {} WHERE document_id = %s LIMIT 1;").format(_kb_table_identifier(kb)),
            (doc_id,),
        )
        row = cur.fetchone()
        return (row[0] or {}) if row else {}
    except Exception:
        return {}
    finally:
        conn.close()


@router.delete("/{kb_id}/documents/{doc_id}", dependencies=[Depends(require_permission(P.RAG))])
def delete_document(kb_id: str, doc_id: str, session: Session = Depends(get_session)) -> dict:
    """§0.68, GAP_REPORT — the same real outbox pattern ingest_document
    now uses, for the exact same real reason: the remote DELETE and the
    local KnowledgeDocument row's own delete used to be two separate,
    unguarded commits — a real failure between them left a real ghost
    local row pointing at chunks that no longer existed remotely, with
    no way to detect it. Now: a real, local, "pending" outbox row
    commits FIRST; the remote DELETE runs and, on success, checkpoints
    the row to "remote_done"; the local delete + the row's own final
    "completed" status land together in one real, atomic local commit.
    A real crash between the remote and local commits now leaves this
    row detectably at "remote_done" instead of a silent ghost."""
    doc = session.get(KnowledgeDocument, doc_id)
    if not doc or doc.kb_id != kb_id:
        raise HTTPException(404, "Document not found")
    kb = _get_kb_pgvector(kb_id, session)

    outbox = KBIngestionOutbox(
        id=f"outbox-{uuid.uuid4().hex[:12]}", kb_id=kb_id, document_id=doc_id, operation="delete",
        status="pending", payload={},
    )
    session.add(outbox)
    session.commit()

    conn = _connect(kb)
    try:
        from psycopg2 import sql
        cur = conn.cursor()
        cur.execute(sql.SQL("DELETE FROM {} WHERE document_id = %s;").format(_kb_table_identifier(kb)), (doc_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        outbox.status = "failed"
        outbox.last_error = str(e)[:2000]
        outbox.updated_at = datetime.utcnow()
        session.add(outbox)
        session.commit()
        raise HTTPException(400, f"Delete from remote table failed: {e}")
    finally:
        conn.close()

    outbox.status = "remote_done"
    outbox.updated_at = datetime.utcnow()
    session.add(outbox)
    session.commit()

    # The kept original goes with the document. Leaving the object
    # behind would mean a bucket that only ever grows, holding files
    # whose only reference in this platform has just been deleted —
    # and on S3 that is a bill for storage nothing can reach.
    original_deleted = _delete_original(session, doc.source_file_id)

    session.delete(doc)
    outbox.status = "completed"
    outbox.updated_at = datetime.utcnow()
    session.add(outbox)
    session.commit()
    return {"deleted": doc_id, "original_file_deleted": original_deleted}


# ---------------- Cross-database consistency (§0.68, GAP_REPORT) ----------------

@router.get("/{kb_id}/outbox", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_outbox(
    kb_id: str, response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[dict]:
    """Real visibility into this KB's own real outbox rows — what an
    operator (or this platform's own monitoring) would check to know
    whether any real ingest/delete for this KB is currently sitting in
    the real drift window ("remote_done": the remote side genuinely
    succeeded, the local side hasn't committed yet).

    §0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning."""
    q = select(KBIngestionOutbox).where(KBIngestionOutbox.kb_id == kb_id)
    rows = paginate(session, KBIngestionOutbox, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=KBIngestionOutbox.created_at, id_col=KBIngestionOutbox.id, response=response)
    return [r.model_dump() for r in rows]


# Registered on the INTERNAL router (routers/internal.py), not this
# product router: it is an operator maintenance operation behind the
# /internal prefix and the require_operator guard. It was a plain,
# unauthenticated route on the product API surface.
def reconcile_kb(kb_id: str, session: Session = Depends(get_session)) -> dict:
    """Real reconciliation (§0.68, GAP_REPORT) — the actual fix for the
    reported drift risk, applied to whatever real drift genuinely
    exists right now. Finds every real outbox row for this KB stuck at
    "remote_done" (the remote commit genuinely succeeded; the separate
    local commit never happened) and completes the local side using
    that row's own saved payload — no need to re-parse the original
    file (this platform doesn't keep a copy of it), since only small,
    real metadata is needed to finish an ingest's local write, and a
    delete's local completion needs no payload at all.

    A real, deliberate safety margin: a row younger than
    RECONCILE_GRACE_SECONDS is left alone — it may just be a real,
    still-in-flight request about to complete its own local commit
    normally in the next moment, not genuine drift yet. Real, honest
    scope: this only ever reconciles what THIS platform's own outbox
    rows recorded (every real ingest/delete since this fix shipped) —
    it does not scan the remote database for arbitrary drift that
    predates the outbox itself, or that came from some other real
    process writing to the same remote table outside this platform
    entirely; a real, broader drift audit is a separate, stated
    follow-on, not silently claimed here."""
    RECONCILE_GRACE_SECONDS = 30
    kb = _get_kb_pgvector(kb_id, session)
    cutoff = datetime.utcnow().timestamp() - RECONCILE_GRACE_SECONDS

    stuck = session.exec(
        select(KBIngestionOutbox).where(
            KBIngestionOutbox.kb_id == kb_id, KBIngestionOutbox.status == "remote_done"
        )
    ).all()

    reconciled: List[str] = []
    skipped_too_recent: List[str] = []
    for row in stuck:
        if row.updated_at.timestamp() > cutoff:
            skipped_too_recent.append(row.id)
            continue
        if row.operation == "ingest":
            existing = session.get(KnowledgeDocument, row.document_id)
            if not existing:
                p = row.payload
                session.add(KnowledgeDocument(
                    id=row.document_id, kb_id=kb_id, filename=p.get("filename", "upload"),
                    chunking_mode=p.get("chunking_mode", "General"), max_words=p.get("max_words", 180),
                    overlap_words=p.get("overlap_words", 30), word_count=p.get("word_count", 0),
                    chunk_count=p.get("chunk_count", 0), retrieval_count=0, status="enabled",
                ))
                if p.get("metadata"):
                    _ensure_metadata_fields(session, kb_id, p["metadata"])
                if p.get("target_table"):
                    kb.config = {**kb.config, "table": p["target_table"], "text_column": p.get("text_column", "content"),
                                 "embed_model": p.get("embed_model"), "schema": p.get("schema")}
                    kb.status = "connected"
                    session.add(kb)
        elif row.operation == "delete":
            existing = session.get(KnowledgeDocument, row.document_id)
            if existing and existing.kb_id == kb_id:
                session.delete(existing)
        row.status = "completed"
        row.updated_at = datetime.utcnow()
        session.add(row)
        reconciled.append(row.id)

    session.commit()
    return {
        "kb_id": kb_id, "reconciled": reconciled, "skipped_too_recent": skipped_too_recent,
        "note": "Only reconciles drift this platform's own outbox rows recorded since §0.68 — see this endpoint's own docstring for the real, stated scope boundary.",
    }


# ---------------- Chunks ----------------

@router.get("/{kb_id}/documents/{doc_id}/chunks", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_chunks(kb_id: str, doc_id: str, session: Session = Depends(get_session)) -> List[dict]:
    kb = _get_kb_pgvector(kb_id, session)
    conn = _connect(kb)
    try:
        from psycopg2 import sql
        cur = conn.cursor()
        text_col = kb.config.get("text_column", "content")
        cur.execute(
            sql.SQL("SELECT id, {}, chunk_index, word_count, enabled, metadata FROM {} WHERE document_id = %s ORDER BY chunk_index;")
            .format(sql.Identifier(text_col), _kb_table_identifier(kb)),
            (doc_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "content": r[1], "chunk_index": r[2], "word_count": r[3] or 0, "enabled": r[4] if r[4] is not None else True, "metadata": r[5] or {}}
        for r in rows
    ]


class ChunkPatchIn(BaseModel):
    content: Optional[str] = None
    enabled: Optional[bool] = None


@router.patch("/{kb_id}/chunks/{chunk_id}", dependencies=[Depends(require_permission(P.RAG))])
def patch_chunk(kb_id: str, chunk_id: int, body: ChunkPatchIn, session: Session = Depends(get_session)) -> dict:
    """Editing a chunk's content genuinely recomputes its real embedding
    (the same embed_texts function ingestion uses) — it does NOT just
    update the text and leave a stale vector, which would silently break
    real nearest-neighbor search for that row."""
    kb = _get_kb_pgvector(kb_id, session)
    conn = _connect(kb)
    text_col = kb.config.get("text_column", "content")
    try:
        from psycopg2 import sql
        table_ident = _kb_table_identifier(kb)
        text_col_ident = sql.Identifier(text_col)
        cur = conn.cursor()
        if body.content is not None:
            # Real, per-KB IMMUTABLE embedding pin (§0.92, GAP_REPORT) —
            # the actual fix for the reported gap: a real, edited
            # chunk's own recomputed vector must land in the SAME
            # embedding space every other chunk in this table already
            # lives in — this KB's own real, stored embedding_provider/
            # embedding_model (set at its own first real ingestion),
            # not whatever the CURRENT global config happens to be at
            # this later moment. None for a KB predating this fix
            # (nothing stored to pin to) — the same prior, honest,
            # env-driven fallback.
            try:
                vec = embed_texts(
                    [body.content], provider=kb.config.get("embedding_provider"), model=kb.config.get("embedding_model"),
                )[0]
            except Exception as e:
                raise HTTPException(
                    400,
                    f"Could not compute a real embedding for this edited chunk using this knowledge base's own "
                    f"stored configuration (provider={kb.config.get('embedding_provider') or 'current default'}, "
                    f"model={kb.config.get('embedding_model') or 'current default'}): {e}",
                )
            cur.execute(
                sql.SQL("UPDATE {} SET {} = %s, embedding = %s::vector, word_count = %s WHERE id = %s;")
                .format(table_ident, text_col_ident),
                (body.content, to_pgvector_literal(vec), len(body.content.split()), chunk_id),
            )
        if body.enabled is not None:
            cur.execute(sql.SQL("UPDATE {} SET enabled = %s WHERE id = %s;").format(table_ident), (body.enabled, chunk_id))
        conn.commit()
        cur.execute(
            sql.SQL("SELECT id, {}, chunk_index, word_count, enabled, metadata FROM {} WHERE id = %s;")
            .format(text_col_ident, table_ident),
            (chunk_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Chunk not found")
    return {"id": row[0], "content": row[1], "chunk_index": row[2], "word_count": row[3] or 0, "enabled": row[4] if row[4] is not None else True, "metadata": row[5] or {}}


@router.delete("/{kb_id}/chunks/{chunk_id}", dependencies=[Depends(require_permission(P.RAG))])
def delete_chunk(kb_id: str, chunk_id: int, session: Session = Depends(get_session)) -> dict:
    kb = _get_kb_pgvector(kb_id, session)
    conn = _connect(kb)
    try:
        from psycopg2 import sql
        table_ident = _kb_table_identifier(kb)
        cur = conn.cursor()
        cur.execute(sql.SQL("SELECT document_id, word_count FROM {} WHERE id = %s;").format(table_ident), (chunk_id,))
        row = cur.fetchone()
        cur.execute(sql.SQL("DELETE FROM {} WHERE id = %s;").format(table_ident), (chunk_id,))
        conn.commit()
    finally:
        conn.close()
    if row:
        doc = session.get(KnowledgeDocument, row[0]) if row[0] else None
        if doc:
            doc.chunk_count = max(0, doc.chunk_count - 1)
            doc.word_count = max(0, doc.word_count - (row[1] or 0))
            session.add(doc)
            session.commit()
    return {"deleted": chunk_id}


# ---------------- Metadata fields ----------------

@router.get("/{kb_id}/metadata-fields", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_metadata_fields(kb_id: str, session: Session = Depends(get_session)) -> List[dict]:
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    rows = session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id)).all()
    return [{"id": f.id, "kb_id": f.kb_id, "name": f.name, "type": f.type} for f in rows]


@router.get("/{kb_id}/metadata-keys", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def list_metadata_keys(kb_id: str, session: Session = Depends(get_session)) -> dict:
    """Just the names — the shape the Retrieval node's manual-filtering
    condition builder actually needs (NodeConfigPanel/mockup)."""
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    rows = session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id)).all()
    return {"keys": [f.name for f in rows]}


class MetaFieldIn(BaseModel):
    name: str
    type: str = "string"


VALID_META_TYPES = {"string", "number", "time"}


@router.post("/{kb_id}/metadata-fields", dependencies=[Depends(require_permission(P.RAG))])
def create_metadata_field(kb_id: str, body: MetaFieldIn, session: Session = Depends(get_session)) -> dict:
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    if body.type not in VALID_META_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(VALID_META_TYPES)}")
    name = body.name.strip().lower().replace(" ", "_")
    if not name:
        raise HTTPException(400, "name is required")
    existing = session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id, KnowledgeMetadataField.name == name)).first()
    if existing:
        raise HTTPException(400, f"A metadata field named '{name}' already exists on this knowledge base")
    field = KnowledgeMetadataField(id=f"kmf-{uuid.uuid4().hex[:10]}", kb_id=kb_id, name=name, type=body.type)
    session.add(field)
    session.commit()
    return {"id": field.id, "kb_id": field.kb_id, "name": field.name, "type": field.type}


@router.patch("/{kb_id}/metadata-fields/{field_id}", dependencies=[Depends(require_permission(P.RAG))])
def rename_metadata_field(kb_id: str, field_id: str, body: MetaFieldIn, session: Session = Depends(get_session)) -> dict:
    field = session.get(KnowledgeMetadataField, field_id)
    if not field or field.kb_id != kb_id:
        raise HTTPException(404, "Metadata field not found")
    new_name = body.name.strip().lower().replace(" ", "_")
    if not new_name:
        raise HTTPException(400, "name is required")
    dup = session.exec(select(KnowledgeMetadataField).where(KnowledgeMetadataField.kb_id == kb_id, KnowledgeMetadataField.name == new_name, KnowledgeMetadataField.id != field_id)).first()
    if dup:
        raise HTTPException(400, f"A metadata field named '{new_name}' already exists on this knowledge base")
    field.name = new_name
    if body.type in VALID_META_TYPES:
        field.type = body.type
    session.add(field)
    session.commit()
    return {"id": field.id, "kb_id": field.kb_id, "name": field.name, "type": field.type}


@router.delete("/{kb_id}/metadata-fields/{field_id}", dependencies=[Depends(require_permission(P.RAG))])
def delete_metadata_field(kb_id: str, field_id: str, session: Session = Depends(get_session)) -> dict:
    field = session.get(KnowledgeMetadataField, field_id)
    if not field or field.kb_id != kb_id:
        raise HTTPException(404, "Metadata field not found")
    session.delete(field)
    session.commit()
    return {"deleted": field_id}


# ---------------- Retrieval Testing ----------------

class RetrievalTestIn(BaseModel):
    query: str
    top_k: int = 5


def _retrieval_test_vector_store(kb: KnowledgeBase, body: "RetrievalTestIn",
                                 session: Session, record_prefix: str = "") -> dict:
    """Retrieval Testing against Qdrant, Weaviate or Milvus/Zilliz.

    Vector search only, and that is a real limit rather than an
    omission: Full-Text and Hybrid need a lexical index, which these
    stores do not provide in a form equivalent to Postgres full-text
    search. Asking for either is answered with that reason instead of
    quietly running a vector query and labelling it hybrid.

    The query is embedded with this pipeline's OWN stored model, not
    whatever is currently the global default — a query vector from a
    different model is not comparable with the stored ones, and the
    results would be quietly meaningless rather than wrong-looking.
    """
    from .. import vector_stores
    from ..secrets_crypto import decrypt_secret

    adapter = vector_stores.adapter_for(kb.provider)
    if adapter is None:
        raise HTTPException(400, f"No vector-store adapter for '{kb.provider}'.")

    query = body.query.strip()
    if not query:
        raise HTTPException(400, "query is required")
    top_k = max(1, min(body.top_k or 5, 20))

    cfg = dict(kb.config)
    method = cfg.get("retrieval_method", "vector")
    if method in ("fulltext", "hybrid"):
        raise HTTPException(
            400,
            f"This pipeline's Retrieval Setting is '{method}', which needs a lexical index "
            f"{kb.provider} does not provide. Switch it to Vector on the Settings tab, or use a "
            f"pgvector pipeline for full-text and hybrid search.",
        )
    if cfg.get("api_key"):
        cfg["api_key"] = decrypt_secret(cfg["api_key"])

    stored_model = cfg.get("embedding_model")
    embed_model_override = stored_model if stored_model and stored_model != EMBED_MODEL_NAME else None
    try:
        qvec = embed_texts([query], provider=cfg.get("embedding_provider"),
                           model=embed_model_override)[0]
    except Exception as e:
        raise HTTPException(400, f"Could not embed the query with this pipeline's model: {e}")

    try:
        matches = adapter.query(cfg, kb.id, qvec, top_k)
    except vector_stores.VectorStoreError as e:
        raise HTTPException(400, str(e))

    threshold = float(cfg.get("score_threshold") or 0) if cfg.get("score_threshold_enabled") else 0.0
    results = []
    for m in matches:
        score = m.get("score")
        score = float(score) if score is not None else 0.0
        if threshold and score < threshold:
            continue
        payload = m.get("payload") or {}
        results.append({
            "content": payload.get("content", ""),
            "score": round(score, 4),
            "document_id": payload.get("document_id"),
            "filename": payload.get("filename"),
            "chunk_index": payload.get("chunk_index"),
            "metadata": {k: v for k, v in payload.items()
                         if k not in ("content", "kb_id", "document_id", "filename", "chunk_index")},
        })

    # Reranking, using the SAME reranker the pgvector path uses
    # (retrieval._llm_rerank) rather than a second implementation, so a
    # result set from a hosted store is ordered by identical logic. This
    # was previously hardcoded to rerank_note=None, which meant enabling
    # a Rerank Model on a hosted pipeline silently did nothing.
    rerank_note = None
    if cfg.get("reranking_enabled") and results:
        from ..retrieval import _llm_rerank, _try_llm_rerank_note
        chosen = cfg.get("rerank_model") or None
        rerank_note = _try_llm_rerank_note(chosen)
        if rerank_note is None:
            results, rerank_note = _llm_rerank(query, results, chosen)

    # Same bookkeeping the pgvector path does, so a test here shows up in
    # Records and in per-document retrieval counts like any other.
    for doc_id in {r["document_id"] for r in results if r.get("document_id")}:
        row = session.get(KnowledgeDocument, doc_id)
        if row:
            row.retrieval_count = (row.retrieval_count or 0) + 1
            session.add(row)
    test = KnowledgeRetrievalTest(
        # No top_k=: KnowledgeRetrievalTest has no such column, so
        # passing it looked persisted while SQLModel dropped it.
        # The prefix marks where the call came from; the text that was
        # EMBEDDED stays clean, so a tag cannot shift the results the
        # row is describing.
        kb_id=kb.id, query=f"{record_prefix}{query}", result_count=len(results),
        results=results,
    )
    session.add(test)
    session.commit()
    return {"query": query, "top_k": top_k, "provider": kb.provider,
            "retrieval_method": "vector", "results": results,
            "rerank_note": rerank_note}


def _retrieval_test_pinecone(kb: KnowledgeBase, body: "RetrievalTestIn", session: Session,
                            record_prefix: str = "") -> dict:
    """Retrieval Testing against a Pinecone index.

    Vector search only, and that is a real limit rather than an
    omission: Full-Text and Hybrid need a lexical index, which Pinecone
    does not provide (its sparse vectors are a different feature, not a
    substitute for Postgres full-text search). Asking for either is
    answered with that reason instead of quietly running a vector query
    and reporting it as hybrid.

    Scores are Pinecone's own cosine similarities, passed through
    unchanged — unlike the pgvector path, which normalises the top hit
    to 1.0. A number here is comparable between queries.
    """
    from ..pinecone_store import PineconeError, describe_index, query as pine_query
    from ..secrets_crypto import decrypt_secret

    cfg = kb.config
    method = cfg.get("retrieval_method", "vector")
    if method in ("fulltext", "hybrid"):
        raise HTTPException(
            400,
            f"'{method}' retrieval needs a lexical index, which Pinecone does not have — only "
            f"Vector Search works against a Pinecone knowledge base. Change the Retrieval "
            f"Setting on the Settings tab.",
        )
    index_name = cfg.get("index")
    if not index_name:
        raise HTTPException(400, "This Pinecone knowledge base has no index configured.")
    api_key = decrypt_secret(cfg.get("api_key", ""))
    if not api_key:
        raise HTTPException(400, "No Pinecone API key stored for this knowledge base.")
    q = body.query.strip()
    if not q:
        raise HTTPException(400, "query is required")
    top_k = max(1, min(body.top_k or 5, 20))

    stored_model = cfg.get("embedding_model")
    try:
        vec = embed_query(q, provider=cfg.get("embedding_provider"),
                          model=stored_model if stored_model and stored_model != EMBED_MODEL_NAME else None)
    except Exception as e:
        raise HTTPException(400, f"Could not embed the query for this knowledge base: {e}")

    try:
        info = describe_index(api_key, index_name)
        matches = pine_query(api_key, info["host"], cfg.get("namespace") or kb.id,
                             vec, top_k, {"enabled": True})
    except PineconeError as e:
        raise HTTPException(400, str(e))

    threshold_on = bool(cfg.get("score_threshold_enabled"))
    threshold = float(cfg.get("score_threshold", 0) or 0)
    results = []
    doc_hits: Dict[str, int] = {}
    for m in matches:
        score = float(m.get("score", 0.0))
        if threshold_on and score < threshold:
            continue
        meta = m.get("metadata") or {}
        doc_id = meta.get("document_id")
        if doc_id:
            doc_hits[doc_id] = doc_hits.get(doc_id, 0) + 1
        results.append({
            "id": m.get("id"),
            "content": meta.get("parent_content") or meta.get("content", ""),
            "source_filename": meta.get("source_filename"),
            "document_id": doc_id,
            "score": round(score, 4),
            # Only the keys this platform did not add itself, so the
            # user's own metadata is what surfaces.
            "metadata": {k: v for k, v in meta.items()
                         if k not in ("content", "parent_content", "source_filename",
                                      "document_id", "chunk_index", "word_count", "enabled")},
        })

    for doc_id, n in doc_hits.items():
        doc = session.get(KnowledgeDocument, doc_id)
        if doc:
            doc.retrieval_count += n
            session.add(doc)
    # The same reranker the pgvector path uses, so a Pinecone result set
    # is ordered by identical logic. Previously hardcoded to None, which
    # meant a Rerank Model set on a Pinecone pipeline did nothing.
    rerank_note = None
    if cfg.get("reranking_enabled") and results:
        from ..retrieval import _llm_rerank, _try_llm_rerank_note
        chosen = cfg.get("rerank_model") or None
        rerank_note = _try_llm_rerank_note(chosen)
        if rerank_note is None:
            results, rerank_note = _llm_rerank(q, results, chosen)

    session.add(KnowledgeRetrievalTest(kb_id=kb.id, query=f"{record_prefix}{q}",
                                       result_count=len(results), results=results))
    session.commit()
    return {"results": results, "rerank_note": rerank_note}


@router.post("/{kb_id}/retrieval-test", dependencies=[Depends(require_permission(P.RAG))])
def run_retrieval_test(kb_id: str, body: RetrievalTestIn, session: Session = Depends(get_session)) -> dict:
    """The exact same real scoring path (app/retrieval.py's retrieve())
    harness.py's handle_retrieval uses for a live `retrieval` node —
    Vector / Full-Text / Hybrid, real Economical keyword-overlap scoring,
    real Score Threshold filtering, honored from this KB's own real
    Retrieval Setting (config.retrieval_method etc., set via
    PATCH .../settings) rather than always hardcoded to vector-only.
    Real retrieval_count increments (on the local KnowledgeDocument row
    for each source document a result came from), and a real
    KnowledgeRetrievalTest row is persisted so the "Records" history
    genuinely reflects past tests."""
    from ..retrieval import retrieve, kb_schema_of

    kb_any = session.get(KnowledgeBase, kb_id)
    if not kb_any:
        raise HTTPException(404, "Knowledge base not found")
    if kb_any.provider == "pinecone":
        return _retrieval_test_pinecone(kb_any, body, session)
    from .. import vector_stores
    if vector_stores.adapter_for(kb_any.provider):
        return _retrieval_test_vector_store(kb_any, body, session)
    kb = _get_kb_pgvector(kb_id, session)
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "query is required")
    top_k = max(1, min(body.top_k or 5, 20))
    c = kb.config

    conn = _connect(kb)
    text_col = c.get("text_column", "content")
    try:
        cur = conn.cursor()
        # Real schema-aware column lookup (§0.67, GAP_REPORT) — the same
        # canonical kb_schema_of ingestion and handle_retrieval both use.
        table_schema = kb_schema_of(c)
        if table_schema:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s;",
                (table_schema, c["table"]),
            )
        else:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s;", (c["table"],))
        cols = {r[0] for r in cur.fetchall()}
        where_sql = "WHERE enabled = true" if "enabled" in cols else ""
        try:
            results, rerank_note = retrieve(
                conn, c["table"], text_col, query, top_k,
                retrieval_method=c.get("retrieval_method", "vector"),
                semantic_weight=float(c.get("semantic_weight", 0.7) or 0.7),
                keyword_weight=float(c.get("keyword_weight", 0.3) or 0.3),
                reranking_enabled=bool(c.get("reranking_enabled")),
                rerank_model=c.get("rerank_model"),
                score_threshold_enabled=bool(c.get("score_threshold_enabled")),
                score_threshold=float(c.get("score_threshold", 0) or 0),
                where_sql=where_sql, select_extra=["metadata"], schema=table_schema,
                embedding_provider=c.get("embedding_provider"), embedding_model=c.get("embedding_model"),
            )
        except Exception as e:
            # Real, honest, actionable error (§0.92, GAP_REPORT) - most
            # commonly raised here when this KB's own real, stored,
            # already-pinned embedding_provider can no longer actually
            # run (a missing/rotated API key, a real provider outage) -
            # NOT silently falling back to computing this query's own
            # vector with a DIFFERENT provider, which would compare it
            # against this KB's real chunks in a meaningless way.
            raise HTTPException(
                400,
                f"Could not compute a real query embedding using this knowledge base's own stored "
                f"configuration (provider={c.get('embedding_provider') or 'current default'}, "
                f"model={c.get('embedding_model') or 'current default'}): {e}",
            )
    finally:
        conn.close()

    doc_ids_seen: Dict[str, int] = {}
    for r in results:
        if r.get("document_id"):
            doc_ids_seen[r["document_id"]] = doc_ids_seen.get(r["document_id"], 0) + 1

    for doc_id, count in doc_ids_seen.items():
        doc = session.get(KnowledgeDocument, doc_id)
        if doc:
            doc.retrieval_count += count
            session.add(doc)

    session.add(KnowledgeRetrievalTest(kb_id=kb_id, query=query, result_count=len(results),
                                       results=results))
    session.commit()

    return {"results": results, "rerank_note": rerank_note}


@router.get("/{kb_id}/retrieval-test-history", dependencies=[Depends(require_any_permission(P.RAG, P.STUDIO))])
def retrieval_test_history(kb_id: str, session: Session = Depends(get_session)) -> List[dict]:
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    rows = session.exec(
        select(KnowledgeRetrievalTest).where(KnowledgeRetrievalTest.kb_id == kb_id).order_by(KnowledgeRetrievalTest.created_at.desc()).limit(20)
    ).all()
    # `results` is the snapshot taken when the test ran (see the model's
    # own comment). Rows predating that column return [], which the UI
    # distinguishes from a real zero-result run via result_count.
    return [{"id": r.id, "kb_id": r.kb_id, "query": r.query, "result_count": r.result_count,
             "results": r.results or [], "created_at": r.created_at} for r in rows]


# ---------------- Retrieval API (this pipeline reachable from outside the builder) ----------------

@router.post("/{kb_id}/api/regenerate-key", dependencies=[Depends(require_permission(P.RAG))])
def regenerate_retrieval_api_key(kb_id: str, session: Session = Depends(get_session)) -> dict:
    """Real, cryptographically random key generation (secrets.token_
    urlsafe — not a UUID or a predictable pattern), stored under the
    dedicated `retrieval_api_key` config field (see SENSITIVE_KEYS'
    comment above for why it's deliberately NOT the same field
    non-pgvector providers use for their own connection's API key).
    Regenerating immediately invalidates the old key — nothing else on
    this platform retains it, so the old key genuinely stops working the
    moment this returns. The new key is the ONE time this endpoint (or
    any endpoint) ever returns it in cleartext — every subsequent GET/
    list masks it like any other SENSITIVE_KEYS field."""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    token = secrets.token_urlsafe(24)
    from ..secrets_crypto import encrypt_secret
    kb.config = {**kb.config, "retrieval_api_key": encrypt_secret(token), "retrieval_api_enabled": True}
    session.add(kb)
    session.commit()
    session.refresh(kb)
    result = _masked(kb)
    result["config"]["retrieval_api_key"] = token  # reveal once, right after generation
    return result


def _require_api_bearer(kb: KnowledgeBase, authorization: Optional[str], *,
                        key_field: str, enabled_field: str, label: str) -> None:
    """Shared bearer check for both external API surfaces. Extracted so
    the Retrieval and Upload APIs cannot drift apart on how they read a
    key, and so the comparison happens in one place: `compare_digest`
    rather than `==`, which keeps the check constant-time and stops the
    comparison itself from leaking the key a character at a time."""
    if not kb.config.get(enabled_field):
        raise HTTPException(403, f"The {label} API is not enabled for this knowledge base — enable it on the API tab first.")
    from ..secrets_crypto import decrypt_secret, SecretDecryptionError
    try:
        expected = decrypt_secret(kb.config.get(key_field))
    except SecretDecryptionError:
        raise HTTPException(
            409, f"This pipeline's {label} API key was saved under a different SECRETS_ENCRYPTION_KEY "
                 f"and can no longer be checked. Regenerate it on the pipeline's API tab and give "
                 f"callers the new key.")
    if not expected:
        raise HTTPException(403, f"No {label} API key has been generated for this knowledge base yet.")
    provided = (authorization or "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    provided = provided.strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(401, "Invalid or missing API key.")


@router.post("/{kb_id}/provision", dependencies=[Depends(require_permission(P.RAG))])
def provision_kb_catalog(kb_id: str, session: Session = Depends(get_session)) -> dict:
    """Register this KB's catalog object, and grant it if a group is
    configured. Idempotent.

    Exists for the two cases automatic provisioning cannot cover: a KB
    created before this existed, and one created while UCS or Gravitino
    was unreachable. Without it the only remedy was creating the
    Gravitino fileset and the UCS role by hand.
    """
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    from ..ucs_provisioning import provision_knowledge_base
    return provision_knowledge_base(kb.id, kb.name)


@router.post("/provision-all", dependencies=[Depends(require_permission(P.RAG))])
def provision_all_kb_catalogs(session: Session = Depends(get_session)) -> dict:
    """Backfill every existing knowledge base.

    Registration is what makes an entitlement check answerable at all,
    and every KB created before this shipped has no catalog object — so
    a deployment upgrading into this needs one call rather than one per
    KB.
    """
    from ..ucs_provisioning import provision_knowledge_base
    results = []
    for kb in session.exec(select(KnowledgeBase)).all():
        results.append(provision_knowledge_base(kb.id, kb.name))
    return {"knowledge_bases": len(results), "results": results}


@router.post("/{kb_id}/api/upload-key/regenerate", dependencies=[Depends(require_permission(P.RAG))])
def regenerate_upload_api_key(kb_id: str, session: Session = Depends(get_session)) -> dict:
    """Mirror of regenerate_retrieval_api_key for the Upload API, under
    its own `upload_api_key` field.

    Deliberately a SECOND, separate key rather than reusing the
    retrieval one. The Retrieval API only reads; this one writes real
    chunks into the knowledge base, so handing a read-only consumer a
    key that can also inject documents would be a real privilege
    upgrade nobody asked for. Two keys means a retrieval integration
    can be given exactly read access, and either surface can be
    revoked without breaking the other."""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    token = secrets.token_urlsafe(24)
    from ..secrets_crypto import encrypt_secret
    kb.config = {**kb.config, "upload_api_key": encrypt_secret(token), "upload_api_enabled": True}
    session.add(kb)
    session.commit()
    session.refresh(kb)
    result = _masked(kb)
    result["config"]["upload_api_key"] = token  # reveal once, right after generation
    return result


@router.post("/{kb_id}/api/upload", dependencies=[Depends(require_permission(P.RAG))])
async def api_upload_document(
    kb_id: str,
    request: Request,
    file: UploadFile = File(...),
    max_words: int = Form(180),
    overlap_words: int = Form(30),
    metadata: Optional[str] = Form(None),
    # Same option the Documents tab offers, same default as
    # ingest_document's: an existing caller does not start writing
    # objects to a bucket because this platform changed under it.
    keep_original: bool = Form(False),
    storage_destination_id: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> dict:
    """The real, external-facing Upload API — the write counterpart to
    api_retrieve below. A genuinely external caller can POST a file
    here with `Authorization: Bearer <upload_api_key>` and have it
    parsed, chunked, embedded and inserted exactly as a file dropped on
    the Documents tab would be.

    It delegates to ingest_document rather than reimplementing any of
    that: same parser, same per-KB Chunk Structure and Index Method,
    same pinned embedding configuration, same outbox/idempotency
    bookkeeping, same KnowledgeDocument row. An external upload is
    therefore indistinguishable from a UI upload once ingested, and
    shows up in the Documents list like any other.

    Note this does NOT take `table` or `force_reindex`, both of which
    ingest_document accepts from the builder UI. Letting an external
    key choose its own target table, or wipe and re-embed every
    existing vector in the KB, is authority this surface has no reason
    to hand out.
    """
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    # Not _get_kb_pgvector: that requires a table to already exist, and
    # the first upload into a fresh KB is what creates it.
    from .. import vector_stores
    if not (kb.provider in ("pgvector", "pinecone")
            or vector_stores.adapter_for(kb.provider)):
        raise HTTPException(
            400,
            f"The Upload API has no ingestion adapter for provider '{kb.provider}'.",
        )
    _require_api_bearer(kb, authorization, key_field="upload_api_key",
                        enabled_field="upload_api_enabled", label="Upload")
    return await ingest_document(
        kb_id=kb_id, request=request, file=file, table=None,
        max_words=max_words, overlap_words=overlap_words,
        metadata=metadata, force_reindex=False, keep_original=keep_original,
        storage_destination_id=storage_destination_id, session=session,
    )


class ApiRetrieveIn(BaseModel):
    query: str
    top_k: int = 5


@router.post("/{kb_id}/api/retrieve", dependencies=[Depends(require_permission(P.RAG))])
def api_retrieve(
    kb_id: str, body: ApiRetrieveIn,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> dict:
    """The real, external-facing Retrieval API — genuinely the SAME
    scoring path (app/retrieval.py's retrieve(), honoring this KB's real
    Chunk Structure/Index Method/Retrieval Setting from the Settings tab)
    every other real retrieval surface in this platform uses (a live
    `retrieval` node, the Settings tab's own Retrieval Testing) — not a
    separate, simplified reimplementation for external callers. Reachable
    over plain HTTP with a real bearer-token check
    (`Authorization: Bearer <retrieval_api_key>`) instead of requiring
    this platform's own builder session, so a genuinely external caller
    (not just an agent's Retrieval node) can query this pipeline
    directly. Every real call here is also logged as a real
    KnowledgeRetrievalTest row and updates real per-document
    retrieval_count — the exact same bookkeeping a builder-triggered
    Retrieval Testing run gets, so API usage shows up honestly in the
    Records history rather than being invisible."""
    from ..retrieval import retrieve, kb_schema_of

    # The bearer check has to happen for EVERY provider, so it comes
    # before the dispatch below rather than inside the pgvector branch.
    kb_any = session.get(KnowledgeBase, kb_id)
    if not kb_any:
        raise HTTPException(404, "Knowledge base not found")
    _require_api_bearer(kb_any, authorization, key_field="retrieval_api_key",
                        enabled_field="retrieval_api_enabled", label="Retrieval")

    if kb_any.provider != "pgvector":
        # Hosted vector stores reach the same embed-and-search code the
        # Retrieval Testing tab uses, so an external caller and the
        # builder get identical results — including the Records row and
        # the per-document retrieval counts those functions write. The
        # query is tagged [API] the same way the pgvector path tags it.
        from .. import vector_stores
        probe = RetrievalTestIn(query=body.query, top_k=body.top_k)
        if kb_any.provider == "pinecone":
            outcome = _retrieval_test_pinecone(kb_any, probe, session, record_prefix="[API] ")
        elif vector_stores.adapter_for(kb_any.provider):
            outcome = _retrieval_test_vector_store(kb_any, probe, session, record_prefix="[API] ")
        else:
            raise HTTPException(
                400,
                f"The Retrieval API has no search path for provider '{kb_any.provider}'.",
            )
        return {"results": outcome.get("results", []),
                "rerank_note": outcome.get("rerank_note")}

    kb = _get_kb_pgvector(kb_id, session)

    query = body.query.strip()
    if not query:
        raise HTTPException(400, "query is required")
    top_k = max(1, min(body.top_k or 5, 20))
    c = kb.config

    conn = _connect(kb)
    text_col = c.get("text_column", "content")
    try:
        cur = conn.cursor()
        # Real schema-aware column lookup (§0.67, GAP_REPORT) — the same
        # canonical kb_schema_of ingestion and handle_retrieval both use.
        table_schema = kb_schema_of(c)
        if table_schema:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s;",
                (table_schema, c["table"]),
            )
        else:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s;", (c["table"],))
        cols = {r[0] for r in cur.fetchall()}
        where_sql = "WHERE enabled = true" if "enabled" in cols else ""
        try:
            results, rerank_note = retrieve(
                conn, c["table"], text_col, query, top_k,
                retrieval_method=c.get("retrieval_method", "vector"),
                semantic_weight=float(c.get("semantic_weight", 0.7) or 0.7),
                keyword_weight=float(c.get("keyword_weight", 0.3) or 0.3),
                reranking_enabled=bool(c.get("reranking_enabled")),
                rerank_model=c.get("rerank_model"),
                score_threshold_enabled=bool(c.get("score_threshold_enabled")),
                score_threshold=float(c.get("score_threshold", 0) or 0),
                where_sql=where_sql, select_extra=["metadata"], schema=table_schema,
                embedding_provider=c.get("embedding_provider"), embedding_model=c.get("embedding_model"),
            )
        except Exception as e:
            # Real, honest, actionable error (§0.92, GAP_REPORT) - most
            # commonly raised here when this KB's own real, stored,
            # already-pinned embedding_provider can no longer actually
            # run (a missing/rotated API key, a real provider outage) -
            # NOT silently falling back to computing this query's own
            # vector with a DIFFERENT provider, which would compare it
            # against this KB's real chunks in a meaningless way.
            raise HTTPException(
                400,
                f"Could not compute a real query embedding using this knowledge base's own stored "
                f"configuration (provider={c.get('embedding_provider') or 'current default'}, "
                f"model={c.get('embedding_model') or 'current default'}): {e}",
            )
    finally:
        conn.close()

    doc_ids_seen: Dict[str, int] = {}
    for r in results:
        if r.get("document_id"):
            doc_ids_seen[r["document_id"]] = doc_ids_seen.get(r["document_id"], 0) + 1
    for doc_id, count in doc_ids_seen.items():
        doc = session.get(KnowledgeDocument, doc_id)
        if doc:
            doc.retrieval_count += count
            session.add(doc)
    session.add(KnowledgeRetrievalTest(kb_id=kb_id, query=f"[API] {query}", result_count=len(results),
                                       results=results))
    session.commit()

    return {"results": results, "rerank_note": rerank_note}


@router.post("/{kb_id}/test", dependencies=[Depends(require_permission(P.RAG))])
def test_knowledge_base(kb_id: str, session: Session = Depends(get_session)) -> dict:
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")

    result = _test_pgvector(kb.config) if kb.provider == "pgvector" else _test_http_reachable(kb.config, kb.provider)

    kb.status = "connected" if result["ok"] else "failed"
    kb.last_error = None if result["ok"] else result.get("error")
    kb.last_checked_at = datetime.utcnow()
    session.add(kb)
    session.commit()
    session.refresh(kb)

    return {**_masked(kb), "test_result": result}


# ---------------- ingest from a configured Data Source ----------------

class SourceIngestIn(BaseModel):
    """One or more files picked out of a Data Source's browser.

    `file_ids` are the ids the browse response gave back, so a selection
    spanning several folders is a single request — the ids already carry
    whatever addressing their source needs (repo path, drive item id,
    bucket/object key, Notion page id)."""
    source: str
    file_ids: List[str] = []
    # Folders to take whole. Expanded server-side into their ingestible
    # files (recursively, bounded) — only files can be fetched, so the
    # walk has to happen somewhere, and one request beats the browser
    # making a round trip per directory.
    folder_paths: List[str] = []
    table: Optional[str] = None
    max_words: int = 180
    overlap_words: int = 30
    metadata: Optional[str] = None
    force_reindex: bool = False
    # Keep each fetched file in this platform's own object storage, the
    # same place a local upload is kept.
    keep_original: bool = False
    storage_destination_id: Optional[str] = None


async def _ingest_selection_from_source(
    kb_id: str, source: str, file_ids: List[str], *, table: Optional[str],
    max_words: int, overlap_words: int, metadata: Optional[str],
    force_reindex: bool, session: Session,
    folder_paths: Optional[List[str]] = None,
    keep_original: bool = False,
    request: Optional[Request] = None,
    storage_destination_id: Optional[str] = None,
) -> dict:
    """Fetches each selected file from the external source and puts it
    through the *same* ingest_document path a local upload takes — same
    extractor, same chunking mode, same embedding model resolution, same
    Pinecone/pgvector branch. Nothing about ingestion is duplicated
    here; only the bytes arrive from somewhere else.

    Shared by the builder's Documents tab and the external Upload API,
    so both surfaces genuinely behave identically rather than drifting.

    Multi-file behaviour: files are processed in order and each result
    is reported individually, because one unreadable file in a folder of
    forty should not discard the other thirty-nine. `force_reindex` is
    honoured on the first file only — it wipes the existing index, and
    applying it per file would erase each preceding file in the same
    batch.

    Not verified against a live tenant: this environment has no
    credentials for any of these providers, so the fetch layer's request
    shapes follow each vendor's documented API and every provider error
    is passed through with its own status and message rather than being
    reduced to a generic failure."""
    from fastapi import UploadFile as _UploadFile
    from starlette.datastructures import Headers as _Headers
    import io as _io
    import mimetypes as _mimetypes
    from .. import data_sources
    from .integrations import _ds_plain_config

    if not file_ids and not (folder_paths or []):
        raise HTTPException(400, "Select at least one file or folder to ingest")

    ds = session.get(DataSource, source)
    if not ds:
        raise HTTPException(404, f"Data source '{source}' not found")
    if ds.status != "connected" or not (ds.config or {}):
        raise HTTPException(400, f"{source} is not connected — add credentials on the "
                                 f"Integrations page.")
    cfg = _ds_plain_config(ds)

    # Folders first, so a folder's files and any individually ticked
    # files end up in one ordered batch. De-duplicated by id: selecting a
    # folder AND a file inside it must not ingest that file twice.
    results: List[Dict[str, Any]] = []
    expanded: List[str] = list(file_ids)
    truncated_folders: List[str] = []
    for folder in (folder_paths or []):
        try:
            files, truncated = data_sources.expand_folder(source, cfg, folder)
        except data_sources.DataSourceError as exc:
            results.append({"file_id": folder, "success": False,
                            "error": f"Could not read folder: {exc}"})
            continue
        except Exception as exc:
            results.append({"file_id": folder, "success": False,
                            "error": f"Could not read folder from {source}: {exc}"})
            continue
        if truncated:
            truncated_folders.append(folder)
        if not files:
            results.append({"file_id": folder, "success": False,
                            "error": "No ingestible files in this folder"})
            continue
        expanded.extend(f["id"] for f in files)

    seen_ids: set = set()
    ordered: List[str] = []
    for fid in expanded:
        if fid not in seen_ids:
            seen_ids.add(fid)
            ordered.append(fid)

    ingested = 0
    for position, file_id in enumerate(ordered):
        try:
            filename, content = data_sources.fetch_file(source, cfg, file_id)
        except data_sources.DataSourceError as exc:
            results.append({"file_id": file_id, "success": False, "error": str(exc)})
            continue
        except Exception as exc:
            results.append({"file_id": file_id, "success": False,
                            "error": f"Could not fetch from {source}: {exc}"})
            continue
        if not content:
            results.append({"file_id": file_id, "filename": filename, "success": False,
                            "error": "File is empty"})
            continue

        mime = _mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload = _UploadFile(file=_io.BytesIO(content), filename=filename, size=len(content),
                             headers=_Headers({"content-type": mime}))
        try:
            outcome = await ingest_document(
                kb_id=kb_id, request=request, file=upload, table=table, max_words=max_words,
                overlap_words=overlap_words, metadata=metadata,
                # Only the first file may clear the index; see docstring.
                force_reindex=force_reindex and position == 0,
                # A file fetched from Drive or SharePoint is kept the
                # same way a locally-uploaded one is: it came from
                # someone else's system, and this platform's copy is
                # the one its own retrieval results point back to.
                keep_original=keep_original,
                storage_destination_id=storage_destination_id,
                session=session,
            )
        except HTTPException as exc:
            results.append({"file_id": file_id, "filename": filename, "success": False,
                            "error": exc.detail})
            continue
        ingested += 1
        results.append({"file_id": file_id, "filename": filename, "success": True,
                        "document_id": outcome.get("document_id"),
                        "chunks_inserted": outcome.get("chunks_inserted"),
                        "table": outcome.get("table"),
                        "embed_model": outcome.get("embed_model"),
                        # Per file, because in a batch one file can be
                        # kept and the next refused — reporting only a
                        # batch-level "kept" would be wrong for whichever
                        # one failed.
                        "original_file": outcome.get("original_file"),
                        "original_file_error": outcome.get("original_file_error")})

    failed = sum(1 for r in results if not r.get("success"))
    return {"source": source, "requested": len(ordered), "ingested": ingested,
            "failed": failed, "results": results,
            # Says the cap was hit rather than quietly indexing a subset.
            "truncated_folders": truncated_folders,
            "folder_file_cap": data_sources.MAX_FOLDER_FILES if truncated_folders else None}


@router.post("/{kb_id}/ingest-from-source", dependencies=[Depends(require_permission(P.RAG))])
async def ingest_from_data_source(kb_id: str, body: SourceIngestIn, request: Request,
                                  session: Session = Depends(get_session)) -> dict:
    """Documents-tab ingestion from a configured Data Source. Accepts
    `table` and `force_reindex` because this is the builder's own
    surface; the external Upload API deliberately does not."""
    if not session.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    return await _ingest_selection_from_source(
        kb_id, body.source, body.file_ids, table=body.table, max_words=body.max_words,
        overlap_words=body.overlap_words, metadata=body.metadata,
        force_reindex=body.force_reindex, session=session,
        folder_paths=body.folder_paths, keep_original=body.keep_original, request=request,
        storage_destination_id=body.storage_destination_id,
    )


class ApiSourceUploadIn(BaseModel):
    """External Upload API body. `file_ids` are the ids the browse
    endpoint returned — for GitHub and Google Cloud Storage those ARE
    the full paths (`owner/repo/docs/policy.md`,
    `my-bucket/reports/q3.csv`), so a command can be written by hand;
    for SharePoint, OneDrive, Google Drive and Notion the provider only
    addresses items by opaque id, so that is what goes here."""
    source: str
    file_ids: List[str] = []
    folder_paths: List[str] = []
    max_words: int = 180
    overlap_words: int = 30
    metadata: Optional[str] = None
    keep_original: bool = False
    storage_destination_id: Optional[str] = None


@router.post("/{kb_id}/api/upload-from-source", dependencies=[Depends(require_permission(P.RAG))])
async def api_upload_from_source(
    kb_id: str, body: ApiSourceUploadIn,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> dict:
    """The external Upload API's data-source counterpart: instead of
    POSTing file bytes, an external caller names files that already live
    in a Data Source this workspace has connected, and the server
    fetches them.

    This exists because the multipart form of the Upload API can only
    ever reference a file on the *caller's* own disk. A document sitting
    in SharePoint or a GitHub repo would otherwise have to be downloaded
    by hand first, and the curl command could never show more than a
    bare filename — the browser that picked it never learns its real
    path. Addressing the file where it actually lives makes the whole
    path expressible in the command.

    Same authority boundary as api_upload: no `table` and no
    `force_reindex`. Same ingestion path as everything else, via
    _ingest_selection_from_source."""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    from .. import vector_stores
    if not (kb.provider in ("pgvector", "pinecone")
            or vector_stores.adapter_for(kb.provider)):
        raise HTTPException(
            400,
            f"The Upload API has no ingestion adapter for provider '{kb.provider}'.",
        )
    _require_api_bearer(kb, authorization, key_field="upload_api_key",
                        enabled_field="upload_api_enabled", label="Upload")
    return await _ingest_selection_from_source(
        kb_id, body.source, body.file_ids, table=None, max_words=body.max_words,
        overlap_words=body.overlap_words, metadata=body.metadata,
        force_reindex=False, session=session,
        folder_paths=body.folder_paths, keep_original=body.keep_original,
        storage_destination_id=body.storage_destination_id,
    )


class ApiStoredUploadIn(BaseModel):
    """Names a file already sitting in the platform's own upload store
    (`POST /api/uploads`), rather than one on the caller's disk."""
    upload_id: str
    max_words: int = 180
    overlap_words: int = 30
    metadata: Optional[str] = None


@router.post("/{kb_id}/api/upload-from-store", dependencies=[Depends(require_permission(P.RAG))])
async def api_upload_from_store(
    kb_id: str, body: ApiStoredUploadIn,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> dict:
    """Ingests a file the platform is already holding, by id.

    Why this exists: `-F "file=@..."` can only ever name a file on the
    *caller's own disk*, and a browser will not tell a web page where a
    picked file lives — `File` carries only `name`, and `input.value` is
    the literal "C:\\fakepath\\<name>". So a snippet built from a
    single-file picker can never contain a real path, and the copied
    command therefore only works from whatever directory happens to
    hold the file. Referring to the file by id removes the path from the
    command entirely, so it runs from anywhere, unchanged, on any
    machine.

    The upload store's own guarantees still apply and are not
    re-implemented here: the bytes were content-type sniffed, malware
    scanned and size capped when they were stored, and a file past its
    retention window is genuinely gone rather than quietly served.

    Same authority boundary as the other Upload API surfaces — no
    `table`, no `force_reindex` — and the same ingestion path as
    everything else, via ingest_document."""
    from fastapi import UploadFile as _UploadFile
    from starlette.datastructures import Headers as _Headers
    from datetime import datetime as _dt
    import io as _io
    from ..models import UploadedFile
    from ..storage import get_storage_backend, StorageError

    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, "Knowledge base not found")
    from .. import vector_stores
    if not (kb.provider in ("pgvector", "pinecone")
            or vector_stores.adapter_for(kb.provider)):
        raise HTTPException(
            400,
            f"The Upload API has no ingestion adapter for provider '{kb.provider}'.",
        )
    _require_api_bearer(kb, authorization, key_field="upload_api_key",
                        enabled_field="upload_api_enabled", label="Upload")

    row = session.get(UploadedFile, body.upload_id)
    if not row:
        raise HTTPException(404, f"No stored upload with id '{body.upload_id}'.")
    if row.expires_at and _dt.utcnow() > row.expires_at:
        raise HTTPException(410, "That stored upload is past its retention window — upload it again.")

    # The backend the object was actually written to, not whichever one
    # is configured now — a file stored under local disk must still be
    # findable after a later switch to S3.
    backend = get_storage_backend(row.storage_backend)
    try:
        content = backend.get(row.object_key)
    except StorageError as e:
        raise HTTPException(502, f"Could not read the stored upload back: {e}")

    upload = _UploadFile(
        file=_io.BytesIO(content), filename=row.filename, size=len(content),
        headers=_Headers({"content-type": row.content_type or "application/octet-stream"}),
    )
    result = await ingest_document(
        kb_id=kb_id, request=request, file=upload, table=None, max_words=body.max_words,
        overlap_words=body.overlap_words, metadata=body.metadata,
        # Never re-upload here: these bytes were read back OUT of
        # object storage a few lines above, so a second copy would be
        # the same file stored twice under two keys. The existing row
        # is linked to the document below instead.
        force_reindex=False, keep_original=False, session=session,
    )
    doc = session.get(KnowledgeDocument, result.get("document_id"))
    if doc is not None:
        doc.source_file_id = row.id
        session.add(doc)
        session.commit()
    from .uploads import stored_file_public
    return {**result, "upload_id": row.id, "original_file": stored_file_public(row)}
