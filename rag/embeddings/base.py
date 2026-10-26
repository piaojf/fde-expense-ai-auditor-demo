from typing import Protocol

BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_DENSE_DIMENSIONS = 1024
BGE_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class EmbeddingAdapter(Protocol):
    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class RerankerAdapter(Protocol):
    def rank(self, query: str, passages: list[str]) -> list[tuple[int, float]]: ...
