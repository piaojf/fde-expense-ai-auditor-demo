"""Deliberately opt-in; at most four successful DeepSeek calls in the API suite."""

import os

import pytest

from scripts import m4b_real_llm


@pytest.mark.live_llm
@pytest.mark.skipif(os.getenv("RUN_LIVE_LLM_TESTS") != "1", reason="Paid DeepSeek gate disabled")
def test_real_deepseek_pgvector_cases_and_governance():
    settings = m4b_real_llm.live_settings()
    result = m4b_real_llm.run_verify(settings)
    assert result["RESULT"] == "PASS"
    assert result["BROWSER_E2E"] == "NOT_RUN"
