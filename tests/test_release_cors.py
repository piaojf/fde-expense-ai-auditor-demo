"""Credentialed enterprise preflight on the production application boundary."""

from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.main import create_app


def test_enterprise_preflight_allows_credentials_only_for_configured_origin():
    with TestClient(
        create_app(Settings(app_env="test", enterprise_enabled=True, _env_file=None))
    ) as c:
        headers = {
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        }
        result = c.options("/api/v1.5/auth/login", headers=headers)
        assert result.status_code == 200
        assert result.headers["access-control-allow-origin"] == headers["Origin"]
        assert result.headers.get("access-control-allow-credentials") == "true"
        rejected = c.options(
            "/api/v1.5/auth/login", headers={**headers, "Origin": "https://untrusted.invalid"}
        )
        assert rejected.status_code in (400, 403)
        assert "access-control-allow-origin" not in rejected.headers


def test_legacy_preflight_remains_without_credentials():
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as c:
        result = c.options(
            "/claims",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        assert result.status_code == 200
        assert "access-control-allow-credentials" not in result.headers
