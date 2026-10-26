"""Real HTTP/SQLite checks for challenge consumption and brute-force limits."""

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL.Image import open as open_png

from apps.api.enterprise import CAPTCHA_COOKIE, COOKIE
from apps.api.login_captcha import ALPHABET, generate_code
from tests.test_enterprise import PASSWORD, captcha
from tests.test_enterprise import portal as portal


def body(challenge):
    return dict(company_id="DEMO-CN", username="employee", password=PASSWORD, **challenge)


def post(client, challenge):
    return client.post("/api/v1.5/auth/login", json=body(challenge))


def test_random_codes_and_png_only_response(portal):
    c, _, _, store = portal
    assert len({generate_code() for _ in range(50)}) > 40
    assert all(len(code := generate_code()) == 4 and set(code) <= set(ALPHABET)
               for _ in range(50))
    with patch("apps.api.enterprise_store.generate_code", return_value="2x4b"):
        r = c.get("/api/v1.5/auth/captcha")
    data = r.json()
    assert set(data) == {"captcha_id", "image", "expires_in"}
    assert data["expires_in"] == 120 and "2x4b" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    image = open_png(io.BytesIO(base64.b64decode(data["image"].split(",")[1])))
    assert image.format == "PNG" and image.size == (160, 56)
    row = store.db.execute("SELECT * FROM enterprise_login_captchas").fetchone()
    assert data["captcha_id"] not in row and "2x4b" not in row
    assert c.cookies.get(CAPTCHA_COOKIE) not in row
    assert post(c, dict(captcha_id=data["captcha_id"], captcha_code=" 2X4B ")).status_code == 200
    assert post(c, dict(captcha_id=data["captcha_id"], captcha_code="2x4b")).status_code == 400


@pytest.mark.parametrize("case", ["wrong", "missing", "expired", "refresh", "wrong_password"])
def test_invalid_or_consumed_challenge_never_creates_session(portal, case):
    c, _, _, store = portal
    challenge = captcha(c)
    original = challenge.copy()
    if case == "wrong":
        challenge["captcha_code"] = "zzzz"
    elif case == "missing":
        challenge = {}
    elif case == "expired":
        with store.db:
            store.db.execute("UPDATE enterprise_login_captchas SET expires=0")
    elif case == "refresh":
        captcha(c)
    if case == "wrong_password":
        r = c.post("/api/v1.5/auth/login", json={**body(challenge), "password": "bad"})
        assert r.status_code == 401
    else:
        r = post(c, challenge)
        assert r.status_code == 400 and r.json()["error"]["code"] == "CAPTCHA_INVALID"
    assert COOKIE not in c.cookies
    assert store.db.execute("SELECT COUNT(*) FROM enterprise_sessions").fetchone()[0] == 0
    if case != "missing":
        assert post(c, original).status_code == 400


def test_challenge_bound_to_browser_and_consumed_atomically(portal):
    c, app, _, _ = portal
    challenge = captcha(c)
    with TestClient(app) as stranger:
        assert post(stranger, challenge).status_code == 400
    # Same browser: concurrent submissions can create at most one session.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(c, challenge).status_code, range(2)))
    assert sorted(results) == [200, 400]


def test_missing_challenges_count_toward_account_limit(portal):
    c, _, _, store = portal
    for _ in range(8):
        assert post(c, {}).status_code == 400
    assert post(c, {}).status_code == 429
    assert store.db.execute("SELECT COUNT(*) FROM enterprise_sessions").fetchone()[0] == 0


def test_issue_limit_and_ip_limit_survive_new_store_instance(portal):
    c, _, repo, store = portal
    for _ in range(30):
        assert c.get("/api/v1.5/auth/captcha").status_code == 200
    assert c.get("/api/v1.5/auth/captcha").status_code == 429
    from apps.api.enterprise_store import EnterpriseStore
    restarted = EnterpriseStore(repo)
    with pytest.raises(Exception) as exc:
        restarted.issue_captcha("testclient", "another-browser")
    assert exc.value.code == "CAPTCHA_THROTTLED"
    with store.db:
        store.db.execute("DELETE FROM enterprise_login_attempts")
    for i in range(30):
        r = c.post("/api/v1.5/auth/login", json={**body({}), "username": f"user{i}"})
        assert r.status_code == 400
    assert post(c, {}).status_code == 429


def test_captcha_origin_and_secure_cookie(portal):
    c, app, _, _ = portal
    origin = "http://127.0.0.1:3000"
    r = c.get("/api/v1.5/auth/captcha", headers={"Origin": origin})
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.headers["access-control-allow-origin"] == origin
    assert c.get("/api/v1.5/claims").status_code == 401
    assert c.post("/api/v1.5/auth/captcha", json={}).status_code != 200


def test_default_captcha_cookie_secure(tmp_path):
    from apps.api.config import Settings
    from apps.api.main import create_app
    from apps.api.repository import MockClaimRepository
    repo = MockClaimRepository(store_path=str(tmp_path / "secure.sqlite3"))
    app = create_app(Settings(app_env="test", enterprise_enabled=True, _env_file=None), repo)
    with TestClient(app) as c:
        assert "Secure" in c.get("/api/v1.5/auth/captcha").headers["set-cookie"]
    repo.close()
