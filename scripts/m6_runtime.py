"""M6 test-only runtime; real dates and publication timestamps remain unchanged."""

import json
from datetime import UTC, date, datetime

import domain.submission as submission
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from integrations.erp.mock import MockERP
from integrations.faults import FailureMode, FaultInjector
from scripts.m6_demo import ROOT, active

OUT = active()


def business_date():
    return date.fromisoformat(
        (OUT / "synthetic-business-date.txt").read_text(encoding="utf-8").strip()
    )


class SyntheticSubmissionClock(datetime):
    @classmethod
    def now(cls, tz=None):
        d = business_date()
        return datetime(d.year, d.month, d.day, 6, tzinfo=UTC).astimezone(tz)


class DemoERP(MockERP):
    """Explicit local dependency fault, never a manufactured HTTP/UI result."""

    async def write_decision(self, result, *, idempotency_key):
        mode = FailureMode((OUT / "erp-fault.txt").read_text(encoding="utf-8").strip())
        if mode != self.faults.mode:
            self.faults = FaultInjector(mode)
        return await super().write_decision(result, idempotency_key=idempotency_key)


settings = Settings(
    app_env="test",
    llm_provider="mock",
    deepseek_api_key=None,
    policy_retriever="pgvector",
    embedding_provider="deterministic",
    embedding_api_key=None,
    policy_demo_enabled=True,
    policy_draft_embedding="deterministic",
    policy_demo_token=(OUT / "demo-token.txt").read_text(encoding="utf-8").strip(),
    policy_remote_enabled=False,
    policy_remote_max_requests=0,
    database_url=(OUT / "database-url.txt").read_text(encoding="utf-8").strip(),
    cors_origins=["http://127.0.0.1:3000"],
    enterprise_enabled=(OUT / "enterprise-enabled.json").exists(),
    enterprise_cookie_secure=False,  # Loopback HTTP synthetic demo only; default is Secure.
    ocr_languages="chi_sim+eng",
    ocr_tessdata=str(ROOT / "output/ui-p2/tessdata"),
    _env_file=None,
)
if settings.enterprise_enabled:
    profile = json.loads((OUT / "enterprise-enabled.json").read_text(encoding="utf-8"))
    assert profile.get("cookie_cors_consent") is True
    assert profile.get("origin") == "http://127.0.0.1:3000"
submission.datetime = SyntheticSubmissionClock
repo = MockClaimRepository(store_path=OUT / "browser.sqlite3")
provider = None
ai_profile = OUT / "enterprise-ai.json"
if ai_profile.exists():
    config = json.loads(ai_profile.read_text(encoding="utf-8"))
    if config["provider"] == "deepseek":
        from integrations.llm.budgeted import BudgetedLLM
        from integrations.llm.deepseek import DeepSeekLLMProvider

        if config.get("egress_consent") is not True:
            raise ValueError("Explicit DeepSeek consent required")
        existing = Settings()
        # Keep the approved endpoint/model pinned; never copy the private key into files.
        settings = settings.model_copy(
            update={"llm_provider": "deepseek", "deepseek_model": config["model"]}
        )
        provider = BudgetedLLM(
            DeepSeekLLMProvider(
                api_key=existing.deepseek_api_key,
                model=config["model"],
                base_url=config["base_url"],
                max_tokens=1024,
            ),
            OUT / "enterprise-ai-budget.sqlite3",
            config["maximum"],
        )
app = create_app(
    settings, repo, erp=DemoERP(repo), provider=provider, business_date_provider=business_date
)
