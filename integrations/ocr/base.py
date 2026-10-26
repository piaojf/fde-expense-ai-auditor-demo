from typing import Protocol

from domain.invoices import OCRResult


class OCRProvider(Protocol):
    def recognize(self, path: str) -> OCRResult: ...


class MockOCRProvider:
    """Explicit test adapter. Never selected by runtime settings."""

    def __init__(self, result: OCRResult):
        self.result = result
        self.calls = 0

    def recognize(self, path: str) -> OCRResult:
        self.calls += 1
        return self.result.model_copy(update={"provider": "mock-test"})
