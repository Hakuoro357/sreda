"""Embeddings service (Phase 3).

Pluggable interface so callers don't bind to a specific provider:

  * ``OpenAICompatEmbeddingClient`` — real HTTP call to an OpenAI-style
    ``/v1/embeddings`` endpoint. Primary production path: LM Studio on
    Mac (multilingual-e5-large) or MiMo's ``/embeddings`` if/when they
    expose it.
  * ``FakeEmbeddingClient`` — deterministic hash-based vectors. Used by
    tests to keep memory recall logic testable without a live service.
    NOT semantic — two synonyms hash to different vectors. Use only
    in tests.
  * ``DisabledEmbeddingClient`` — always raises. Picked by the factory
    when no endpoint is configured and caller didn't explicitly opt
    into the fake client; forces a loud failure instead of silently
    degrading search quality.

Every client exposes two methods:
  * ``embed_document(text)`` — for facts we're persisting
  * ``embed_query(text)`` — for recall queries

Some models (notably ``intfloat/multilingual-e5-*``) want a prefix on
queries (``"query: "``) for best quality. The OpenAI-compat client
supports optional prefixes via settings; this keeps the same client
compatible with plain-text endpoints too.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from sreda.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

EMBEDDING_DIM_FAKE = 64


class EmbeddingClient(Protocol):
    """Minimal interface expected by memory repo + graph node.

    Implementations are free to batch / cache internally. Callers treat
    embeddings as opaque ``list[float]`` vectors — similarity math lives
    in ``recall()`` at the repository layer."""

    dim: int

    def embed_document(self, text: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass
class OpenAICompatEmbeddingClient:
    """Calls a ``POST /embeddings`` on any OpenAI-compatible server.

    Works with:
      * LM Studio (``http://host:1234/v1``)
      * OpenAI proper
      * MiMo, when it exposes ``/embeddings``

    Use ``query_prefix`` / ``passage_prefix`` for E5-family models that
    expect ``"query: "`` / ``"passage: "`` prefixes. Leave both empty
    for plain models (e.g. OpenAI ``text-embedding-3-*``)."""

    base_url: str
    api_key: str
    model: str
    dim: int = 1024  # e5-large default; overridable for other models
    timeout_seconds: float = 30.0
    query_prefix: str = ""
    passage_prefix: str = ""

    def embed_document(self, text: str) -> list[float]:
        return self._embed(self.passage_prefix + text)

    def embed_query(self, text: str) -> list[float]:
        return self._embed(self.query_prefix + text)

    def _embed(self, text: str) -> list[float]:
        url = self.base_url.rstrip("/") + "/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {"input": text, "model": self.model}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        vectors = data.get("data") or []
        if not vectors or "embedding" not in vectors[0]:
            raise RuntimeError(
                f"embeddings endpoint {url} returned unexpected body: {data!r}"
            )
        return [float(x) for x in vectors[0]["embedding"]]


@dataclass
class FakeEmbeddingClient:
    """Deterministic hash-based embeddings for tests.

    NOT semantic — ``embed("дочь")`` and ``embed("дочери")`` hash to
    different vectors. Use only when testing memory storage / recall
    wiring, never for quality-sensitive tests."""

    dim: int = EMBEDDING_DIM_FAKE

    def embed_document(self, text: str) -> list[float]:
        return _hash_embed(text, self.dim)

    def embed_query(self, text: str) -> list[float]:
        return _hash_embed(text, self.dim)


class DisabledEmbeddingClient:
    """Raises loudly when embeddings aren't configured.

    Picked by ``get_embeddings_client()`` when no endpoint is set and
    the caller didn't pass ``allow_fake=True``. Silently degrading to
    fake embeddings in production would hide missing config from the
    operator."""

    dim = 0

    def embed_document(self, text: str) -> list[float]:
        raise RuntimeError(
            "embeddings disabled: set SREDA_EMBEDDINGS_BASE_URL + SREDA_EMBEDDINGS_MODEL"
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed_document(text)


def get_embeddings_client(
    settings: Settings | None = None,
    *,
    allow_fake: bool = False,
) -> EmbeddingClient:
    """Return an embeddings client based on settings.

    Precedence:
      1. ``embeddings_base_url`` + ``embeddings_model`` set → real HTTP client
      2. ``allow_fake=True``                               → ``FakeEmbeddingClient``
      3. Otherwise                                         → ``DisabledEmbeddingClient``

    The ``allow_fake`` escape hatch is for tests / dev bootstrap where
    you want memory CRUD to work without running LM Studio. Production
    should always hit path 1."""
    settings = settings or get_settings()
    if settings.embeddings_base_url and settings.embeddings_model:
        # E5-family default: "query: " for queries, plain for passages.
        # Safe default — works for ``multilingual-e5-*`` and is harmless
        # for non-E5 models that happen to tolerate the prefix.
        query_prefix = ""
        passage_prefix = ""
        model_lower = settings.embeddings_model.lower()
        if "e5" in model_lower:
            query_prefix = "query: "
            passage_prefix = "passage: "
        return OpenAICompatEmbeddingClient(
            base_url=settings.embeddings_base_url,
            api_key=settings.embeddings_api_key,
            model=settings.embeddings_model,
            timeout_seconds=settings.embeddings_request_timeout_seconds,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
        )
    if allow_fake:
        return FakeEmbeddingClient()
    return DisabledEmbeddingClient()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Standard cosine similarity. Returns ``0.0`` if either vector has
    zero magnitude (defensive against pathological embeddings)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _hash_embed(text: str, dim: int) -> list[float]:
    """Deterministic text → float vector via SHA-256 expansion.

    Produces unit-ish vectors (values in ``[-1, 1]``). Same input
    always yields the same output. Not semantic — purely for test
    determinism."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand to ``dim`` bytes by rehashing.
    buf = bytearray()
    counter = 0
    while len(buf) < dim:
        buf.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return [(b - 128) / 128 for b in buf[:dim]]
