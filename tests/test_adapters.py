import asyncio

import pytest

from apps.api.repository import MockClaimRepository
from integrations.erp.mock import MockERP
from integrations.llm.base import ProviderConfig


def test_mock_repository_isolation():
    repo = MockClaimRepository()
    claim = repo.get("CLM-2026-001")
    claim.employee.name = "mutated"
    assert repo.get("CLM-2026-001").employee.name == "演示员工甲"


def test_mock_erp_context():
    context = asyncio.run(MockERP().fetch_claim_context("CLM-2026-001"))
    assert context["source"] == "mock"
    assert context["employee_active"] is True


def test_mock_erp_rejects_unvalidated_write():
    with pytest.raises(ValueError, match="validated APPROVE"):
        asyncio.run(MockERP().write_decision(None, idempotency_key="test"))


@pytest.mark.parametrize("provider", ["mock", "deepseek", "qwen", "openai"])
def test_provider_adapter_configuration(provider):
    config = ProviderConfig(provider=provider, api_key="secret")
    assert config.provider == provider
    assert "secret" not in repr(config)
