"""Embedding providers.

Anthropic doesn't offer an embeddings endpoint; Voyage AI is the recommended
pairing. The `hashing` provider is a dependency-free lexical embedder for
development, tests and air-gapped demos. Switching providers requires
re-indexing (`python -m app.cli reindex`).
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from app.config import get_settings


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have i in is it its of on or that the this to was "
    "were will with what when where who how can do does my our we you your".split()
)


class HashingEmbedder:
    """Feature-hashed unigram + bigram bag of words, L2-normalised."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        toks = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str, dim: int) -> None:
        import voyageai

        self.client = voyageai.Client(api_key=api_key)
        self.model = model
        self.dim = dim

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            res = self.client.embed(
                texts[i : i + 64],
                model=self.model,
                input_type=input_type,
                output_dimension=self.dim,
            )
            out.extend(res.embeddings)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "voyage":
        return VoyageEmbedder(s.voyage_api_key, s.voyage_model, s.embedding_dim)
    return HashingEmbedder(s.embedding_dim)
