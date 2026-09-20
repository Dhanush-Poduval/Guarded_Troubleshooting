"""Text embedding via fastembed (ONNX Runtime).

fastembed is used rather than sentence-transformers because it runs on ONNX Runtime and
does not pull in PyTorch. That keeps the install small and, more importantly, keeps the
per-process memory cost low: each uvicorn worker holds its own copy of the model.

Measured behaviour of BAAI/bge-small-en-v1.5 under fastembed 0.8.0:
  * embed(), query_embed() and passage_embed() return identical vectors. fastembed does
    not apply the BGE retrieval instruction for v1.5. Any prefix must be prepended by
    hand, which is what Settings.catalog_query_prefix is for.
  * output vectors are L2-normalised, so cosine similarity equals the dot product and
    cosine distance equals 1 - dot product.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Sequence

import numpy as np

from app.config import Settings, get_settings


class Encoder:
    """Wraps a fastembed model behind the two usages this system has.

    The two are kept as separate methods on purpose. Cache lookup compares a user query
    against other user queries and is symmetric, so both sides must be encoded the same
    way. Catalog search compares a user query against catalog descriptions and is
    asymmetric, so the query side may carry an instruction prefix that the document side
    must not. Collapsing these into one call is how that distinction gets silently lost.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        from fastembed import TextEmbedding

        self._settings = settings or get_settings()
        self._model = TextEmbedding(self._settings.embedding_model)
        self._dim = self._settings.embedding_dim

    @property
    def dimension(self) -> int:
        return self._dim

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)
        vectors = np.array(list(self._model.embed(list(texts))), dtype=np.float32)
        if vectors.shape[1] != self._dim:
            raise ValueError(
                f"{self._settings.embedding_model} produced {vectors.shape[1]} dimensions "
                f"but EMBEDDING_DIM is {self._dim}. The database columns will not match."
            )
        return vectors

    def encode_for_cache(self, texts: Iterable[str]) -> np.ndarray:
        """Encode queries and paraphrases for semantic cache storage and lookup.

        Never prefixed: both sides of this comparison are user queries.
        """
        return self._encode(list(texts))

    def encode_catalog_entries(self, texts: Iterable[str]) -> np.ndarray:
        """Encode catalog match_text, i.e. the document side of catalog search."""
        return self._encode(list(texts))

    def encode_catalog_query(self, text: str) -> np.ndarray:
        """Encode a user query for catalog search, applying the prefix if configured."""
        return self._encode([self._settings.catalog_query_prefix + text])[0]

    def encode_one_for_cache(self, text: str) -> np.ndarray:
        return self.encode_for_cache([text])[0]


@lru_cache(maxsize=1)
def get_encoder() -> Encoder:
    """Process-wide encoder. Loading the model takes seconds, so it is done once at
    startup rather than per request."""
    return Encoder()
