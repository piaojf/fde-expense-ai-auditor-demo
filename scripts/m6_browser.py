"""Real Chrome UI + HTTP + PG/SQLite verification for the three M6 demos."""

import json
import os
import sqlite3
from uuid import uuid4

import httpx

from scripts.m6_demo import OUT, ROOT, active, private_json

GEN = active()
os.environ.update(
    UI_P3B_OUT=str(GEN),
    UI_P3B_SESSION="m6",
    UI_P3B_SERVICES="scripts.m6_demo",
    UI_P3B_SHOT_PREFIX="output/playwright/m6-policy-",
)

from scripts import ui_p3b_browser as ui  # noqa: E402


def get_claim(cid):
    response = httpx.get("http://127.0.0.1:8001/claims/" + cid)
    response.raise_for_status()
    return response.json()


def events(cid):
    response = httpx.get("http://127.0.0.1:8001/claims/" + cid + "/audit-log?limit=100")
    response.raise_for_status()
    return [e["event_type"] for e in response.json()["items"]]


def effect_count(cid):
    with sqlite3.connect("file:" + str(GEN / "browser.sqlite3") + "?mode=ro", uri=True) as db:
        return db.execute("SELECT count(*) FROM erp_effects WHERE claim_id=?", (cid,)).fetchone()[0]


def shot(name):
    ui.snap()
    ui.run(
        "await page.screenshot({path:"
        + json.dumps("output/playwright/m6-" + name + ".png")
        + ",fullPage:true});"
    )


def invoice():
    ui.cli("goto", ui.BASE + "/claims/new")
    ui.snap()
    values = {
        "申请人姓名": "M6 合成 OCR 申请人",
        "所属部门": "演示销售部",
        "职位": "客户经理",
        "报销金额（元 / CNY）": "111.00",
        "费用发生日期": "2026-09-19",
        "报销事项 *": "合成票据住宿演示",
        "出差开始日期": "2026-09-20",
        "出差结束日期": "2026-09-21",
        "开票单位（销售方）": "识别前占位单位",
    }
    ui.run(
        "".join(
            'await page.getByRole("textbox",{name:'
            + json.dumps(k)
            + "}).fill("
            + json.dumps(v)
            + ");"
            for k, v in values.items()
        )
        + 'await page.getByLabel("出差城市 *").selectOption("深圳");'
        + 'await page.getByLabel("选择发票附件（选填）").setInputFiles('
        + json.dumps(str(ROOT / "tests/fixtures/invoices/scan-chinese.png"))
        + ");"
        + 'await page.getByRole("button",{name:"核对并确认票据信息"})'
        ".waitFor({timeout:60000});"
    )
    ui.snap()
    fields = ui.run(
        'return {amount:await page.getByLabel("报销金额（元 / CNY）").inputValue(),'
        'date:await page.getByLabel("费用发生日期").inputValue(),'
        'seller:await page.getByLabel("开票单位（销售方）").inputValue()};'
    )
    assert fields == dict(amount="600.00", date="2026-09-20", seller="合成深圳酒店"), fields
    ui.run(
        'await page.getByRole("button",{name:"提交报销并启动审核"}).click();'
        'await page.getByRole("alert").filter({hasText:"请先核对并确认"}).waitFor();'
        'await page.getByLabel("购买方名称",{exact:true}).fill("合成人工纠正公司");'
        'await page.getByLabel("票据差异或修正说明（有提示时必填）")'
        '.fill("M6 合成案例：对照原始像素修正购买方，人工复核");'
        'await page.getByRole("button",{name:"核对并确认票据信息"}).click();'
        'await page.getByRole("button",{name:"已人工确认票据"}).waitFor();'
    )
    shot("ocr-confirmed")
    ui.run(
        'await page.getByRole("button",{name:"提交报销并启动审核"}).click();'
        "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
        'await page.getByRole("link",{name:"进入人工复核"}).waitFor();'
    )
    cid = ui.run("return page.url();").split("/")[-1]
    assert get_claim(cid)["status"] == "PENDING_HUMAN" and effect_count(cid) == 0
    shot("ocr-pending-human")
    ui.run('await page.getByRole("link",{name:"进入人工复核"}).click();')
    ui.snap()
    ui.run(
        'await page.getByRole("textbox").last().fill("对照合成扫描和更正记录，批准本机演示");'
        'await page.getByRole("button",{name:"通过（Approve）",exact:true}).click();'
        'await page.waitForURL(url=>!url.pathname.endsWith("/review"));'
    )
    ui.snap()
    ui.run(
        "await page.reload();"
        'await page.getByRole("button",{name:"⟳ 恢复处理（Resume）"}).click();'
        'await page.getByText("mock: APPROVED",{exact:true}).waitFor();'
    )
    shot("ocr-resumed")
    assert get_claim(cid)["status"] == "COMPLETED" and effect_count(cid) == 1
    assert {
        "CLAIM_CREATED",
        "INVOICE_CONFIRMED",
        "HUMAN_REVIEW_SUBMITTED",
        "WORKFLOW_RESUMED",
    } <= set(events(cid))
    with sqlite3.connect(GEN / "browser.sqlite3") as db:
        evidence = json.loads(
            db.execute("SELECT body FROM invoice_evidence WHERE claim_id=?", (cid,)).fetchone()[0]
        )
        assert evidence["attachment"]["ocr"]["methods"] == ["tesseract"]
        assert evidence["attachment"]["ocr"]["fields"]["buyer_name"]["value"] == "合成演示公司"
        assert evidence["confirmed_fields"]["buyer_name"] == "合成人工纠正公司"
    return dict(
        claim_id=cid,
        autofill=fields,
        ocr="real local tesseract",
        erp_effects=1,
        human_review="PASS",
        evidence_original_preserved=True,
    )


def recovery_and_replay():
    ui.run(
        'page.__m6Body=null; page.on("request",r=>{if(r.method()==="POST"'
        '&&r.url().endsWith("/api/v1.1/claims"))page.__m6Body=r.postDataJSON()});'
    )
    (GEN / "erp-fault.txt").write_text("TIMEOUT_ALWAYS", encoding="utf-8")
    try:
        case = ui.claim("m6-erp-pending", "2026-09-20", "600.00")
        cid = case["claim_id"]
        assert get_claim(cid)["status"] == "PENDING_ERP" and effect_count(cid) == 0
        assert {"RETRY_EXHAUSTED", "RECOVERY_REQUIRED"} <= set(events(cid))
        shot("erp-pending")
        before = events(cid)
    finally:
        (GEN / "erp-fault.txt").write_text("NONE", encoding="utf-8")
    # Persisted recovery checkpoint must survive a real owned backend restart.
    import subprocess

    subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "scripts.m6_demo", "restart"], check=True)
    ui.cli("goto", ui.BASE + "/claims/" + cid)
    ui.snap()
    ui.run(
        'await page.getByRole("button",{name:"⟳ 恢复工作流（Resume）",exact:true}).click();'
        'await page.getByText("mock: APPROVED",{exact:true}).waitFor();'
    )
    shot("erp-recovered")
    assert get_claim(cid)["status"] == "COMPLETED" and effect_count(cid) == 1
    after = events(cid)
    for name in (
        "CLAIM_LOADED",
        "ERP_CONTEXT_FETCHED",
        "DOCUMENTS_PARSED",
        "HARD_GATE_CHECKED",
        "RULES_CHECKED",
        "POLICY_RETRIEVED",
        "LLM_ANALYSIS_COMPLETED",
        "RISK_ASSESSED",
    ):
        assert after.count(name) == before.count(name) == 1, name
    assert {"WORKFLOW_RECOVERY_RESUMED", "ERP_UPDATED", "WORKFLOW_COMPLETED"} <= set(after)
    replay = ui.run(
        "const b=page.__m6Body;const results=[];for(let i=0;i<2;i++){"
        'const r=await page.request.post("http://127.0.0.1:8001/api/v1.1/claims",'
        "{data:b});results.push({status:r.status(),body:await r.json()})}"
        "return results;"
    )
    assert all(r["status"] == 202 and r["body"]["claim"]["claim_id"] == cid for r in replay)
    assert events(cid).count("CLAIM_CREATED") == 1 and effect_count(cid) == 1
    ui.cli("goto", ui.BASE + "/claims/" + cid + "/audit")
    shot("erp-audit")
    return dict(
        claim_id=cid,
        pending="PENDING_ERP",
        final="COMPLETED",
        backend_restart=True,
        upstream_reexecution=0,
        erp_effects=1,
        replay_count=2,
    )


def hard_block():
    case = ui.claim("m6-hard-block", "2026-09-20", "780.00")
    cid = case["claim_id"]
    claim = get_claim(cid)
    assert case["decision"] == "REJECT" and effect_count(cid) == 0
    result = httpx.post(
        "http://127.0.0.1:8001/claims/" + cid + "/human-review",
        json=dict(
            run_id=claim["active_run_id"],
            expected_revision=1,
            idempotency_key="m6-" + uuid4().hex,
            reviewer_id="m6-synthetic-reviewer",
            decision="APPROVE",
            comment="测试不能覆盖超限",
        ),
    )
    assert result.status_code == 409, result.text
    assert effect_count(cid) == 0 and get_claim(cid)["decision_result"]["decision"] == "REJECT"
    shot("hard-block")
    return dict(**case, human_override_http=409, erp_effects=0)


def main():
    ui.cli("open", ui.BASE + "/policies", "--browser", "chrome")
    ui.snap()
    ui.run('await page.getByTestId("m6-demo-notice").waitFor();')
    ui.main()
    policy = json.loads((GEN / "browser-report.json").read_text(encoding="utf-8"))
    ui.run(
        "await page.setViewportSize({width:1440,height:1000});"
        "page.__m6={errors:[],failed:[],responses:[]};"
        'page.on("pageerror",e=>page.__m6.errors.push(e.message));'
        'page.on("requestfailed",r=>page.__m6.failed.push(r.url()));'
        'page.on("response",r=>{if(r.status()>=400)page.__m6.responses.push('
        "{url:r.url(),status:r.status()})});"
    )
    b = invoice()
    c = recovery_and_replay()
    d = hard_block()
    pages = []
    for path in (
        "/",
        "/claims",
        "/claims/new",
        "/policies",
        "/claims/" + c["claim_id"],
        "/claims/" + c["claim_id"] + "/audit",
    ):
        ui.cli("goto", ui.BASE + path)
        ui.snap()
        ui.run('await page.getByTestId("m6-demo-notice").waitFor();')
        pages.append(path)
    shot("dashboard-final")
    console = ui.run("return page.__m6;")
    assert not console["errors"] and not console["failed"], console
    assert all(
        r["status"] == 409 and r["url"].endswith("/human-review") for r in console["responses"]
    ), console
    report = dict(
        status="PASS_OFFLINE",
        policy=policy,
        invoice=b,
        recovery=c,
        hard_block=d,
        pages=pages,
        console=console,
        paid_http_calls=0,
        real_bge_m3="NOT_RUN",
        real_deepseek="NOT_RUN",
        synthetic_business_date="2026-11-03",
    )
    private_json(OUT / "browser-report.json", report)
    print("M6_BROWSER_SMOKE=PASS_OFFLINE; real OCR/HTTP/PG/SQLite; Mock LLM/ERP")


if __name__ == "__main__":
    main()
