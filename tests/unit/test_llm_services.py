"""Phase 3a sanity tests for LLM + embeddings services."""

from __future__ import annotations

from pathlib import Path

import pytest

from sreda.config.settings import Settings, get_settings
from sreda.services.embeddings import (
    DisabledEmbeddingClient,
    EMBEDDING_DIM_FAKE,
    FakeEmbeddingClient,
    OpenAICompatEmbeddingClient,
    cosine_similarity,
    get_embeddings_client,
)
from sreda.services.llm import get_chat_llm


# ---------------------------------------------------------------------------
# Settings: MiMo API key resolution
# ---------------------------------------------------------------------------


def test_resolve_mimo_api_key_from_env(monkeypatch):
    monkeypatch.setenv("SREDA_MIMO_API_KEY", "env-key")
    get_settings.cache_clear()
    assert get_settings().resolve_mimo_api_key() == "env-key"


def test_resolve_mimo_api_key_from_file(monkeypatch, tmp_path: Path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("  file-key  \n", encoding="utf-8")
    monkeypatch.delenv("SREDA_MIMO_API_KEY", raising=False)
    monkeypatch.setenv("SREDA_MIMO_API_KEY_FILE", str(key_file))
    get_settings.cache_clear()
    assert get_settings().resolve_mimo_api_key() == "file-key"


def test_resolve_mimo_api_key_none_when_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SREDA_MIMO_API_KEY", raising=False)
    monkeypatch.setenv("SREDA_MIMO_API_KEY_FILE", str(tmp_path / "nonexistent.txt"))
    get_settings.cache_clear()
    assert get_settings().resolve_mimo_api_key() is None


# ---------------------------------------------------------------------------
# Chat LLM factory
# ---------------------------------------------------------------------------


def test_get_chat_llm_returns_none_when_no_key(monkeypatch):
    monkeypatch.delenv("SREDA_MIMO_API_KEY", raising=False)
    monkeypatch.delenv("SREDA_MIMO_API_KEY_FILE", raising=False)
    get_settings.cache_clear()
    assert get_chat_llm() is None


def test_get_chat_llm_builds_client_with_key(monkeypatch):
    monkeypatch.setenv("SREDA_MIMO_API_KEY", "test-key")
    monkeypatch.setenv("SREDA_MIMO_CHAT_MODEL", "mimo-v2-pro")
    get_settings.cache_clear()
    llm = get_chat_llm()
    assert llm is not None
    # langchain-openai stores the model on ``.model_name``
    assert llm.model_name == "mimo-v2-pro"


# ---------------------------------------------------------------------------
# Embeddings clients
# ---------------------------------------------------------------------------


def test_fake_embedding_client_deterministic():
    client = FakeEmbeddingClient()
    vec1 = client.embed_document("hello world")
    vec2 = client.embed_document("hello world")
    vec3 = client.embed_document("hello world!")
    assert vec1 == vec2
    assert vec1 != vec3
    assert len(vec1) == EMBEDDING_DIM_FAKE
    # Values should be in [-1, 1]
    assert all(-1 <= v <= 1 for v in vec1)


def test_cosine_similarity_identical_vectors():
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine_similarity(a, b) == 0.0


def test_cosine_similarity_handles_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_mismatched_dim():
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_get_embeddings_client_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SREDA_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("SREDA_EMBEDDINGS_MODEL", raising=False)
    get_settings.cache_clear()
    client = get_embeddings_client()
    assert isinstance(client, DisabledEmbeddingClient)
    with pytest.raises(RuntimeError, match="embeddings disabled"):
        client.embed_document("hi")


def test_get_embeddings_client_allow_fake(monkeypatch):
    monkeypatch.delenv("SREDA_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("SREDA_EMBEDDINGS_MODEL", raising=False)
    get_settings.cache_clear()
    client = get_embeddings_client(allow_fake=True)
    assert isinstance(client, FakeEmbeddingClient)


def test_get_embeddings_client_picks_openai_compat_when_configured(monkeypatch):
    monkeypatch.setenv("SREDA_EMBEDDINGS_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("SREDA_EMBEDDINGS_MODEL", "text-embedding-multilingual-e5-large")
    get_settings.cache_clear()
    client = get_embeddings_client()
    assert isinstance(client, OpenAICompatEmbeddingClient)
    # E5 models get the "query: " / "passage: " prefixes automatically
    assert client.query_prefix == "query: "
    assert client.passage_prefix == "passage: "


def test_openai_compat_client_posts_to_embeddings_endpoint(monkeypatch):
    """Mock httpx so we don't hit a real server. Verifies the client
    builds the right URL/headers/body and parses the response."""

    captured: dict = {}

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return _FakeResponse({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    import sreda.services.embeddings as emb_module

    monkeypatch.setattr(emb_module.httpx, "Client", _FakeClient)

    client = OpenAICompatEmbeddingClient(
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
        model="e5-large",
        query_prefix="query: ",
        passage_prefix="passage: ",
    )
    vec = client.embed_query("сколько лет дочери")
    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://localhost:1234/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer lm-studio"
    assert captured["body"]["input"] == "query: сколько лет дочери"
    assert captured["body"]["model"] == "e5-large"


# ---------------------------------------------------------------------------
# Live LM Studio smoke test (optional — skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    "SREDA_EMBEDDINGS_LIVE_TEST" not in __import__("os").environ,
    reason="set SREDA_EMBEDDINGS_LIVE_TEST=1 to run against real LM Studio",
)
def test_live_lm_studio_embeddings_round_trip(monkeypatch):
    monkeypatch.setenv("SREDA_EMBEDDINGS_BASE_URL", "http://192.168.3.39:1234/v1")
    monkeypatch.setenv(
        "SREDA_EMBEDDINGS_MODEL",
        "text-embedding-intfloat-multilingual-e5-large-instruct",
    )
    get_settings.cache_clear()
    client = get_embeddings_client()
    vec = client.embed_query("у меня дочь Маша 9 лет")
    assert len(vec) >= 512  # e5-large returns 1024
    # Similar query should have high cosine similarity
    vec2 = client.embed_query("сколько лет моей дочери")
    sim = cosine_similarity(vec, vec2)
    # Semantic similarity should be meaningfully above random
    assert sim > 0.7
