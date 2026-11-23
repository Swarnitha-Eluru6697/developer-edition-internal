"""Real Pinecone vector-store operations for a Knowledge Base.

Uses Pinecone's REST API over httpx rather than the `pinecone` SDK: the
SDK is not in requirements.txt, and the four calls needed here (describe
index, upsert, query, delete-by-filter) are small and stable. Keeping it
dependency-free also means an existing deployment does not have to
rebuild its image to gain Pinecone support.

Honest scope. Pinecone is a vector index, so only the parts of this
platform's KB model that map onto one are implemented:

  * ingest      -> upsert, with chunk text and bookkeeping as metadata
  * retrieval    -> vector query, filtered on the same `enabled` flag
                    pgvector's WHERE clause uses
  * documents    -> delete / enable / disable by metadata filter

Deliberately NOT implemented, because Pinecone has no equivalent rather
than because it was skipped:

  * Full-Text and Hybrid retrieval — there is no lexical index to run
    `to_tsvector` against. Pinecone's sparse vectors are a genuinely
    different feature, not a drop-in for Postgres full-text search.
  * The Economical index method — it stores keywords and no vectors,
    which a vector index cannot hold.

Both are refused with a specific reason at the call site rather than
silently degrading to something else.
"""
from typing import Any, Dict, List, Optional, Tuple

import httpx

API_VERSION = "2025-04"
CONTROL_PLANE = "https://api.pinecone.io"

# Pinecone caps metadata at 40 KB per vector, and the chunk text lives
# there because Pinecone stores no payload outside metadata. Chunks are
# bounded by max_words at ingest, but a long chunk in a CJK script can
# still exceed this, so it is enforced rather than assumed.
MAX_METADATA_BYTES = 40 * 1024


class PineconeError(RuntimeError):
    pass


def _headers(api_key: str) -> Dict[str, str]:
    return {"Api-Key": api_key, "X-Pinecone-API-Version": API_VERSION,
            "Content-Type": "application/json"}


def list_indexes(api_key: str) -> List[Dict[str, Any]]:
    r = httpx.get(f"{CONTROL_PLANE}/indexes", headers=_headers(api_key), timeout=30)
    if r.status_code != 200:
        raise PineconeError(f"Pinecone /indexes returned {r.status_code}: {r.text[:160]}")
    return r.json().get("indexes", [])


def describe_index(api_key: str, index_name: str) -> Dict[str, Any]:
    """The index's own host and dimension. Both are needed before any
    write: every other call goes to the index-specific host, and a
    vector whose width differs from the index is rejected outright."""
    r = httpx.get(f"{CONTROL_PLANE}/indexes/{index_name}", headers=_headers(api_key), timeout=30)
    if r.status_code == 404:
        available = [i.get("name") for i in list_indexes(api_key)]
        raise PineconeError(
            f"No Pinecone index named '{index_name}'. Indexes on this account: {available or 'none'}."
        )
    if r.status_code != 200:
        raise PineconeError(f"Pinecone describe index failed ({r.status_code}): {r.text[:160]}")
    return r.json()


def _data_url(host: str, path: str) -> str:
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{host}{path}"


def upsert(api_key: str, host: str, namespace: str, vectors: List[Dict[str, Any]]) -> int:
    """Upsert in batches. Pinecone's documented ceiling is 1000 vectors
    or 2 MB per request, whichever comes first; 100 keeps requests well
    inside the size limit given chunk text travels in metadata."""
    sent = 0
    for i in range(0, len(vectors), 100):
        batch = vectors[i:i + 100]
        body: Dict[str, Any] = {"vectors": batch}
        if namespace:
            body["namespace"] = namespace
        r = httpx.post(_data_url(host, "/vectors/upsert"), headers=_headers(api_key),
                       json=body, timeout=120)
        if r.status_code != 200:
            raise PineconeError(f"Pinecone upsert failed ({r.status_code}): {r.text[:200]}")
        sent += r.json().get("upsertedCount", len(batch))
    return sent


def query(api_key: str, host: str, namespace: str, vector: List[float], top_k: int,
          metadata_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    body: Dict[str, Any] = {"vector": vector, "topK": max(1, top_k),
                            "includeMetadata": True, "includeValues": False}
    if namespace:
        body["namespace"] = namespace
    if metadata_filter:
        body["filter"] = metadata_filter
    r = httpx.post(_data_url(host, "/query"), headers=_headers(api_key), json=body, timeout=60)
    if r.status_code != 200:
        raise PineconeError(f"Pinecone query failed ({r.status_code}): {r.text[:200]}")
    return r.json().get("matches", [])


def delete_by_filter(api_key: str, host: str, namespace: str, metadata_filter: Dict[str, Any]) -> None:
    body: Dict[str, Any] = {"filter": metadata_filter}
    if namespace:
        body["namespace"] = namespace
    r = httpx.post(_data_url(host, "/vectors/delete"), headers=_headers(api_key),
                   json=body, timeout=60)
    if r.status_code != 200:
        raise PineconeError(f"Pinecone delete failed ({r.status_code}): {r.text[:200]}")


def describe_stats(api_key: str, host: str) -> Dict[str, Any]:
    r = httpx.post(_data_url(host, "/describe_index_stats"), headers=_headers(api_key),
                   json={}, timeout=30)
    if r.status_code != 200:
        raise PineconeError(f"Pinecone stats failed ({r.status_code}): {r.text[:160]}")
    return r.json()


def build_vectors(document_id: str, filename: str, chunks: List[str],
                  embeddings: List[List[float]], extra_metadata: Optional[Dict[str, Any]] = None,
                  parent_texts: Optional[List[Optional[str]]] = None) -> List[Dict[str, Any]]:
    """One Pinecone vector per chunk. Pinecone has no columns, so the
    fields pgvector keeps as columns (content, document_id, enabled,
    chunk_index) become metadata — which is also what makes them
    filterable, the same way the SQL WHERE clause filters on them."""
    out = []
    for i, (text, vec) in enumerate(zip(chunks, embeddings)):
        meta: Dict[str, Any] = {
            "content": text,
            "source_filename": filename,
            "document_id": document_id,
            "chunk_index": i,
            "word_count": len(text.split()),
            # Mirrors pgvector's `enabled` column so a disabled document
            # is excluded by the query filter rather than deleted.
            "enabled": True,
        }
        if parent_texts and i < len(parent_texts) and parent_texts[i]:
            meta["parent_content"] = parent_texts[i]
        for k, v in (extra_metadata or {}).items():
            # Pinecone metadata values must be scalars or string lists.
            if isinstance(v, (str, int, float, bool)):
                meta[k] = v
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                meta[k] = v
            else:
                meta[k] = str(v)
        size = len(str(meta).encode("utf-8"))
        if size > MAX_METADATA_BYTES:
            raise PineconeError(
                f"Chunk {i} of '{filename}' needs {size} bytes of metadata, over Pinecone's "
                f"{MAX_METADATA_BYTES}-byte limit. Re-ingest with a smaller max_words."
            )
        out.append({"id": f"{document_id}-{i}", "values": vec, "metadata": meta})
    return out
