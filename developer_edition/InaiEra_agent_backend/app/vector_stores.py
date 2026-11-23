"""Qdrant, Weaviate and Milvus/Zilliz over their own REST APIs.

Written as plain httpx calls rather than pulling in qdrant-client,
weaviate-client and pymilvus, for the same reason pinecone_store.py
does: three more SDKs is three more dependency trees, each with its own
transitive pins, to write documents into a collection and read the
nearest vectors back. The REST surfaces used here are small and stable.

Every adapter exposes the same four operations, so the ingest path does
not branch per provider beyond picking a module:

    ensure_collection(cfg, dim)      create it if absent, verify the
                                     dimension if present
    upsert(cfg, points)              write vectors + payload
    query(cfg, vector, top_k)        nearest neighbours with payload
    delete_by_document(cfg, doc_id)  remove one document's chunks

A note on what these CANNOT do, stated because the pgvector path can:
full-text and hybrid retrieval, and the "economical" keyword-only index
method, all rely on Postgres's own text search. A hosted vector index
holds vectors and payload, so retrieval here is vector similarity only.
"""
from typing import Any, Dict, List, Optional, Tuple
import uuid

import httpx

TIMEOUT = 60


class VectorStoreError(RuntimeError):
    """A provider refused a request, with its own status and body."""


def _check(r: httpx.Response, what: str) -> Any:
    if r.status_code >= 400:
        raise VectorStoreError(f"{what} failed ({r.status_code}): {r.text[:300]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


CONSOLE_HOSTS = {
    "cloud.qdrant.io": ("Qdrant", "https://<cluster-id>.<region>.aws.cloud.qdrant.io:6333",
                        "Qdrant Cloud > Clusters > your cluster > the endpoint shown there"),
    "console.weaviate.cloud": ("Weaviate", "https://<cluster>.<region>.gcp.weaviate.cloud",
                               "Weaviate Cloud > your cluster > REST Endpoint"),
    "cloud.zilliz.com": ("Zilliz/Milvus", "https://in03-<id>.api.<region>.zillizcloud.com",
                         "Zilliz Cloud > Clusters > your cluster > Public Endpoint"),
    "app.pinecone.io": ("Pinecone", "https://<index>-<project>.svc.<env>.pinecone.io",
                        "Pinecone > Indexes > your index > Host"),
}


def check_endpoint_url(raw: str, provider: str = "") -> None:
    """Raise if this is a web console URL rather than an API endpoint.

    Separate from _base_url so the connection editor can apply the SAME
    rule when the value is saved. Discovering it at first use means the
    message arrives per-file in the middle of an ingest, nowhere near
    the field that caused it.
    """
    from urllib.parse import urlparse
    url = (raw or "").strip().rstrip("/")
    if not url:
        return
    probe = url if url.startswith(("http://", "https://")) else f"https://{url}"
    hostname = (urlparse(probe).hostname or "").lower()
    entry = CONSOLE_HOSTS.get(hostname)
    if not entry:
        return
    label, shape, where = entry
    raise VectorStoreError(
        f"'{url}' is the {label} web console, not its API endpoint — requests to it return a "
        f"login page, not data. Find the endpoint at: {where}. It looks like {shape}."
    )


def _base_url(cfg: Dict[str, Any], provider: str) -> str:
    """The data-plane endpoint, rejecting a web-console URL.

    Every one of these providers has a console on a different hostname
    from the API, and pasting the console URL is the obvious mistake —
    it is the address you are looking at when you go to copy something.
    Caught here with the real endpoint's shape named, because otherwise
    the failure is an HTML login page parsed as JSON, hundreds of lines
    from anything that mentions a URL.
    """
    url = (cfg.get("url") or "").strip().rstrip("/")
    if not url:
        raise VectorStoreError(f"This {provider} knowledge base has no URL configured.")
    check_endpoint_url(url, provider)
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _collection(cfg: Dict[str, Any], kb_id: str) -> str:
    """Which collection/class/index to write into.

    Falls back to the KB id so a pipeline works before anyone names one,
    rather than refusing; `index` is the field the Integrations form
    already collects for these providers.
    """
    return (cfg.get("index") or cfg.get("collection") or cfg.get("class_name")
            or kb_id.replace("-", "_"))


# ------------------------------------------------------------------ Qdrant
class Qdrant:
    name = "qdrant"

    @staticmethod
    def _h(cfg: Dict[str, Any]) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            h["api-key"] = cfg["api_key"]
        return h

    @classmethod
    def ensure_collection(cls, cfg: Dict[str, Any], kb_id: str, dim: int) -> str:
        base, coll, h = _base_url(cfg, "Qdrant"), _collection(cfg, kb_id), cls._h(cfg)
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(f"{base}/collections/{coll}", headers=h)
            if r.status_code == 200:
                existing = (((r.json().get("result") or {}).get("config") or {})
                            .get("params", {}).get("vectors", {}))
                size = existing.get("size") if isinstance(existing, dict) else None
                if size and int(size) != int(dim):
                    raise VectorStoreError(
                        f"Collection '{coll}' stores {size}-dimensional vectors but this "
                        f"pipeline's embedding model produces {dim}. A collection's vector size "
                        f"is fixed at creation — use a different collection, or an embedding "
                        f"model of matching width."
                    )
                # Also for a collection that already exists: anything
                # created before these indexes were added would refuse
                # every filtered delete until they are.
                cls._ensure_payload_indexes(c, base, coll, h)
                return coll
            _check(c.put(f"{base}/collections/{coll}", headers=h,
                         json={"vectors": {"size": int(dim), "distance": "Cosine"}}),
                   f"Qdrant create collection '{coll}'")
            cls._ensure_payload_indexes(c, base, coll, h)
        return coll

    @staticmethod
    def _ensure_payload_indexes(c: httpx.Client, base: str, coll: str,
                                h: Dict[str, str]) -> None:
        """Keyword indexes on the two payload fields this platform filters
        by. Without them a filtered delete is REFUSED, not merely slow:
        Qdrant Cloud runs collections in strict mode, which answers
        `400 Index required but not found for "kb_id" of one of the
        following types: [keyword]`. Deleting a document, and the
        force-reindex that deletes everything this platform wrote, both
        filter on exactly these fields — so without this, both fail on a
        real hosted Qdrant.

        Idempotent and non-fatal: an index that already exists is a
        no-op, and a cluster that does not require one must not have
        ingestion fail because index creation was unavailable.
        """
        for field in ("kb_id", "document_id"):
            try:
                c.put(f"{base}/collections/{coll}/index?wait=true", headers=h,
                      json={"field_name": field, "field_schema": "keyword"})
            except httpx.HTTPError:
                pass

    @classmethod
    def upsert(cls, cfg: Dict[str, Any], kb_id: str, points: List[Dict[str, Any]]) -> int:
        base, coll, h = _base_url(cfg, "Qdrant"), _collection(cfg, kb_id), cls._h(cfg)
        body = {"points": [
            # A UUID per chunk: Qdrant ids must be an unsigned integer or
            # a UUID, so a "doc-x#3" string cannot be used directly.
            {"id": str(uuid.uuid4()), "vector": p["vector"], "payload": p["payload"]}
            for p in points
        ]}
        with httpx.Client(timeout=TIMEOUT) as c:
            _check(c.put(f"{base}/collections/{coll}/points?wait=true", headers=h, json=body),
                   f"Qdrant upsert into '{coll}'")
        return len(points)

    @classmethod
    def query(cls, cfg: Dict[str, Any], kb_id: str, vector: List[float],
              top_k: int) -> List[Dict[str, Any]]:
        base, coll, h = _base_url(cfg, "Qdrant"), _collection(cfg, kb_id), cls._h(cfg)
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _check(c.post(f"{base}/collections/{coll}/points/search", headers=h,
                                 json={"vector": vector, "limit": top_k,
                                       "with_payload": True}),
                          f"Qdrant search '{coll}'")
        return [{"score": m.get("score"), "payload": m.get("payload") or {}}
                for m in (data.get("result") or [])]

    @classmethod
    def delete_by_document(cls, cfg: Dict[str, Any], kb_id: str, document_id: Optional[str]) -> None:
        base, coll, h = _base_url(cfg, "Qdrant"), _collection(cfg, kb_id), cls._h(cfg)
        # No document_id means "everything this platform wrote", which is
        # what a force-reindex needs. Scoped by kb_id either way so a
        # collection shared with other data is never mass-deleted.
        must: List[Dict[str, Any]] = [{"key": "kb_id", "match": {"value": kb_id}}]
        if document_id:
            must.append({"key": "document_id", "match": {"value": document_id}})
        with httpx.Client(timeout=TIMEOUT) as c:
            cls._ensure_payload_indexes(c, base, coll, h)
            _check(c.post(f"{base}/collections/{coll}/points/delete?wait=true", headers=h,
                          json={"filter": {"must": must}}),
                   f"Qdrant delete from '{coll}'")


# ---------------------------------------------------------------- Weaviate
class Weaviate:
    name = "weaviate"

    @staticmethod
    def _h(cfg: Dict[str, Any]) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            h["Authorization"] = f"Bearer {cfg['api_key']}"
        return h

    @staticmethod
    def _class_name(cfg: Dict[str, Any], kb_id: str) -> str:
        """Weaviate class names must be GraphQL-safe and start with an
        uppercase letter, so a kb id like "support-docs" cannot be used
        verbatim."""
        raw = _collection(cfg, kb_id)
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
        if not cleaned:
            cleaned = "Documents"
        return cleaned[0].upper() + cleaned[1:]

    @classmethod
    def ensure_collection(cls, cfg: Dict[str, Any], kb_id: str, dim: int) -> str:
        base, cname, h = _base_url(cfg, "Weaviate"), cls._class_name(cfg, kb_id), cls._h(cfg)
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(f"{base}/v1/schema/{cname}", headers=h)
            if r.status_code == 200:
                return cname
            _check(c.post(f"{base}/v1/schema", headers=h, json={
                "class": cname,
                # vectorizer none: this platform supplies its own vectors,
                # so Weaviate must not try to embed the text again with a
                # different model.
                "vectorizer": "none",
                "properties": [
                    {"name": "content", "dataType": ["text"]},
                    {"name": "kb_id", "dataType": ["text"]},
                    {"name": "document_id", "dataType": ["text"]},
                    {"name": "filename", "dataType": ["text"]},
                    {"name": "chunk_index", "dataType": ["int"]},
                ],
            }), f"Weaviate create class '{cname}'")
        return cname

    @classmethod
    def upsert(cls, cfg: Dict[str, Any], kb_id: str, points: List[Dict[str, Any]]) -> int:
        base, cname, h = _base_url(cfg, "Weaviate"), cls._class_name(cfg, kb_id), cls._h(cfg)
        objects = []
        for p in points:
            payload = dict(p["payload"])
            props = {
                "content": payload.get("content", ""),
                "kb_id": payload.get("kb_id", kb_id),
                "document_id": payload.get("document_id", ""),
                "filename": payload.get("filename", ""),
                "chunk_index": int(payload.get("chunk_index", 0)),
            }
            # Anything the caller added — the user's own metadata — must
            # travel too. Mapping only the five declared properties threw
            # every metadata field away silently: it was visible in the
            # ingest request and absent from the stored object, so the
            # Metadata column read empty with nothing to explain why.
            # Weaviate's auto-schema adds undeclared properties on write.
            for key, value in payload.items():
                if key in props:
                    continue
                # Only scalars: a nested object would need its own class.
                if isinstance(value, (str, int, float, bool)) or value is None:
                    props[key] = value
            objects.append({"class": cname, "vector": p["vector"], "properties": props})
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _check(c.post(f"{base}/v1/batch/objects", headers=h,
                                 json={"objects": objects}),
                          f"Weaviate batch insert into '{cname}'")
        # A batch returns 200 even when individual objects fail, with the
        # reason on each element — so success has to be counted, not
        # assumed from the status code.
        failures = [o for o in (data or [])
                    if ((o.get("result") or {}).get("status") == "FAILED")]
        if failures:
            first = ((failures[0].get("result") or {}).get("errors") or {})
            raise VectorStoreError(
                f"Weaviate rejected {len(failures)} of {len(objects)} objects: "
                f"{str(first)[:200]}"
            )
        return len(objects)

    @classmethod
    def query(cls, cfg: Dict[str, Any], kb_id: str, vector: List[float],
              top_k: int) -> List[Dict[str, Any]]:
        base, cname, h = _base_url(cfg, "Weaviate"), cls._class_name(cfg, kb_id), cls._h(cfg)
        gql = {
            "query": f"""{{
              Get {{
                {cname}(
                  limit: {int(top_k)}
                  nearVector: {{ vector: {list(vector)} }}
                  where: {{ path: ["kb_id"], operator: Equal, valueText: "{kb_id}" }}
                ) {{
                  content document_id filename chunk_index
                  _additional {{ certainty }}
                }}
              }}
            }}"""
        }
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _check(c.post(f"{base}/v1/graphql", headers=h, json=gql),
                          f"Weaviate search '{cname}'")
        if data.get("errors"):
            raise VectorStoreError(f"Weaviate query error: {str(data['errors'])[:220]}")
        rows = ((data.get("data") or {}).get("Get") or {}).get(cname) or []
        out = []
        for row in rows:
            out.append({
                "score": (row.get("_additional") or {}).get("certainty"),
                "payload": {k: row.get(k) for k in
                            ("content", "document_id", "filename", "chunk_index")},
            })
        return out

    @classmethod
    def delete_by_document(cls, cfg: Dict[str, Any], kb_id: str, document_id: Optional[str]) -> None:
        base, cname, h = _base_url(cfg, "Weaviate"), cls._class_name(cfg, kb_id), cls._h(cfg)
        operands = [{"path": ["kb_id"], "operator": "Equal", "valueText": kb_id}]
        if document_id:
            operands.append({"path": ["document_id"], "operator": "Equal",
                             "valueText": document_id})
        where = ({"operator": "And", "operands": operands} if len(operands) > 1
                 else operands[0])
        with httpx.Client(timeout=TIMEOUT) as c:
            # request(), not delete(): httpx's own delete() accepts no
            # body at all, so `json=` there was a TypeError every time
            # this ran — "Client.delete() got an unexpected keyword
            # argument 'json'". Weaviate's batch-delete genuinely is a
            # DELETE with a body, which only request() can send.
            r = c.request("DELETE", f"{base}/v1/batch/objects", headers=h,
                          json={"match": {"class": cname, "where": where}})
            # A class that was never created has nothing to delete, which
            # is a success for a force-reindex, not a failure.
            if r.status_code not in (200, 404):
                _check(r, f"Weaviate delete from '{cname}'")


# ----------------------------------------------------------- Milvus / Zilliz
class Milvus:
    name = "milvus"

    @staticmethod
    def _h(cfg: Dict[str, Any]) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            h["Authorization"] = f"Bearer {cfg['api_key']}"
        return h

    @classmethod
    def ensure_collection(cls, cfg: Dict[str, Any], kb_id: str, dim: int) -> str:
        base, coll, h = _base_url(cfg, "Zilliz/Milvus"), _collection(cfg, kb_id), cls._h(cfg)
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _check(c.post(f"{base}/v2/vectordb/collections/list", headers=h, json={}),
                          "Milvus list collections")
            if coll in (data.get("data") or []):
                return coll
            # The quick-setup schema: an auto id, a float vector of the
            # given width, and dynamic fields for the payload, which
            # avoids declaring every metadata key up front.
            _check(c.post(f"{base}/v2/vectordb/collections/create", headers=h, json={
                "collectionName": coll, "dimension": int(dim),
                "metricType": "COSINE", "enableDynamicField": True,
            }), f"Milvus create collection '{coll}'")
        return coll

    @classmethod
    def upsert(cls, cfg: Dict[str, Any], kb_id: str, points: List[Dict[str, Any]]) -> int:
        base, coll, h = _base_url(cfg, "Zilliz/Milvus"), _collection(cfg, kb_id), cls._h(cfg)
        rows = [{"vector": p["vector"], **p["payload"]} for p in points]
        with httpx.Client(timeout=TIMEOUT) as c:
            _check(c.post(f"{base}/v2/vectordb/entities/insert", headers=h,
                          json={"collectionName": coll, "data": rows}),
                   f"Milvus insert into '{coll}'")
        return len(rows)

    @classmethod
    def query(cls, cfg: Dict[str, Any], kb_id: str, vector: List[float],
              top_k: int) -> List[Dict[str, Any]]:
        base, coll, h = _base_url(cfg, "Zilliz/Milvus"), _collection(cfg, kb_id), cls._h(cfg)
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _check(c.post(f"{base}/v2/vectordb/entities/search", headers=h, json={
                "collectionName": coll, "data": [vector], "limit": int(top_k),
                "filter": f'kb_id == "{kb_id}"',
                "outputFields": ["content", "document_id", "filename", "chunk_index"],
            }), f"Milvus search '{coll}'")
        out = []
        for row in (data.get("data") or []):
            out.append({"score": row.get("distance"),
                        "payload": {k: row.get(k) for k in
                                    ("content", "document_id", "filename", "chunk_index")}})
        return out

    @classmethod
    def delete_by_document(cls, cfg: Dict[str, Any], kb_id: str, document_id: Optional[str]) -> None:
        base, coll, h = _base_url(cfg, "Zilliz/Milvus"), _collection(cfg, kb_id), cls._h(cfg)
        expr = f'kb_id == "{kb_id}"'
        if document_id:
            expr += f' and document_id == "{document_id}"'
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(f"{base}/v2/vectordb/entities/delete", headers=h,
                       json={"collectionName": coll, "filter": expr})
            if r.status_code not in (200, 404):
                _check(r, f"Milvus delete from '{coll}'")


def _sample_qdrant(cfg, kb_id, document_id):
    base, coll, h = _base_url(cfg, "Qdrant"), _collection(cfg, kb_id), Qdrant._h(cfg)
    body = {"limit": 1, "with_payload": True, "filter": {"must": [
        {"key": "kb_id", "match": {"value": kb_id}},
        {"key": "document_id", "match": {"value": document_id}},
    ]}}
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _check(c.post(f"{base}/collections/{coll}/points/scroll", headers=h, json=body),
                      "Qdrant scroll")
    pts = (data.get("result") or {}).get("points") or []
    return (pts[0].get("payload") or {}) if pts else {}


def _sample_weaviate(cfg, kb_id, document_id):
    base, cname, h = _base_url(cfg, "Weaviate"), Weaviate._class_name(cfg, kb_id), Weaviate._h(cfg)
    # Ask for the class's actual properties, since user metadata fields
    # were added by auto-schema and are not known ahead of time.
    with httpx.Client(timeout=TIMEOUT) as c:
        schema = _check(c.get(f"{base}/v1/schema/{cname}", headers=h), "Weaviate class schema")
        names = [p["name"] for p in (schema.get("properties") or [])]
        extra = [n for n in names if n not in
                 ("content", "kb_id", "document_id", "filename", "chunk_index")]
        if not extra:
            return {}
        fields = " ".join(extra)
        gql = {"query": f"""{{ Get {{ {cname}(
            limit: 1
            where: {{ operator: And operands: [
              {{ path: ["kb_id"], operator: Equal, valueText: "{kb_id}" }},
              {{ path: ["document_id"], operator: Equal, valueText: "{document_id}" }}
            ] }}
        ) {{ {fields} }} }} }}"""}
        data = _check(c.post(f"{base}/v1/graphql", headers=h, json=gql), "Weaviate sample")
    rows = ((data.get("data") or {}).get("Get") or {}).get(cname) or []
    return {k: v for k, v in (rows[0] or {}).items() if v is not None} if rows else {}


def _sample_milvus(cfg, kb_id, document_id):
    base, coll, h = _base_url(cfg, "Zilliz/Milvus"), _collection(cfg, kb_id), Milvus._h(cfg)
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _check(c.post(f"{base}/v2/vectordb/entities/query", headers=h, json={
            "collectionName": coll, "limit": 1,
            "filter": f'kb_id == "{kb_id}" and document_id == "{document_id}"',
            "outputFields": ["*"],
        }), "Milvus query")
    rows = data.get("data") or []
    skip = {"content", "kb_id", "document_id", "filename", "chunk_index", "vector", "id"}
    return {k: v for k, v in (rows[0] or {}).items() if k not in skip} if rows else {}


Qdrant.sample_payload = staticmethod(_sample_qdrant)
Weaviate.sample_payload = staticmethod(_sample_weaviate)
Milvus.sample_payload = staticmethod(_sample_milvus)


ADAPTERS = {"qdrant": Qdrant, "weaviate": Weaviate, "milvus": Milvus}


def adapter_for(provider: str):
    """The store adapter for a provider, or None if it has none."""
    return ADAPTERS.get(provider)


def build_points(kb_id: str, document_id: str, filename: str, chunks: List[str],
                 vectors: List[List[float]],
                 user_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Chunks + vectors in the shape every adapter above expects.

    kb_id travels on every point so a collection shared with other data
    can still be queried and cleaned up without touching anything this
    platform did not write.
    """
    points = []
    for i, (text, vec) in enumerate(zip(chunks, vectors)):
        payload: Dict[str, Any] = {
            "content": text, "kb_id": kb_id, "document_id": document_id,
            "filename": filename, "chunk_index": i,
        }
        if user_meta:
            payload.update({k: v for k, v in user_meta.items() if k not in payload})
        points.append({"vector": list(vec), "payload": payload})
    return points
