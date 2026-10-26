"""Real Chrome E2E via the repository's installed Playwright CLI workflow.

No route mocks. Decisions use visible React forms and actual HTTP. Read-only
SQLite queries independently verify ERP effects and persisted actor/version.
"""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

from scripts.finance_workbench_demo import API, OUT, ROOT, WEB, active, save
from tests.test_enterprise import PASSWORD

GEN = active()
CASES = json.loads((GEN / "cases.json").read_text(encoding="utf-8"))
CACHED_CLI = sorted(
    (Path.home() / ".npm/_npx").glob("*/node_modules/@playwright/cli/playwright-cli.js")
)
CLI = (
    [shutil.which("node"), str(CACHED_CLI[0])]
    if CACHED_CLI
    else [str(Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh")]
)
SESSION = "p4b-finance-e2e"
RESULTS = []
EXPECTED_FAILURES = []
CLIENTS = {}
SHOTS = OUT / "screenshots"
SHOTS.mkdir(exist_ok=True)


def cli(*args):
    proc = subprocess.run(
        [*CLI, "-s=" + SESSION, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=75,
    )
    output = proc.stdout + proc.stderr
    with (GEN / "browser-cli.txt").open("a", encoding="utf-8") as f:
        f.write(output)
    if proc.returncode or "### Error" in output:
        raise AssertionError(output[-3000:])
    return output


def run(code):
    output = cli("run-code", "async () => {" + code + "}")
    match = re.search(r"### Result\n(.*?)(?:\n###|$)", output, re.S)
    return json.loads(match[1]) if match else None


def check(name, evidence):
    RESULTS.append(dict(name=name, status="PASS", evidence=evidence))
    save(OUT / "browser-results.json", dict(tests=RESULTS, generation=str(GEN)))
    print("PASS", name, flush=True)


def snap():
    cli("snapshot")


def shot(name):
    targets = {
        "02-normal-hotel": 'page.locator(".p4b-report-header")',
        "03-policy-source": "page.getByText(/^已重新核对原文/)",
        "04-ai-advisory": 'page.getByRole("region",{name:"AI 审核建议",exact:true}).locator("h4")',
        "05-erp-approved-refresh": 'page.getByText("财务状态：审核通过",{exact:false})',
        "06-ai-failure-handoff": "page.getByText(/^AI审核建议不可靠，需要财务独立核查/)",
        "07-missing-blocked": 'page.locator(".p4b-report-header")',
        "07-overlimit-blocked": 'page.locator(".p4b-report-header")',
    }
    target = targets.get(name)
    if name.endswith("-decision"):
        target = 'page.locator(".portal-finance-form")'
    if target:
        run("await " + target + ".scrollIntoViewIfNeeded();")
    snap()
    run(
        "await page.screenshot({path:"
        + json.dumps(str(SHOTS / (name + ".png")))
        + ",fullPage:true});"
    )


def effect(name):
    with sqlite3.connect("file:" + str(GEN / "portal.sqlite3") + "?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT count(*) FROM erp_effects WHERE claim_id=?", (CASES[name]["claim_id"],)
        ).fetchone()[0]


def client(username="finance"):
    if username in CLIENTS:
        return CLIENTS[username]
    c = httpx.Client(base_url=API, timeout=30)
    response = c.post(
        "/api/v1.5/auth/login",
        json=dict(company_id="DEMO-CN", username=username, password=PASSWORD),
    )
    assert response.status_code == 200, response.text
    CLIENTS[username] = c
    return c


def detail(name, c=None):
    c = c or client()
    response = c.get("/api/v1.5/claims/" + CASES[name]["claim_id"])
    assert response.status_code == 200, response.text
    return response.json()


def login(username):
    run("await page.context().clearCookies(); await page.goto(" + json.dumps(WEB + "/login") + ");")
    snap()
    run(
        'await page.getByLabel("账号",{exact:true}).fill(' + json.dumps(username) + ");"
        'await page.getByLabel("密码",{exact:true}).fill(' + json.dumps(PASSWORD) + ");"
        'await page.getByRole("button",{name:"登录",exact:true}).click();'
        "await page.waitForURL(/workbench|my-claims/);"
    )
    snap()


def open_claim(name, employee=False):
    run(
        "await page.goto("
        + json.dumps(WEB + ("/my-claims" if employee else "/company/claims"))
        + ");"
    )
    snap()
    cid = CASES[name]["claim_id"]
    run(
        'await page.getByRole("row").filter({has:page.getByLabel('
        + json.dumps("单号 " + cid)
        + ',{exact:true})}).getByRole("button").click();'
        'await page.getByRole("region",{name:"报销进度与详情"}).waitFor();'
    )
    snap()


def manual(name):
    run('await page.getByText("记录本报告的独立人工核查",{exact:true}).click();')
    snap()
    run(
        'await page.getByLabel("人工核查结论").selectOption("CLEAR");'
        'await page.getByLabel("是否需要例外授权").selectOption("false");'
        'await page.getByLabel("独立材料来源").fill("SYNTHETIC-INDEPENDENT-FINANCE-REVIEW");'
        'await page.getByLabel("逐项人工核查依据").fill("独立核对合成票据、住宿订单、'
        '业务批准、制度与重复排查；无未决事项、无需例外付款。");'
        'await page.getByRole("checkbox",{name:/已知 AI 可能失败/}).check();'
        'const response = page.waitForResponse(r=>r.url().endsWith("/manual-review")'
        ' && r.request().method()==="POST");'
        'await page.getByRole("button",{name:"记录人工核查",exact:true}).click();'
        "const r=await response; if(r.status()!==200) throw new Error(await r.text());"
        'await page.getByText("独立人工核查 · 人工核查无未决事项",{exact:true}).waitFor();'
    )
    report = detail(name)["advisory_analysis"]
    assert report["manual_review_current"] and report["manual_review"]["outcome"] == "CLEAR"
    assert effect(name) == 0


def decide(name, action):
    reason = "合成浏览器财务" + action + "：独立材料核查已记录。"
    run(
        'await page.getByLabel("处理方式").selectOption(' + json.dumps(action) + ");"
        'await page.getByLabel("审核说明 / 退回或不通过理由 *").fill(' + json.dumps(reason) + ");"
    )
    shot(name + "-decision")
    result = run(
        'const waiting=page.waitForResponse(r=>r.url().endsWith("/finance-action")'
        ' && r.request().method()==="POST");'
        'await page.getByRole("button",{name:"确认审核决定",exact:true}).click();'
        "const r=await waiting; const d=await r.json(); return {status:r.status(),"
        "request:r.request().postDataJSON(),stage:d.stage,version:d.version,revision:d.revision};"
    )
    assert result["status"] == 200, result
    save(GEN / (name + "-finance-request.json"), result)
    expected = dict(APPROVE="APPROVED", REJECT="REJECTED", RETURN="RETURNED")[action]
    current = detail(name)
    assert current["stage"] == expected, current["stage"]
    assert current["reason"] == reason
    events = [e for e in current["timeline"] if e["kind"] == "FINANCE_" + action]
    assert len(events) == 1 and events[0]["role"] == "FINANCE"
    assert events[0]["version"] == current["version"]
    actor = client().get("/api/v1.5/auth/me").json()["user"]
    assert events[0]["actor_id"] == actor["id"]
    assert result["request"]["expected_revision"] == current["revision"] - 1
    save(GEN / (name + "-audit-summary.json"), events)
    expected_label = {
        "APPROVED": "审核通过",
        "REJECTED": "审核不通过",
        "RETURNED": "已退回 · 待修改",
    }[expected]
    run(
        "await page.getByText("
        + json.dumps("财务状态：" + expected_label)
        + ",{exact:false}).waitFor();"
    )
    return result


def browser_post(path, body):
    result = run(
        "const r=await page.evaluate(async ({url,body})=>{const r=await fetch(url,{"
        'method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},'
        "body:JSON.stringify(body)});const d=await r.json();return {status:r.status,"
        "code:d.error?.code,stage:d.stage};},{url:"
        + json.dumps(API + "/api/v1.5" + path)
        + ",body:"
        + json.dumps(body, ensure_ascii=False)
        + "}); return r;"
    )
    if result["status"] >= 400:
        EXPECTED_FAILURES.append((API + "/api/v1.5" + path, result["status"]))
    return result


def forged_approval(name):
    return dict(
        action="APPROVE",
        reason="合成对抗测试：不得放行",
        expected_revision=detail(name)["revision"],
        idempotency_key=str(uuid4()),
    )


def main():
    cli("open", WEB, "--browser", "chrome")
    snap()
    cli("tracing-start")
    run(
        "page.__p4b={errors:[],console:[],requests:[]};"
        'page.on("pageerror",e=>page.__p4b.errors.push(String(e)));'
        'page.on("console",m=>{if(m.type()==="error") page.__p4b.console.push('
        "{text:m.text(),url:m.location().url})});"
        'page.on("response",r=>{if(r.url().startsWith(' + json.dumps(API) + "))"
        " page.__p4b.requests.push({url:r.url(),method:r.request().method(),status:r.status()})});"
    )
    login("finance")
    shot("01-workbench")
    check("01_workbench", "实际登录与dashboard HTTP，企业工作台heading")
    open_claim("normal")
    check("02_claim_list", "实际列表行定位申请ID并打开详情")
    d = detail("normal")
    a = d["advisory_analysis"]
    assert d["fields"]["amount"] == "680.00" and d["fields"]["city"] == "深圳"
    assert d["fields"]["incurred_on"] == "2026-11-02"
    assert d["compliance_analysis"]["trusted_facts"]["grade"] == "D"
    assert a["rule_results"][0]["limit"] == "800.00" and a["deterministic_status"] == "PASS"
    text = run('return await page.getByRole("region",{name:"报销进度与详情"}).innerText();')
    for token in ("680.00", "800.00", "2026-11-02", "深圳", "职级 D", "确定性审核 符合"):
        assert token in text, token
    shot("02-normal-hotel")
    check("03_normal_hotel", "浏览器D/深圳/日期/680/800/PASS与后端已发布222版本一致")
    run("await page.getByText(/^查看实际检索原文/).click();")
    snap()
    run(
        'await page.getByRole("button",{name:"核对原文来源",exact:true}).first().click();'
        "await page.getByText(/^已重新核对原文/).waitFor();"
    )
    source_text = run(
        'return await page.getByRole("region",{name:"混合合规审核报告"}).innerText();'
    )
    assert "65043838143a8d043130881b8fda35fd2573ed7fcd8716f1a33fc72f32bf1a98" in source_text
    assert "V2" in source_text
    assert d["compliance_analysis"]["supporting_evidence"][0]["source_text"] in source_text
    assert "第三十九条" in source_text and "住宿费需提交入住信息或酒店订单" in source_text
    shot("03-policy-source")
    check("04_policy_original", "点击来源按钮触发实际evidence GET，222 SHA与V2及原文可见")
    run("await page.getByText(/^查看实际检索原文/).click();")
    advice = run(
        'return await page.getByRole("region",{name:"AI 审核建议",exact:true}).innerText();'
    )
    assert a["risk_level"] == "LOW" and a["recommendation"] == "RECOMMEND_APPROVAL"
    assert "风险：低" in advice and "建议批准" in advice and "一般提示" in advice
    assert "mock" in advice and "最终审批" in advice and "财务决定 PENDING" in advice
    shot("04-ai-advisory")
    check("05_mock_ai_advisory", "Mock/LOW/INFO/建议批准仅为建议，财务PENDING")
    assert effect("normal") == 0
    check("06_preapproval_erp_zero", "SQLite erp_effects COUNT=0")
    manual("normal")
    approval = decide("normal", "APPROVE")
    assert effect("normal") == 1
    check("07_finance_approve", approval)
    run("await page.reload();")
    open_claim("normal")
    ui_text = run('return await page.locator("main").innerText();')
    assert "财务状态：审核通过" in ui_text
    assert "ERP Mock：已回写" in ui_text
    assert detail("normal")["erp_sync_status"] == "SUCCEEDED"
    progress = run('return await page.locator(".portal-progress").innerText();')
    assert "财务决定已记录" in progress and "等待人工核查" not in progress
    check("19_approved_progress", "真实刷新后工作流进度显示财务决定已记录")
    shot("05-erp-approved-refresh")
    check("08_refresh_persistence", "真实page.reload后财务APPROVED及ERP状态持久化")
    replay = browser_post(
        "/claims/" + CASES["normal"]["claim_id"] + "/finance-action", approval["request"]
    )
    assert replay["status"] == 200 and effect("normal") == 1, replay
    check("09_idempotent_replay", "同申请/同请求/同幂等键真实HTTP重放，SQLite COUNT=1")
    open_claim("reject")
    decide("reject", "REJECT")
    assert effect("reject") == 0
    assert (
        browser_post(
            "/claims/" + CASES["reject"]["claim_id"] + "/finance-action", forged_approval("reject")
        )["status"]
        == 409
    )
    check("10_reject", "浏览器REJECT，持久化理由/actor/version；ERP=0，不能再批准")
    open_claim("return")
    decide("return", "RETURN")
    assert effect("return") == 0
    assert (
        browser_post(
            "/claims/" + CASES["return"]["claim_id"] + "/finance-action", forged_approval("return")
        )["status"]
        == 409
    )
    check("11_return", "浏览器RETURN，RETURNED理由持久化；ERP=0，不能强行批准")
    open_claim("ai_failure")
    failed = detail("ai_failure")["advisory_analysis"]
    assert failed["ai_advisory_status"] == "PARTIAL" and failed["recommendation"] == "UNDETERMINED"
    assert any(e["code"] == "UNSUPPORTED_EVIDENCE_REQUIREMENT" for e in failed["validation_errors"])
    failure_ui = run(
        'return await page.getByRole("region",{name:"AI 审核建议",exact:true}).innerText();'
    )
    assert "AI审核建议不可靠，需要财务独立核查" in failure_ui and "建议校验通过" not in failure_ui
    assert effect("ai_failure") == 0
    assert (
        browser_post(
            "/claims/" + CASES["ai_failure"]["claim_id"] + "/finance-action",
            forged_approval("ai_failure"),
        )["status"]
        == 409
    )
    shot("06-ai-failure-handoff")
    manual("ai_failure")
    assert detail("ai_failure")["advisory_analysis"]["ai_advisory_status"] == "PARTIAL"
    assert effect("ai_failure") == 0
    check(
        "12_ai_failure_handoff",
        "合成unsupported missing_evidence被Validator拒绝；独立人工核查仍不批准/写ERP",
    )
    for name in ("missing", "overlimit"):
        open_claim(name)
        assert run(
            'return await page.locator("#finance-decision option[value=APPROVE]").isDisabled();'
        )
        blocked = browser_post(
            "/claims/" + CASES[name]["claim_id"] + "/finance-action", forged_approval(name)
        )
        assert blocked["status"] == 409 and effect(name) == 0, blocked
        if name == "missing":
            assert "invoice_verified" in run('return await page.locator("main").innerText();')
        shot("07-" + name + "-blocked")
        check("13_" + name + "_hard_gate", blocked)
    login("employee")
    open_claim("normal", employee=True)
    assert run('return await page.locator("#finance-decision").count();') == 0
    denied = browser_post(
        "/claims/" + CASES["revoked"]["claim_id"] + "/finance-action", forged_approval("revoked")
    )
    assert denied["status"] == 403 and effect("revoked") == 0, denied
    check("14_employee_permission", "员工无财务表单，构造请求后端403；ERP=0")
    open_claim("return", employee=True)
    assert "合成浏览器财务RETURN" in run('return await page.locator("main").innerText();')
    check("15_employee_return_visible", "员工实际登录后可见RETURNED及退回理由")
    login("revoked")
    admin = client("admin")
    users = admin.get("/api/v1.5/users").json()["items"]
    actor = next(u for u in users if u["username"] == "revoked")
    disabled = admin.post("/api/v1.5/users/" + actor["id"] + "/enabled", json=dict(enabled=False))
    assert disabled.status_code == 200, disabled.text
    denied = browser_post(
        "/claims/" + CASES["revoked"]["claim_id"] + "/finance-action", forged_approval("revoked")
    )
    assert denied["status"] in (401, 403) and effect("revoked") == 0, denied
    check("16_revoked_old_session", denied)
    login("finance")
    run(
        "await page.goto(" + json.dumps(WEB + "/company/audit") + ");"
        'await page.getByText("财务审核通过",{exact:true}).first().waitFor();'
    )
    shot("08-audit-trail")
    audit = client().get("/api/v1.5/audit").json()
    events = [e for e in audit["events"] if e.get("claim_id") == CASES["normal"]["claim_id"]]
    assert any(e["kind"] == "FINANCE_APPROVE" for e in events)
    save(OUT / "audit-summary.json", events)
    check("17_audit", "浏览器审计页+实际audit GET校验FINANCE_APPROVE actor及申请")
    evidence = run("return page.__p4b;")
    save(OUT / "api-summary.json", evidence)
    assert not evidence["errors"], evidence["errors"]
    unexpected = [
        r
        for r in evidence["requests"]
        if r["status"] >= 400
        and not (r["status"] == 401 and r["url"].endswith("/auth/me"))
        and (r["url"], r["status"]) not in EXPECTED_FAILURES
    ]
    assert not unexpected, unexpected
    for error in evidence["console"]:
        assert error["text"].startswith(
            "Failed to load resource: the server responded with a status of "
        ), error
        match = re.search(r"status of (\d+)", error["text"])
        status = int(match[1])
        assert (error["url"], status) in EXPECTED_FAILURES or (
            status == 401 and error["url"].endswith("/auth/me")
        ), error
    check("18_console", "无pageerror/CORS/非预期请求错误；预期401/403/409单列")
    save(OUT / "mock-erp-records.json", {name: effect(name) for name in CASES})
    cli("tracing-stop")
    print("Browser E2E:", len(RESULTS), "passed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        save(OUT / "browser-failure.json", dict(error=str(exc), passed=RESULTS))
        print("BROWSER FAILURE", str(exc)[-3000:], flush=True)
        try:
            shot("failure")
            cli("tracing-stop")
        finally:
            raise
