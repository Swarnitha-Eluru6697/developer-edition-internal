"""Tests for the embedding-model catalog and the predicate that decides
what the RAG Settings dropdown may offer.

Two real problems these lock down:

1. The dropdown filtered on the "Text Embedding" type — a checkbox a
   person ticks when registering a model. A genuine embedding model
   saved as types=["LLM"] (`intfloat/e5-base-v2`) was silently absent
   with nothing on screen explaining why. The same gate also existed
   inside embeddings.py's runtime lookup, so the two had to move
   together or the dropdown would offer a model that then refused to
   embed.

2. seed() returns early on an already-seeded database, so anything
   added to a catalog list afterwards never reached an existing
   install. sync_model_catalog() backfills insert-only.

These exercise the real functions directly and touch no network.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.embeddings import is_embedding_model, EMBEDDING_TYPE
from app.seed import PROVIDER_EMBEDDING_MODELS


class TestIsEmbeddingModel:
    def test_a_declared_text_embedding_type_qualifies_without_inference(self):
        usable, inferred = is_embedding_model(
            "E5 Large 1024", "intfloat/multilingual-e5-large", [EMBEDDING_TYPE])
        assert usable is True
        # Declared, so nothing was guessed and no caveat is warranted.
        assert inferred is False

    @pytest.mark.parametrize("api_model_id", [
        "text-embedding-3-small", "text-embedding-ada-002", "embed-english-v3.0",
        "nomic-embed-text-v1.5", "mixedbread-ai/mxbai-embed-large-v1",
        "BAAI/bge-large-en-v1.5", "Alibaba-NLP/gte-large-en-v1.5",
        "sentence-transformers/all-MiniLM-L6-v2", "sentence-transformers/all-mpnet-base-v2",
        "jinaai/jina-embeddings-v3", "voyage-3-large", "gemini-embedding-001",
        "hkunlp/instructor-large", "intfloat/e5-base-v2", "Qwen/Qwen3-Embedding-8B",
    ])
    def test_a_real_embedding_model_qualifies_from_its_id_even_when_typed_llm(self, api_model_id):
        """The whole point: the model id is evidence about the model,
        the type checkbox is only evidence about what someone ticked."""
        usable, inferred = is_embedding_model("whatever", api_model_id, ["LLM"])
        assert usable is True, f"{api_model_id} should be recognised as an embedding model"
        assert inferred is True

    @pytest.mark.parametrize("api_model_id", [
        "claude-sonnet-4-6", "deepseek-v4-pro", "Qwen/Qwen2.5", "gpt-4o",
        "meta-llama/Llama-3.1-8B-Instruct", "google/flan-t5-base",
        "facebook/bart-large-cnn", "mistral-large-latest", "rerank-english-v3.0",
    ])
    def test_a_chat_or_rerank_model_is_never_offered_as_an_embedder(self, api_model_id):
        """Offering one would not fail here — it would fail mid-ingest
        with a confusing vendor error, after the person had already
        chosen it."""
        usable, inferred = is_embedding_model("whatever", api_model_id, ["LLM"])
        assert usable is False, f"{api_model_id} must not be offered as an embedding model"
        assert inferred is False

    def test_missing_id_and_types_are_tolerated(self):
        assert is_embedding_model("x", None, None) == (False, False)


class TestCatalog:
    def test_every_catalogued_model_declares_a_real_dimension(self):
        """The pgvector column is created at this width and cannot be
        widened without re-embedding everything, so a missing or
        implausible number here is not cosmetic."""
        for m in PROVIDER_EMBEDDING_MODELS:
            dim = m.get("embedding_dimension")
            assert isinstance(dim, int) and 0 < dim <= 8192, f"{m['name']}: bad dimension {dim!r}"

    def test_no_embedding_models_are_catalogued_for_providers_without_an_embeddings_api(self):
        """Anthropic serves only messages and DeepSeek only
        chat/completions — neither ships an embeddings endpoint. A row
        for either would be offered and then 404 on first ingest."""
        offenders = [m["name"] for m in PROVIDER_EMBEDDING_MODELS
                     if m["provider_name"] in ("Anthropic", "DeepSeek")]
        assert offenders == [], f"these providers have no embeddings API: {offenders}"

    def test_catalogued_names_and_provider_model_ids_are_unique(self):
        """`name` is the primary key, and a duplicated
        (provider, api_model_id) would offer the same model twice."""
        names = [m["name"] for m in PROVIDER_EMBEDDING_MODELS]
        assert len(names) == len(set(names))
        pairs = [(m["provider_name"], m["api_model_id"]) for m in PROVIDER_EMBEDDING_MODELS]
        assert len(pairs) == len(set(pairs))

    def test_every_catalogued_model_is_recognised_by_the_predicate(self):
        """Otherwise a seeded model would be invisible in the very
        dropdown it exists to populate."""
        for m in PROVIDER_EMBEDDING_MODELS:
            usable, _ = is_embedding_model(m["name"], m["api_model_id"], [EMBEDDING_TYPE])
            assert usable is True, m["name"]
