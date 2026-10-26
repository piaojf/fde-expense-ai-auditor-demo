"""Isolated browser acceptance app; synthetic test environment, no paid providers.

Run only after explicit local application-egress/port consent. Reads the ignored
URL file for a newly created test DB; never migrates existing demo databases.
"""

from pathlib import Path

from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3a/browser"
OUT.mkdir(parents=True, exist_ok=True)
url = (OUT / "database-url.txt").read_text(encoding="utf-8").strip()
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
    database_url=url,
    cors_origins=["http://127.0.0.1:3000"],
    ocr_languages="chi_sim+eng",
    ocr_tessdata=str(ROOT / "output/ui-p2/tessdata"),
    _env_file=None,
)
repo = MockClaimRepository(store_path=OUT / "browser.sqlite3")
app = create_app(settings, repo)
