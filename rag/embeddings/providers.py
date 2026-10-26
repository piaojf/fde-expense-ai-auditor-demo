"""Replaceable dense embedding providers; deterministic vectors are test-only."""

import hashlib
import math
from typing import Protocol

from rag.embeddings.base import BGE_M3_DENSE_DIMENSIONS, BGE_M3_MODEL
from rag.embeddings.remote import RemoteEmbeddingProvider as RemoteEmbeddingProvider


class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


def validate_vectors(vectors, *, dimension: int, count: int) -> list[list[float]]:
    if len(vectors) != count:
        raise ValueError("Embedding count mismatch")
    result = []
    for vector in vectors:
        values = [float(v) for v in vector]
        if len(values) != dimension or not all(math.isfinite(v) for v in values):
            raise ValueError("Embedding dimension or finite-value mismatch")
        if not any(values):
            raise ValueError("Zero embedding cannot use cosine distance")
        result.append(values)
    return result


class DeterministicTestEmbedding:
    model_name = "deterministic-test-only-v1"
    dimension = BGE_M3_DENSE_DIMENSIONS

    def embed_query(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        for char in text or " ":
            digest = hashlib.sha256(char.encode("utf-8")).digest()
            values[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


class SentenceTransformerEmbedding:
    """Lazy local inference; neither an LLM provider nor a remote API credential."""

    def __init__(self, model_name=BGE_M3_MODEL, dimension=1024, device="cpu"):
        if dimension != BGE_M3_DENSE_DIMENSIONS:
            raise ValueError("M4A schema requires 1024-dimensional dense embeddings")
        self.model_name, self.dimension, self.device = model_name, dimension, device
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, device=self.device)
            if model.get_sentence_embedding_dimension() != self.dimension:
                raise ValueError("Configured embedding model differs from database dimension")
            self._model = model
        return self._model

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load().encode(texts, normalize_embeddings=True).tolist()
        return validate_vectors(vectors, dimension=self.dimension, count=len(texts))
