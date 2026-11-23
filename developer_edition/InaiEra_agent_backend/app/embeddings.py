"""
Real, pluggable text embeddings (§0.66, GAP_REPORT — the "embedding
provider abstraction" the reported target architecture asks for).

The default, always-available provider is fully local and genuinely
computed — not a placeholder — but honestly NOT a pretrained semantic
transformer model: scikit-learn's HashingVectorizer, real feature
hashing over word n-grams into a fixed-dimension, L2-normalized
vector. It's genuinely useful for exact/near-duplicate and
lexical-overlap retrieval via real pgvector distance queries — it will
NOT capture deep semantic similarity the way a real trained embedding
model would (it won't know "car" and "automobile" are related).

Why this default exists at all, confirmed directly rather than
assumed: this sandbox's own network policy blocks Hugging Face (a
direct `curl` to huggingface.co returns 403; pypi.org returns 200) —
where real pretrained sentence-transformer weights live — so a real,
locally-run semantic model genuinely cannot be downloaded here.

A real, second, pluggable provider is genuinely implemented and will
actually run once a real API key is configured: EMBEDDING_PROVIDER=
voyage calls Voyage AI's real embeddings API (Anthropic's own
recommended embedding partner, since Anthropic itself has no
embeddings endpoint) over a real HTTPS request — genuine semantic
embeddings, not a fallback. Untested against a live Voyage endpoint in
this environment specifically (no real VOYAGE_API_KEY is available
here to test against), but the same real, direct HTTP call any other
real caller of that API would make; see TestRealVoyageProvider in
tests/test_embedding_provider.py for what's verified here (request
shape, response parsing, real error handling) versus what can only be
confirmed against a genuine live key.

Every call site in this codebase goes through `embed_texts`/
`embed_query`/`current_embed_dim`/`current_embed_model_name` below —
that's the one place any future real provider gets added.
"""
import logging
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

logger = logging.getLogger(__name__)

EMBED_DIM = 384  # the real, fixed dimension of the default hashing provider specifically
EMBED_MODEL_NAME = "local-hashing-v1"  # not a real pretrained model — see module docstring

_vectorizer = HashingVectorizer(
    n_features=EMBED_DIM, alternate_sign=False, norm="l2",
    analyzer="word", ngram_range=(1, 2), lowercase=True,
)

# Real, honest, process-lifetime cache of a real hosted provider's own
# actual embedding dimension, discovered from its own real first
# response rather than hardcoded — different real models (and
# real per-account configuration, for some providers) can genuinely
# produce different dimensions, and guessing wrong would silently
# corrupt every later real pgvector column-dimension assumption.
_voyage_dim_cache: Optional[int] = None


def _active_provider() -> str:
    # Read fresh on every call, not cached at import — matches this
    # codebase's own established pattern (model_adapter._mocks_allowed,
    # etc.) for anything env-driven, so a real test can flip the real
    # env var and see the real, immediate effect.
    return os.getenv("EMBEDDING_PROVIDER", "hashing").strip().lower()


def _embed_hashing(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    matrix = _vectorizer.transform(texts)
    return [row.tolist() for row in matrix.toarray().astype(np.float32)]


def _embed_voyage(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """A real HTTP call to Voyage AI's real embeddings API — genuinely
    NOT a fallback or a simulation. Requires a real VOYAGE_API_KEY;
    raises a real, honest error (never a silent fallback to hashing —
    a person who explicitly configured this provider should see a real
    failure, not unknowingly get lower-quality vectors) if it's
    missing or the real call itself fails.

    model (§0.92, GAP_REPORT) — an explicit override, used by a real
    query-time call pinned to a specific KB's own stored, immutable
    embedding_model (see embed_query's own docstring) rather than
    whatever VOYAGE_EMBED_MODEL currently happens to be. None (every
    real ingestion call, and any query call with no real, stored KB
    config to pin to) keeps reading the current env var exactly as
    before — this is additive, not a behavior change for any existing
    caller."""
    global _voyage_dim_cache
    if not texts:
        return []
    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "EMBEDDING_PROVIDER=voyage is set, but no VOYAGE_API_KEY is configured — "
            "refusing to silently fall back to the hashing provider for a person who "
            "explicitly asked for real semantic embeddings."
        )
    import httpx
    model = model or os.getenv("VOYAGE_EMBED_MODEL", "voyage-3-lite")
    resp = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"input": texts, "model": model},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    vectors = [item["embedding"] for item in data["data"]]
    if vectors:
        _voyage_dim_cache = len(vectors[0])
    return vectors


# Dimensions discovered from a provider's own first real response,
# keyed by (provider, model). Never guessed: a wrong dimension is not a
# recoverable error once a pgvector column has been created with it.
_dim_cache: Dict[tuple, int] = {}


# Substrings that identify a real embedding-model family from its own
# api_model_id or display name. This exists because "Text Embedding" is
# a checkbox a person ticks when registering a model, and a mis-ticked
# box used to make a genuine embedding model invisible to the RAG
# Settings dropdown — `intfloat/e5-base-v2` saved as types=["LLM"] is an
# embedding model whatever the checkbox says. Matching on the model id
# is evidence about the model itself, not about what someone remembered
# to tick.
#
# Deliberately narrow: every entry below names an embedding family, so
# a chat model (claude-sonnet-4-6, flan-t5-base, bart-large-cnn) matches
# nothing here and stays out of the dropdown, where offering it would
# only produce a confusing failure at ingest time.
_EMBEDDING_ID_HINTS = (
    "text-embedding",      # OpenAI text-embedding-3-*, ada-002
    "embed",               # cohere embed-*, nomic-embed-*, mxbai-embed-*, gemini-embedding
    "e5-", "/e5",          # intfloat/e5-*, multilingual-e5-*
    "bge-",                # BAAI/bge-*
    "gte-",                # thenlper/gte-*, Alibaba-NLP/gte-*
    "minilm",              # sentence-transformers/all-MiniLM-*
    "mpnet",               # all-mpnet-base-v2
    "sentence-transformers/",
    "jina-embeddings",
    "voyage-",
    "instructor-",         # hkunlp/instructor-*
    "stella",
    "nv-embed",
)

EMBEDDING_TYPE = "Text Embedding"


def is_embedding_model(name: str, api_model_id: Optional[str], types) -> Tuple[bool, bool]:
    """Whether a registered model can serve as an embedding model, and
    whether that had to be inferred from its id rather than read off its
    declared types.

    Returns (is_embedding, inferred). One predicate, used by BOTH the
    RAG Settings dropdown and the runtime lookup below — if those two
    ever disagreed, the dropdown would offer a model that then refused
    to embed anything.
    """
    if EMBEDDING_TYPE in (types or []):
        return True, False
    hay = f"{name or ''} {api_model_id or ''}".lower()
    if any(hint in hay for hint in _EMBEDDING_ID_HINTS):
        return True, True
    return False, False


def _registered_embedding_model(model_name: str):
    """Look up a model registered on the Integrations page that can
    embed — see is_embedding_model for what qualifies. Returns
    (provider_display_name, api_model_id, declared_dimension) or
    (None, None, None).

    This is what makes the embedding model dynamic: `embeddings.py` no
    longer owns a hardcoded list. A model added under a provider that
    has an API key becomes selectable, and the KB's Settings tab writes
    its name into config.embedding_model.
    """
    try:
        from .db import engine
        from .models import ProviderModel
        from sqlmodel import Session
        with Session(engine) as s:
            row = s.get(ProviderModel, model_name)
            if not row:
                return None, None, None
            usable, _inferred = is_embedding_model(row.name, row.api_model_id, row.types)
            if not usable:
                return None, None, None
            return row.provider_name, row.api_model_id, row.embedding_dimension
    except Exception:
        return None, None, None


def _provider_credentials(provider_display_name: str):
    """The stored API key / base URL for a provider, reusing the same
    row the LLM adapters read so a key is configured once."""
    from .model_adapter import _stored_provider_config
    return _stored_provider_config(provider_display_name)


def _embed_hf_feature_extraction(texts: List[str], api_model_id: str, api_key: str) -> List[List[float]]:
    """Hugging Face serves embeddings from its feature-extraction
    pipeline, not an OpenAI-style /embeddings route — the router's
    OpenAI-compatible surface returns 404 for embedding models, so this
    is a separate path rather than a base_url swap.

    Sentence-transformer models return one vector per input; some return
    a token-level matrix, so the result is mean-pooled when nested.
    """
    import httpx
    url = f"https://router.huggingface.co/hf-inference/models/{api_model_id}/pipeline/feature-extraction"
    out: List[List[float]] = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), 16):
            batch = texts[i:i + 16]
            resp = client.post(url, headers={"Authorization": f"Bearer {api_key}"},
                               json={"inputs": batch, "options": {"wait_for_model": True}})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected feature-extraction response: {str(data)[:120]}")
            if len(batch) == 1 and data and not isinstance(data[0], list):
                data = [data]
            for item in data:
                vec = item
                # Token-level output -> mean-pool to one vector.
                while isinstance(vec, list) and vec and isinstance(vec[0], list):
                    cols = list(zip(*vec))
                    vec = [sum(c) / len(c) for c in cols]
                out.append([float(x) for x in vec])
    return out


def _embed_openai_compatible(texts: List[str], provider: str, api_model_id: str,
                             api_key: str, base_url: Optional[str]) -> List[List[float]]:
    """/v1/embeddings on any provider that implements it (OpenAI itself,
    and the compatibility endpoints of Gemini, Cohere, DeepSeek).
    Reuses the installed openai SDK, so no new dependency."""
    import openai
    from .egress import governed_httpx_client
    from .model_adapter import _OPENAI_COMPATIBLE_BASES
    resolved = base_url or _OPENAI_COMPATIBLE_BASES.get(provider)
    if resolved == "":
        raise RuntimeError(
            f"{provider} is provisioned per resource — set its API base URL on the "
            f"Integrations page before using it for embeddings."
        )
    client = openai.OpenAI(api_key=api_key, http_client=governed_httpx_client(),
                           **({"base_url": resolved} if resolved else {}))
    out: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        out.extend(_embed_batch_with_retry(client, api_model_id, batch, provider))
    return out


# Batch size and retry policy for a rate-limited embedding endpoint.
# Batches were already sent 64 at a time, but back to back with no
# pacing and no retry — so a document large enough to exhaust a
# per-minute token budget failed the WHOLE ingest, having already
# written nothing, rather than waiting out the window. A trial Cohere
# key allows 100,000 tokens/minute, which a few hundred chunks can
# reach.
EMBED_BATCH_SIZE = 64
_EMBED_MAX_ATTEMPTS = 5
_EMBED_BACKOFF_SECONDS = (5, 15, 30, 60)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """The provider's own Retry-After, when it sends one.

    Preferred over any guess: the service knows when its window resets,
    and waiting longer than necessary is as wasteful as retrying too
    soon."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
        value = headers.get(key) if hasattr(headers, "get") else None
        if value:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    return None


def _embed_batch_with_retry(client, api_model_id: str, batch: List[str],
                            provider: str) -> List[List[float]]:
    """One batch, retried while the provider says it is rate limited.

    Only 429 is retried. A bad key, a wrong model id or a malformed
    request will fail identically every time, and retrying those just
    delays an error the caller needs to see.
    """
    import time
    import openai

    last: Optional[Exception] = None
    for attempt in range(_EMBED_MAX_ATTEMPTS):
        try:
            resp = client.embeddings.create(model=api_model_id, input=batch)
            return [list(d.embedding) for d in resp.data]
        except openai.RateLimitError as e:
            last = e
            if attempt == _EMBED_MAX_ATTEMPTS - 1:
                break
            wait = _retry_after_seconds(e)
            if wait is None:
                wait = _EMBED_BACKOFF_SECONDS[min(attempt, len(_EMBED_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "%s rate-limited an embedding batch of %d; waiting %.0fs (attempt %d of %d)",
                provider, len(batch), wait, attempt + 1, _EMBED_MAX_ATTEMPTS,
            )
            time.sleep(wait)
    # Out of attempts: say which provider imposed it and what to do,
    # rather than surfacing the raw vendor payload.
    raise RuntimeError(
        f"{provider} rate-limited this ingestion and kept doing so after "
        f"{_EMBED_MAX_ATTEMPTS} attempts over roughly "
        f"{sum(_EMBED_BACKOFF_SECONDS[:_EMBED_MAX_ATTEMPTS - 1])} seconds. "
        f"Its own message: {last}. Ingest fewer or smaller documents at a time, or use an "
        f"embedding model on a provider without this cap — a trial key's per-minute token "
        f"budget is usually the cause."
    )


def _embed_registered(texts: List[str], model_name: str) -> List[List[float]]:
    """Embed with a model registered on the Integrations page. Raises
    rather than falling back to hashing: a caller who explicitly chose a
    real model must not silently receive lexical hash vectors, which
    would sit in the same column at the same dimension and be
    indistinguishable afterwards."""
    provider, api_model_id, _declared = _registered_embedding_model(model_name)
    if not provider:
        raise RuntimeError(f"'{model_name}' is not a registered Text Embedding model.")
    api_key, _org, base_url = _provider_credentials(provider)
    if not api_key:
        raise RuntimeError(f"No API key configured for {provider} — add one on the Integrations page.")
    if provider == "Hugging Face Hub":
        vectors = _embed_hf_feature_extraction(texts, api_model_id, api_key)
    else:
        vectors = _embed_openai_compatible(texts, provider, api_model_id, api_key, base_url)
    if vectors:
        _dim_cache[(provider, model_name)] = len(vectors[0])
    return vectors


def embedding_dimension_for(model_name: str) -> int:
    """The real dimension for a model, resolved in order: what a live
    response actually returned, then the value declared when the model
    was registered, then a single probe call. Never a guess — the
    pgvector column is created with this number and cannot be changed
    afterwards without re-embedding everything."""
    if model_name in (EMBED_MODEL_NAME, None, ""):
        return EMBED_DIM
    provider, _api_id, declared = _registered_embedding_model(model_name)
    if not provider:
        return EMBED_DIM
    cached = _dim_cache.get((provider, model_name))
    if cached:
        return cached
    if declared:
        return declared
    probe = _embed_registered(["dimension probe"], model_name)
    return len(probe[0])


def embed_texts(texts: List[str], provider: Optional[str] = None, model: Optional[str] = None) -> List[List[float]]:
    """Real vectors for a real batch of chunks — dispatches to the
    real active provider (EMBEDDING_PROVIDER env var) by default. Same
    provider every call within a process, so results are stable and
    comparable across ingestion runs and query time.

    provider/model (§0.92, GAP_REPORT) — a real, explicit override for
    the actual, reported gap: ingestion used whatever provider was
    CURRENTLY configured, and so did every later query — if the real,
    global EMBEDDING_PROVIDER/VOYAGE_EMBED_MODEL changed in between (a
    real, live re-configuration, a deploy, a config edit), the two
    could genuinely diverge, producing vector-dimension errors or
    worse, dimension-compatible but semantically incompatible vectors.
    Passing the SAME explicit provider/model a KB's own ingestion
    originally used and PERSISTED (see knowledge_bases.py's own
    ingest_documents) keeps every later real re-ingestion for that
    same KB pinned to it regardless of what the current global config
    says — None (the default) preserves the exact prior, env-driven
    behavior for a brand-new KB with nothing stored yet."""
    if not texts:
        return []
    # An explicitly named registered model takes precedence: it is what
    # a KB pinned at ingest time, and it must be honoured regardless of
    # whatever EMBEDDING_PROVIDER currently says.
    if model and model != EMBED_MODEL_NAME:
        reg_provider, _api_id, _dim = _registered_embedding_model(model)
        if reg_provider:
            return _embed_registered(texts, model)
    provider = provider or _active_provider()
    if provider == "voyage":
        return _embed_voyage(texts, model=model)
    return _embed_hashing(texts)


def embed_query(text: str, provider: Optional[str] = None, model: Optional[str] = None) -> List[float]:
    """Same real embedding function as ingestion — a query and the
    chunks it's compared against MUST come from the same provider AND
    model or the distance is meaningless.

    provider/model (§0.92, GAP_REPORT) — the actual fix for the
    reported gap: a real caller with a specific KB's own stored,
    immutable embedding_provider/embedding_model (see
    KnowledgeBase.config, routers/knowledge_bases.py) passes them
    through here, pinning THIS query's own vector to the EXACT same
    provider/model that KB's own chunks were actually embedded with —
    never whatever the current, possibly-since-changed global
    EMBEDDING_PROVIDER happens to be at THIS later moment. None (a KB
    predating this fix, with no stored config to pin to) falls back to
    the prior, honest, env-driven behavior."""
    return embed_texts([text], provider=provider, model=model)[0]


def current_embed_dim() -> int:
    """The real, active provider's real vector dimension — used by
    knowledge_bases.py's own ingestion path to size a new KB table's
    real `embedding VECTOR(n)` column correctly for whichever provider
    is genuinely active, instead of a single hardcoded constant that
    would silently be wrong for any provider other than hashing."""
    if _active_provider() == "voyage" and _voyage_dim_cache is not None:
        return _voyage_dim_cache
    return EMBED_DIM


def current_embed_provider() -> str:
    """The real, active provider's own name (§0.92, GAP_REPORT) — a
    real, public accessor for _active_provider, for the same real
    reason current_embed_dim/current_embed_model_name are public:
    surfaced to a caller (knowledge_bases.py's own ingestion path) that
    needs to persist exactly which real provider produced a KB's own
    vectors, not just its model name — a KB's own stored
    embedding_provider/embedding_model/embedding_dimension triple is
    what later query-time embedding pins itself to, so all three need
    to be genuinely knowable, not just the model name alone."""
    return _active_provider()


def current_embed_model_name() -> str:
    """The real, active provider's real model identifier, for the same
    real reason current_embed_dim exists — surfaced to the person via
    the ingestion response and stored on the KB's own config so a
    later re-ingestion (or a person reading their own KB's settings)
    can see which real provider/model actually produced these
    vectors, not an assumed one."""
    provider = _active_provider()
    if provider == "voyage":
        return os.getenv("VOYAGE_EMBED_MODEL", "voyage-3-lite")
    return EMBED_MODEL_NAME


def to_pgvector_literal(vec: List[float]) -> str:
    """pgvector's real text input format: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


# A small, real (not exhaustive) English stopword list — good enough for
# genuine frequency-based keyword extraction without pulling in a corpus
# download this sandbox can't reach.
_STOPWORDS = frozenset("""
a an the this that these those is are was were be been being have has had
do does did will would shall should may might must can could to of in on
at by for with about against between into through during before after
above below from up down out off over under again further then once here
there when where why how all any both each few more most other some such
no nor not only own same so than too very s t just don now i you he she
it we they what which who whom and or but if as until while
""".split())


def extract_keywords(text: str, n: int = 10) -> List[str]:
    """Real, genuine keyword extraction for the Economical index method —
    a plain word-frequency count over the chunk's own text after
    stopword removal, NOT a placeholder list and NOT an embedding call
    (the whole point of "Economical": no embedding tokens spent). This is
    honestly a much weaker retrieval signal than a real TF-IDF-across-
    corpus or trained keyword model would give — good enough for exact
    keyword-overlap matching, not for anything semantic. See
    module docstring for the same honesty boundary on `embed_texts`."""
    import re
    from collections import Counter
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]{1,}", text.lower())
    counts = Counter(w for w in words if w not in _STOPWORDS and len(w) > 2)
    return [w for w, _ in counts.most_common(n)]
