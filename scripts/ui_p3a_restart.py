"""Restart only the recorded UI-P3A backend; assert durable real HTTP/SQL state."""

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3a/browser"
CLI = Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"
SESSION = "ui-p3a"
TOKEN = (OUT / "demo-token.txt").read_text(encoding="utf-8").strip()
URL = (OUT / "database-url.txt").read_text(encoding="utf-8").strip()
assert (make_url(URL).database or "").startswith("fde_m0_test_ui_p3a_browser_")
engine = create_engine(URL)


def cli(*args):
    result = subprocess.run(
        [str(CLI), "-s=" + SESSION, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    safe = (result.stdout + result.stderr).replace(TOKEN, "[REDACTED]")
    with (OUT / "restart-browser-cli.txt").open("a", encoding="utf-8") as log:
        log.write(safe)
    if result.returncode or "### Error" in result.stdout:
        raise AssertionError(safe)
    return safe


def run(code):
    result = cli("run-code", "async () => {" + code + "}")
    print(result, flush=True)
    return result


def logical_snapshot():
    result = {}
    with engine.connect() as c:
        for table, key in [
            ("policy_uploads", "upload_id"),
            ("policy_versions", "policy_version_id"),
            ("policy_chunks", "chunk_id"),
        ]:
            rows = c.execute(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY {key}"))
            encoded = json.dumps(rows.scalars().all()).encode()
            result[table] = hashlib.sha256(encoded).hexdigest()
    with sqlite3.connect("file:" + str(OUT / "browser.sqlite3") + "?mode=ro", uri=True) as c:
        result["sqlite"] = hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
    return result


def main():
    before = logical_snapshot()
    manifest = json.loads((OUT / "processes.json").read_text(encoding="utf-8"))
    previous = manifest["backend"]
    assert previous["owned"] and previous["cwd"] == str(ROOT)
    actual = subprocess.check_output(
        ["ps", "-p", str(previous["pid"]), "-o", "command="], text=True, encoding="utf-8"
    )
    assert "scripts.ui_p3a_runtime:app" in actual and "--port 8001" in actual
    assert "uvicorn" in actual and str(ROOT / ".venv/bin/python") in actual
    os.kill(previous["pid"], signal.SIGTERM)
    for _ in range(80):
        r = subprocess.run(["lsof", "-nP", "-iTCP:8001", "-sTCP:LISTEN"], capture_output=True)
        if r.returncode:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Owned backend did not stop gracefully; no forced termination")
    env = os.environ.copy()
    env.update(
        APP_ENV="test",
        POLICY_RETRIEVER="inmemory",
        LLM_PROVIDER="mock",
        POLICY_DEMO_ENABLED="false",
        POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0",
        MOCK_STORE_PATH=str(OUT / "import-safety.sqlite3"),
    )
    with (OUT / "backend-after-restart.log").open("ab") as log:
        p = subprocess.Popen(
            previous["command"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    manifest["previous_backend"] = previous
    manifest["backend"] = {**previous, "pid": p.pid}
    (OUT / "processes.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for _ in range(40):
        try:
            assert httpx.get("http://127.0.0.1:8001/health", timeout=2).status_code == 200
            break
        except (httpx.HTTPError, AssertionError):
            time.sleep(0.25)
    else:
        raise RuntimeError("Restarted backend not ready")
    assert logical_snapshot() == before
    with engine.connect() as c:
        ordered_ids = (
            c.execute(
                text(
                    "SELECT upload_id FROM policy_uploads WHERE state='INDEXED' "
                    "ORDER BY created_at DESC"
                )
            )
            .scalars()
            .all()
        )
    assert len(ordered_ids) == 3
    cli("goto", "http://127.0.0.1:3000/policies")
    cli("snapshot")
    run(
        "await page.setViewportSize({width:1440,height:1000});"
        'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
        'await page.getByLabel("选择制度文件").waitFor();'
    )
    tested = []
    for index, uid in enumerate(ordered_ids):
        cli("snapshot")
        result = run(
            'await page.locator(".policy-list-item").nth(' + str(index) + ").click();"
            'await page.waitForFunction(id=>document.querySelector(".policy-detail '
            '.policy-source")?.innerText.startsWith(id),' + json.dumps(uid) + ");"
            'if(await page.getByLabel("公司",{exact:true}).inputValue()!=="DEMO-CN")'
            'throw Error("Company not restored");'
            'if(await page.getByLabel("地区",{exact:true}).inputValue()!=="深圳")'
            'throw Error("Confirmed region not restored");'
            'if(!(await page.getByLabel("公司",{exact:true}).isDisabled()))'
            'throw Error("Metadata not frozen after restart");'
            'await page.getByRole("button",{name:"仅检索此草稿制度",exact:true}).click();'
            'await page.getByRole("heading",{name:"草稿检索结果 · 1 条",exact:true}).waitFor();'
            'const result=await page.locator(".policy-results").innerText();'
            'if(!result.includes("800 per night")||!result.includes(' + json.dumps(uid) + "))"
            'throw Error("Post-restart retrieval failed");return result;'
        )
        assert "deterministic-test-only-v1" in result
        tested.append(uid)
    cli("snapshot")
    run('await page.screenshot({path:"output/playwright/ui-p3a-after-restart.png",fullPage:true});')
    browser = json.loads((OUT / "browser-report.json").read_text(encoding="utf-8"))
    cli("goto", "http://127.0.0.1:3000/claims/" + browser["claim_id"])
    cli("snapshot")
    run(
        'await page.getByText("高风险",{exact:true}).waitFor();'
        'await page.getByText("未写入",{exact:true}).waitFor();'
        'await page.screenshot({path:"output/playwright/ui-p3a-780-after-restart.png",'
        "fullPage:true});"
    )
    with sqlite3.connect(OUT / "browser.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM erp_effects").fetchone()[0] == 0
    with engine.connect() as c:
        assert (
            c.scalar(
                text(
                    "SELECT authoritative_rules->>'hotel_limit_per_night' FROM "
                    "policy_versions WHERE policy_version_id='POL-SZ-2026'"
                )
            )
            == "700.00"
        )
        assert (
            c.scalar(
                text(
                    "SELECT count(*) FROM policy_versions WHERE source_uri LIKE "
                    "'upload://%' AND status<>'DRAFT'"
                )
            )
            == 0
        )
    assert logical_snapshot() == before
    # Capture post-restart page/HTTP errors; the two earlier negative uploads remain expected.
    result = run("return page.__p3a;")
    match = re.search(r"### Result\n(.*?)(?:\n###|$)", result, re.S)
    assert match
    console = json.loads(match[1])
    assert not console["errors"] and not console["failed"]
    assert len(console["responses"]) == 2 and all(r["status"] == 422 for r in console["responses"])
    report = dict(
        status="PASS",
        before=before,
        after=logical_snapshot(),
        restored_ids=tested,
        previous_pid=previous["pid"],
        restarted_pid=p.pid,
        erp_effects=0,
        formal_limit="700.00",
        no_uploaded_active_policy=True,
        console=console,
    )
    (OUT / "restart-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    engine.dispose()
    print("UI_P3A_SERVICE_RESTART_AND_RETRIEVAL=PASS", flush=True)


if __name__ == "__main__":
    main()
