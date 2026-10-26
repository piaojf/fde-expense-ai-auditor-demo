"""Synthetic, isolated browser app. Requires this round's loopback consent.

A private test-only business clock exercises future dates without changing system
clock, old production API validation, publication time, or .env. The clock file
is ignored runtime data. No HTTP endpoint can set it. Real audit-start timestamps
remain real, so policy pinning can be verified across publication and restart.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import domain.submission as submission
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3b/browser"
CLOCK = OUT / "synthetic-business-date.txt"


def business_date():
    return date.fromisoformat(CLOCK.read_text(encoding="utf-8").strip())


class SyntheticSubmissionClock(datetime):
    @classmethod
    def now(cls, tz=None):
        d = business_date()
        return datetime(d.year, d.month, d.day, 6, tzinfo=UTC).astimezone(tz)


settings = Settings(
    app_env="test",
    llm_provider="mock",
    deepseek_api_key=None,
    policy_retriever="pgvector",
    embedding_provider="deterministic",
    embedding_api_key=None,
    policy_demo_enabled=True,
    policy_demo_token=(OUT / "demo-token.txt").read_text(encoding="utf-8").strip(),
    policy_draft_embedding="deterministic",
    policy_remote_enabled=False,
    policy_remote_max_requests=0,
    database_url=(OUT / "database-url.txt").read_text(encoding="utf-8").strip(),
    cors_origins=["http://127.0.0.1:3000"],
    _env_file=None,
)
assert settings.app_env == "test"
submission.datetime = SyntheticSubmissionClock
repo = MockClaimRepository(store_path=OUT / "browser.sqlite3")
app = create_app(settings, repo, business_date_provider=business_date)
