"""Actual API wiring selects the real V3 adapter without sending network requests."""

from datetime import date

import pytest
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from integrations.llm.advisory_v3 import AdvisoryDeepSeekProviderV3
from integrations.llm.budgeted import BudgetedLLM
from tests.test_enterprise import login


def settings(tmp_path):
    return Settings(
        _env_file=None, app_env="test", enterprise_enabled=True,
        p4a_enabled=True, p4b_enabled=True, p4b_advisory_enabled=True,
        p4b_advisory_protocol="p4b.advisory.3", p4b_live_llm=True,
        llm_provider="deepseek", deepseek_api_key="unit-test-only",
        p4b_live_budget_path=str(tmp_path / "shared.sqlite3"), p4b_live_max_requests=2,
    )


def test_actual_catalog_selects_real_budgeted_v3(hotel_portal, tmp_path):
    c, app, repo = hotel_portal[:3]
    install_compliance_api(
        app.state.enterprise_app, settings(tmp_path), repo, app.state.audit_service,
        business_clock=lambda: date(2027, 1, 5),
    )
    provider = app.state.enterprise_app.state.compliance.provider
    assert isinstance(provider, BudgetedLLM)
    assert isinstance(provider.provider, AdvisoryDeepSeekProviderV3)
    assert provider.provider._transport is None
    assert provider.protocol_version == "p4b.advisory.3"
    assert provider.usage() == {"maximum": 2, "used": 0, "remaining": 2}
    assert login(c).status_code == 200
    response = c.get("/api/v1.5/p4b/v1/runtime")
    assert response.status_code == 200
    runtime = response.json()
    assert runtime["live_llm"] is True
    assert runtime["semantic_model"] == "deepseek-flash"
    provider.db.close()


def test_live_mode_cannot_accept_injected_mock(hotel_portal, tmp_path):
    _, app, repo = hotel_portal[:3]
    with pytest.raises(ValueError, match="explicitly configured DeepSeek"):
        install_compliance_api(
            app.state.enterprise_app, settings(tmp_path), repo, app.state.audit_service,
            provider=object(),
        )
