from typing import Protocol

from langchain_core.documents import Document


class PolicyIngestor(Protocol):
    """Chunks retain policy_version_id and source metadata; implementation is M2."""

    def load(self, source_uri: str, *, policy_version_id: str) -> list[Document]: ...
