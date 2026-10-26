"""Opt-in and durable provider call cap; no external requests or key output."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.llm.base import AnalysisRequest
from integrations.llm.budgeted import BudgetedLLM
from integrations.llm.deepseek import DeepSeekError
from integrations.llm.mock import MockLLMProvider
from scripts import enterprise_ai


def test_call_cap_survives_restart_and_counts_failure(tmp_path):
    path = tmp_path / "cap.sqlite3"
    provider = BudgetedLLM(MockLLMProvider(), path, 1)
    request = AnalysisRequest(claim_id="synthetic", facts={}, evidence=[])
    asyncio.run(provider.analyze(request))
    assert provider.usage() == dict(maximum=1, used=1, remaining=0)
    provider.db.close()
    provider = BudgetedLLM(MockLLMProvider(), path, 1)
    with pytest.raises(DeepSeekError, match="LOCAL_AI_BUDGET_EXHAUSTED"):
        asyncio.run(provider.analyze(request))
    assert provider.usage()["used"] == 1
    provider.db.close()


def test_existing_cap_cannot_be_increased(tmp_path):
    path = tmp_path / "cap.sqlite3"
    provider = BudgetedLLM(MockLLMProvider(), path, 1)
    provider.db.close()
    with pytest.raises(ValueError):
        BudgetedLLM(MockLLMProvider(), path, 2)


def test_enable_requires_specific_consent_and_reuses_key_privately(tmp_path, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(enterprise_ai, "active", lambda: tmp_path)
    monkeypatch.setattr(
        enterprise_ai,
        "Settings",
        lambda: SimpleNamespace(
            deepseek_api_key=SecretStr("synthetic-private-key"),
            deepseek_model="deepseek-flash",
            deepseek_base_url="https://api.deepseek.com",
        ),
    )
    with pytest.raises(ValueError, match="Explicit consent"):
        enterprise_ai.enable()
    assert not (tmp_path / "enterprise-ai.json").exists()
    enterprise_ai.enable(consent=True, maximum=20)
    raw = (tmp_path / "enterprise-ai.json").read_text(encoding="utf-8")
    assert "synthetic-private-key" not in raw
    assert (tmp_path / "enterprise-ai.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        enterprise_ai.enable(consent=True, maximum=21)
    with pytest.raises(ValueError):
        enterprise_ai.disable()
    enterprise_ai.disable(consent=True)
    assert "mock" in Path(tmp_path / "enterprise-ai.json").read_text(encoding="utf-8")
