"""Real Chrome + production UI + HTTP + independent DB acceptance.

No intercepted/mocked API, document, OCR, LLM or rule results. Deterministic
embeddings/Mock LLM and Mock ERP are explicit. Future business dates use the
isolated backend business-date file and Playwright's browser clock; publication
and audit-start time remain real. Requires the user's loopback consent.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.getenv("UI_P3B_OUT", str(ROOT / "output/ui-p3b/browser")))
CLI = Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"
TOKEN = (OUT / "demo-token.txt").read_text(encoding="utf-8").strip()
ENGINE = create_engine((OUT / "database-url.txt").read_text(encoding="utf-8").strip())
BASE = "http://127.0.0.1:3000"


def cli(*args):
    r = subprocess.run(
        [str(CLI), "-s=" + os.getenv("UI_P3B_SESSION", "ui-p3b"), *args],
        capture_output=True, text=True, encoding="utf-8"
    )
    safe = (r.stdout + r.stderr).replace(TOKEN, "[REDACTED]")
    with (OUT / "browser-cli.txt").open("a", encoding="utf-8") as f:
        f.write(safe)
    if r.returncode or "### Error" in r.stdout:
        raise AssertionError(safe)
    return safe


def run(code):
    code = code.replace(
        "output/playwright/ui-p3b-",
        os.getenv("UI_P3B_SHOT_PREFIX", "output/playwright/ui-p3b-"),
    )
    result = cli("run-code", "async () => {" + code + "}")
    match = re.search(r"### Result\n(.*?)(?:\n###|$)", result, re.S)
    return json.loads(match[1]) if match else None


def snap():
    cli("snapshot")


def upload(path):
    snap()
    run(
        'await page.getByLabel("选择制度文件").setInputFiles(' + json.dumps(str(path)) + ");"
        'await page.getByText("文件已保存为 DRAFT",{exact:false}).waitFor();'
    )
    snap()
    uid = run(
        'return await page.locator(".policy-detail .policy-source").first().innerText();'
    ).split("\n")[0]
    run(
        'await page.getByLabel("我已对照原文核对上述元数据").check();'
        'await page.getByRole("button",{name:"确认草稿元数据",exact:true}).click();'
        'await page.getByText("元数据已人工确认并锁定",{exact:false}).waitFor();'
        'await page.getByRole("button",{name:"建立草稿向量索引",exact:true}).click();'
        'await page.getByRole("button",{name:"重复导入（幂等核对）",exact:true}).waitFor();'
    )
    snap()
    run(
        'await page.getByLabel("规则变更原因").fill("逐条核对合成原文，仅限本机测试");'
        'await page.getByLabel("我已对照公司、地区、版本、日期、金额、币种及原始条款确认规则")'
        ".check();"
        'await page.getByRole("button",{name:"确认结构化规则",exact:true}).click();'
        'await page.getByText("结构化规则已人工确认",{exact:false}).waitFor();'
    )
    return uid


def claim(case, day, amount, notes=""):
    cli("goto", BASE + "/claims/new")
    snap()
    fields = {
        "申请人姓名": "UI-P3B 合成申请人 " + case,
        "所属部门": "华南测试部",
        "职位": "客户经理",
        "报销金额（元 / CNY）": amount,
        "费用发生日期": day,
        "报销事项 *": "合成深圳住宿验收",
        "出差开始日期": day,
        "出差结束日期": day[:8] + str(int(day[8:]) + 1).zfill(2),
        "开票单位（销售方）": "深圳合成酒店",
        "备注或特殊情况说明（选填）": notes,
    }
    code = "".join(
        'await page.getByRole("textbox",{name:' + json.dumps(k) + "}).fill(" + json.dumps(v) + ");"
        for k, v in fields.items()
    )
    run(
        code + 'await page.getByLabel("出差城市 *").selectOption("深圳");'
        'await page.getByRole("button",{name:"提交报销并启动审核",exact:true}).click();'
        "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    )
    snap()
    cid = run("return page.url();").split("/")[-1]
    with httpx.Client() as client:
        result = client.get("http://127.0.0.1:8001/api/v1.1/claims/" + cid).json()
    data = result["claim"]
    rule = data["decision_result"]["rule_results"][0]
    evidence = data["decision_result"]["policy_evidence"]
    assert evidence and all(e["policy_version_id"] == rule["policy_version_id"] for e in evidence)
    with sqlite3.connect(OUT / "browser.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM claims WHERE id=?", (cid,)).fetchone()[0] == 1
    run(
        "await page.screenshot({path:"
        + json.dumps("output/playwright/ui-p3b-" + case + ".png")
        + ",fullPage:true});"
    )
    return dict(
        claim_id=cid,
        rule=rule,
        decision=data["decision_result"]["decision"],
        hard_gate_passed=data["decision_result"]["hard_gate_passed"],
        erp_status=data["erp_status"],
    )


def snapshot():
    with ENGINE.connect() as c:
        values = {
            t: c.execute(text(f"SELECT row_to_json(t)::text FROM {t} t ORDER BY 1")).scalars().all()
            for t in (
                "policy_uploads",
                "policy_versions",
                "policy_chunks",
                "policy_rule_reviews",
                "policy_publication_events",
            )
        }
    with sqlite3.connect("file:" + str(OUT / "browser.sqlite3") + "?mode=ro", uri=True) as db:
        values["sqlite"] = ["\n".join(db.iterdump())]
    return {k: hashlib.sha256(json.dumps(v).encode()).hexdigest() for k, v in values.items()}


def main():
    Path("output/playwright").mkdir(exist_ok=True, parents=True)
    cli("goto", BASE + "/policies")
    snap()
    run(
        "page.__p3b={errors:[],failed:[],negativeResponses:[]};"
        'page.on("pageerror",e=>page.__p3b.errors.push(e.message));'
        'page.on("requestfailed",r=>page.__p3b.failed.push(r.url()));'
        'page.on("response",r=>{if(r.status()>=400)page.__p3b.negativeResponses.push({url:r.url(),status:r.status()})});'
        'page.on("request",r=>{if(r.url().endsWith("/publish"))page.__publishBody=JSON.parse(r.postData())});'
        "await page.setViewportSize({width:1440,height:1000});"
        'await page.getByLabel("本机演示凭证").fill(' + json.dumps(TOKEN) + ");"
        'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
        'await page.getByLabel("选择制度文件").waitFor();'
    )
    uid = upload(ROOT / "tests/fixtures/policies/publication-800.docx")
    # Real original download proves file integrity independently of the displayed SHA.
    run(
        'const [download]=await Promise.all([page.waitForEvent("download"),'
        'page.getByRole("button",{name:"下载原文件核对",exact:true}).click()]);'
        "await download.saveAs(" + json.dumps(str(OUT / "download.docx")) + ");"
    )
    assert (OUT / "download.docx").read_bytes() == (
        ROOT / "tests/fixtures/policies/publication-800.docx"
    ).read_bytes()
    run(
        'await page.getByRole("button",{name:"查看发布前差异",exact:true}).click();'
        'await page.getByRole("heading",{name:"发布前预览",exact:true}).waitFor();'
    )
    snap()
    preview = run('return await page.getByTestId("publication-preview").innerText();')
    assert all(v in preview for v in ("700.00", "800.00", "2026-11-01", "2026-11-800", "NOT_RUN"))
    assert run(
        'return await page.getByRole("button",{name:"显式发布制度",exact:true}).isDisabled();'
    )
    run('await page.screenshot({path:"output/playwright/ui-p3b-preview.png",fullPage:true});')
    a = claim("unpublished", "2026-09-20", "780.00")
    assert (
        a["rule"]["limit"] == "700.00"
        and a["rule"]["outcome"] == "FAIL"
        and a["decision"] == "REJECT"
    )
    pending = claim("pending-human", "2026-09-20", "680.00", "人工核实行程")
    assert pending["decision"] == "HUMAN_REVIEW"
    history_before = httpx.get("http://127.0.0.1:8001/claims/" + a["claim_id"]).json()
    pending_before = httpx.get("http://127.0.0.1:8001/claims/" + pending["claim_id"]).json()
    cli("goto", BASE + "/policies")
    snap()
    run(
        'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
        'await page.locator(".policy-list-item").first().click();'
        'await page.getByRole("button",{name:"查看发布前差异",exact:true}).waitFor();'
        'await page.getByRole("button",{name:"查看发布前差异",exact:true}).click();'
        'await page.getByRole("heading",{name:"发布前预览",exact:true}).waitFor();'
    )
    snap()
    with ENGINE.begin() as c:
        c.execute(
            text(
                "CREATE FUNCTION browser_fail_release() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'synthetic late transaction failure'; END; $$"
            )
        )
        c.execute(
            text(
                "CREATE TRIGGER browser_fail_release BEFORE INSERT ON policy_publication_events "
                "FOR EACH ROW EXECUTE FUNCTION browser_fail_release()"
            )
        )
    before_failure = snapshot()
    run(
        'await page.getByLabel("我再次确认上述差异、替代版本及未来生效范围，显式发布").check();'
        'await page.getByRole("button",{name:"显式发布制度",exact:true}).click();'
        'await page.getByRole("alert").filter({hasText:"事务失败"}).waitFor();'
    )
    assert snapshot() == before_failure
    with ENGINE.begin() as c:
        c.execute(text("DROP TRIGGER browser_fail_release ON policy_publication_events"))
        c.execute(text("DROP FUNCTION browser_fail_release()"))
    snap()
    run(
        'await page.getByRole("button",{name:"显式发布制度",exact:true}).click();'
        'await page.getByText("发布已持久化",{exact:false}).waitFor();'
    )
    duplicate = run(
        "const b=page.__publishBody; const r=await page.evaluate(async b=>{"
        'const r=await fetch("http://127.0.0.1:8001/api/v1.4/policies/' + uid + '/publish",'
        '{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        "return {status:r.status,body:await r.json()};},b);return r;"
    )
    assert duplicate["status"] == 200
    pid = duplicate["body"]["policy"]["policy_version_id"]
    with ENGINE.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM policy_publication_events")) == 1
        assert (
            c.scalar(
                text("SELECT count(*) FROM policy_chunks WHERE policy_version_id=:p"), dict(p=pid)
            )
            == 1
        )
    # Test clock is confined to synthetic claim dates, not the publication/API contract.
    (OUT / "synthetic-business-date.txt").write_text("2026-11-03", encoding="utf-8")
    run('await page.clock.setFixedTime(new Date("2026-11-03T06:00:00Z"));')
    b = claim("old-date", "2026-10-20", "780.00")
    c = claim("new-date", "2026-11-02", "780.00")
    d = claim("new-over-limit", "2026-11-02", "850.00")
    assert b["rule"]["limit"] == "700.00" and b["decision"] == "REJECT"
    assert c["rule"]["limit"] == "800.00" and c["rule"]["outcome"] == "PASS"
    assert c["decision"] == "APPROVE" and c["hard_gate_passed"] and c["erp_status"] == "SUCCEEDED"
    assert (
        d["rule"]["outcome"] == "FAIL"
        and d["decision"] == "REJECT"
        and d["erp_status"] == "NOT_STARTED"
    )
    assert httpx.get("http://127.0.0.1:8001/claims/" + a["claim_id"]).json() == history_before
    assert httpx.get("http://127.0.0.1:8001/claims/" + pending["claim_id"]).json() == pending_before
    # Genuine conflicting source version; the UI must expose a blocker instead of publish.
    buf = io.BytesIO()
    with (
        zipfile.ZipFile(ROOT / "tests/fixtures/policies/publication-800.docx") as src,
        zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for entry in src.infolist():
            data = src.read(entry)
            if entry.filename == "word/document.xml":
                data = data.replace(b"2026-11-800", b"conflict-800")
            dst.writestr(entry.filename, data)
    conflict = OUT / "conflict-800.docx"
    conflict.write_bytes(buf.getvalue())
    cli("goto", BASE + "/policies")
    snap()
    run(
        'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
        'await page.getByLabel("选择制度文件").waitFor();'
    )
    conflict_uid = upload(conflict)
    run(
        'await page.getByRole("button",{name:"查看发布前差异",exact:true}).click();'
        'await page.getByRole("alert").filter({hasText:"拒绝发布"}).waitFor();'
        'await page.screenshot({path:"output/playwright/ui-p3b-conflict.png",fullPage:true});'
    )
    blocker = run('return await page.getByTestId("publication-preview").innerText();')
    assert "POLICY_CONFLICT" in blocker
    before_restart = snapshot()
    subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m",
         os.getenv("UI_P3B_SERVICES", "scripts.ui_p3b_services"), "restart"], check=True
    )
    assert snapshot() == before_restart
    cli("goto", BASE + "/policies")
    snap()
    run(
        'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
        'await page.locator(".policy-list-item").filter({hasText:"2026-11-800"}).click();'
        'await page.getByText("此来源已正式进入本机受控版本库",{exact:false}).waitFor();'
        'await page.getByText("版本发布审计与未来退休 / 回退",{exact:true}).click();'
        'await page.screenshot({path:"output/playwright/ui-p3b-after-restart.png",fullPage:true});'
    )
    # Controlled rollback also uses preview then explicit confirmation through the real UI.
    run(
        'await page.getByLabel("规则变更原因").fill("合成验收：12月起恢复700，保留11月历史");'
        'await page.getByLabel("退休回退生效日期").fill("2026-12-01");'
        'await page.getByRole("button",{name:"预览退休或回退",exact:true}).click();'
        'await page.getByLabel("我确认未来退休或回退范围并保留历史").waitFor();'
    )
    snap()
    run(
        'await page.getByLabel("我确认未来退休或回退范围并保留历史").check();'
        'await page.getByRole("button",{name:"确认退休或回退",exact:true}).click();'
        'await page.getByText("未来退休或回退已记录",{exact:false}).waitFor();'
    )
    assert (
        httpx.get("http://127.0.0.1:8001/claims/" + c["claim_id"]).json()["decision_result"][
            "rule_results"
        ][0]["limit"]
        == "800.00"
    )
    run(
        "await page.setViewportSize({width:390,height:844});"
        'await page.screenshot({path:"output/playwright/ui-p3b-mobile.png",fullPage:true});'
    )
    assert run("return await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);")
    console = run("return page.__p3b;")
    assert not console["errors"] and not console["failed"], console
    assert (
        len(console["negativeResponses"]) == 1 and console["negativeResponses"][0]["status"] == 503
    ), console
    with sqlite3.connect(OUT / "browser.sqlite3") as db:
        effects = db.execute("SELECT count(*) FROM erp_effects").fetchone()[0]
        assert effects == 1
    with ENGINE.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM policy_publication_events")) == 2
    report = dict(
        status="PASS_OFFLINE",
        upload_id=uid,
        policy_id=pid,
        conflict_upload_id=conflict_uid,
        cases=dict(A=a, B=b, C=c, D=d),
        pending_preserved=pending,
        late_failure_atomic=True,
        duplicate_event_id=duplicate["body"]["event_id"],
        restart_before=before_restart,
        restart_after=before_restart,
        rollback="PASS",
        console=console,
        erp_effects=effects,
        future_dates=(
            "Synthetic backend/browser business clock; audit-start/publication timestamps real"
        ),
        real_bge_m3="NOT_RUN",
        paid_http_calls=0,
    )
    (OUT / "browser-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ENGINE.dispose()
    print("BROWSER_E2E=PASS_OFFLINE; real BGE-M3/DeepSeek NOT_RUN; one Mock ERP effect")


if __name__ == "__main__":
    main()
