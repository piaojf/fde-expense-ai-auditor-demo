"""No-network verification of real-provider selection and durable failure accounting."""

import asyncio
import json

import pytest

from integrations.llm.budgeted import BudgetedLLM
from integrations.llm.deepseek import DeepSeekError
from scripts.finance_workbench_ai import LEDGER, PROFILE, enable, load_live_settings


def runtime(tmp_path, *, key="unit-test-only"):
    config = tmp_path / "private.env"
    config.write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
    directory = tmp_path / "generation"
    directory.mkdir()
    (directory / "fixture-summary.json").write_text("{}", encoding="utf-8")
    return directory, config


def test_real_opt_in_contains_no_secret_and_never_resets_budget(tmp_path):
    directory, config = runtime(tmp_path)
    result = enable(directory, config, consent=True, maximum=2)
    assert result["provider"] == "deepseek"
    assert "unit-test-only" not in (directory / PROFILE).read_text(encoding="utf-8")
    assert (directory / PROFILE).stat().st_mode & 0o777 == 0o600
    settings = load_live_settings(directory)
    assert settings.p4b_live_llm is True
    assert settings.llm_provider == "deepseek"
    assert settings.p4b_live_budget_path == str(directory / LEDGER)
    budget = BudgetedLLM(None, directory / LEDGER, 2)
    with budget.db:
        budget.db.execute("INSERT INTO llm_attempts DEFAULT VALUES")
    budget.db.close()
    assert enable(directory, config, consent=True, maximum=2)["budget"]["remaining"] == 1
    with pytest.raises(ValueError, match="cannot be silently changed"):
        enable(directory, config, consent=True, maximum=3)


def test_missing_credentials_or_consent_never_enables_mock(tmp_path):
    directory, config = runtime(tmp_path, key="")
    assert load_live_settings(directory) is None
    with pytest.raises(ValueError, match="Explicit consent"):
        enable(directory, config)
    with pytest.raises(DeepSeekError, match="API_KEY_REQUIRED"):
        enable(directory, config, consent=True)
    assert not (directory / PROFILE).exists()
    config.write_text("DEEPSEEK_API_KEY=unit-test-only\n", encoding="utf-8")
    enable(directory, config, consent=True)
    config.unlink()
    with pytest.raises(ValueError, match="credential configuration required"):
        load_live_settings(directory)


def test_invalid_live_profile_and_uninitialized_runtime_fail_closed(tmp_path):
    directory, config = runtime(tmp_path)
    enable(directory, config, consent=True)
    path = directory / PROFILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = "mock"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="no Mock fallback"):
        load_live_settings(directory)
    path.unlink()
    (directory / "fixture-summary.json").unlink()
    with pytest.raises(ValueError, match="Initialize offline"):
        enable(directory, config, consent=True)


def test_explicit_current_data_switch_preserves_remaining_budget(tmp_path):
    directory, config = runtime(tmp_path)
    enable(directory, config, consent=True, maximum=2)
    budget = BudgetedLLM(None, directory / LEDGER, 2)
    with budget.db:
        budget.db.execute("INSERT INTO llm_attempts DEFAULT VALUES")
    budget.db.close()
    result = enable(directory, config, consent=True, maximum=2, existing_data=True)
    assert result["advisory_protocol"] == "p4b.advisory.4"
    assert result["budget"] == {"maximum": 2, "used": 1, "remaining": 1}
    assert load_live_settings(directory).p4b_advisory_protocol == "p4b.advisory.4"


def test_shared_budget_preserves_protocol_and_counts_failed_calls(tmp_path):
    class Provider:
        model = "deepseek-test"
        protocol_version = "p4b.advisory.3"

        def __init__(self, fail=False):
            self.calls, self.fail = 0, fail

        async def analyze(self, request):
            self.calls += 1
            if self.fail:
                raise DeepSeekError("UPSTREAM_FAILURE")
            return "real-adapter-result"

    upstream = Provider()
    failing = Provider(fail=True)
    first = BudgetedLLM(upstream, tmp_path / LEDGER, 2)
    second = BudgetedLLM(failing, tmp_path / LEDGER, 2)
    assert first.protocol_version == "p4b.advisory.3"
    async def check_calls():
        assert await first.analyze(None) == "real-adapter-result"
        with pytest.raises(DeepSeekError, match="UPSTREAM_FAILURE"):
            await second.analyze(None)
        with pytest.raises(DeepSeekError, match="LOCAL_AI_BUDGET_EXHAUSTED"):
            await first.analyze(None)

    asyncio.run(check_calls())
    assert upstream.calls == failing.calls == 1
    assert first.usage() == {"maximum": 2, "used": 2, "remaining": 0}
    first.db.close()
    second.db.close()
    restarted = BudgetedLLM(upstream, tmp_path / LEDGER, 2)
    assert restarted.usage()["remaining"] == 0
    restarted.db.close()
