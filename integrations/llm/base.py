from typing import Literal, Protocol

from pydantic import Field, PrivateAttr, SecretStr

from domain.schemas import DTO, LLMAnalysis, PolicyEvidence

ProviderName = Literal["mock", "deepseek", "qwen", "openai"]


class ProviderConfig(DTO):
    provider: ProviderName = "mock"
    model: str = "not-configured"
    base_url: str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class AnalysisRequest(DTO):
    _provider_evidence: dict = PrivateAttr(default_factory=dict)
    claim_id: str
    facts: dict[str, str]
    evidence: list[PolicyEvidence]


class LLMProvider(Protocol):
    """Adapters normalize analysis only; approval and ERP access are excluded."""

    async def analyze(self, request: AnalysisRequest) -> LLMAnalysis: ...
