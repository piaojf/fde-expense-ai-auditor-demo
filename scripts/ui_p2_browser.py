"""UI-P2 real Chrome/HTTP acceptance with synthetic invoices and independent SQLite checks.

Run against an isolated API/SQLite and frontend. Requires installed Playwright CLI,
Tesseract + chi_sim+eng. UI_P2_BASE defaults to http://127.0.0.1:3000.
Run UI-P1 on the same store first, then this script. No mocked OCR/API responses.
"""

import base64
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

CLI = Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"
BASE = os.getenv("UI_P2_BASE", "http://127.0.0.1:3000")
OUT = Path(os.getenv("UI_P2_OUT", "output/ui-p2"))
OUT.mkdir(parents=True, exist_ok=True)
SESSION = "ui-p2"
FIXTURES = Path("tests/fixtures/invoices").resolve()


def cli(*args):
    result = subprocess.run(
        [str(CLI), "-s=" + SESSION, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    with (OUT / "browser-cli.txt").open("a", encoding="utf-8") as log:
        log.write(result.stdout + result.stderr)
    if result.returncode or "### Error" in result.stdout:
        raise AssertionError(result.stdout)
    return result.stdout


def run(code):
    result = cli("run-code", "async () => {" + code + "}")
    print(result, flush=True)
    return result


def fresh(case):
    cli("goto", BASE + "/claims/new")
    cli("snapshot")
    values = {
        "申请人姓名": f"UI-P2 浏览器申请人 {case}",
        "所属部门": "演示销售部",
        "职位": "客户经理",
        "报销金额（元 / CNY）": "600.00",
        "费用发生日期": "2026-09-20",
        "报销事项 *": "合成票据住宿测试",
        "出差开始日期": "2026-09-20",
        "出差结束日期": "2026-09-21",
        "开票单位（销售方）": "Synthetic Shenzhen Hotel",
    }
    run(
        "".join(
            f'await page.getByRole("textbox",{{name:{json.dumps(k)}}}).fill({json.dumps(v)});'
            for k, v in values.items()
        )
        + 'await page.getByLabel("出差城市 *").selectOption("深圳");'
    )


def upload(name):
    run(
        'await page.getByLabel("报销金额（元 / CNY）").fill("111.00");'
        'await page.getByLabel("费用发生日期").fill("2026-09-19");'
        'await page.getByLabel("开票单位（销售方）").fill("识别前占位单位");'
    )
    run(
        'await page.getByLabel("选择发票附件（选填）").setInputFiles('
        + json.dumps(str(FIXTURES / name))
        + ");"
        'await page.getByRole("button",{name:"核对并确认票据信息"}).waitFor({timeout:60000});'
        'if(await page.getByLabel("报销金额（元 / CNY）").inputValue()!=="600.00") throw Er'
        'ror("No amount autofill");'
        'if(await page.getByLabel("费用发生日期").inputValue()!=="2026-09-20") throw Error'
        '("No date autofill");'
        'await page.getByAltText("发票第一页预览").waitFor();'
        'await page.getByAltText("发票第一页预览").evaluate(img=>img.decode());'
    )
    cli("snapshot")


def confirm(reason=""):
    if reason:
        run(
            'await page.getByLabel("票据差异或修正说明（有提示时必填）").fill('
            + json.dumps(reason)
            + ");"
        )
    run(
        'await page.getByRole("button",{name:"核对并确认票据信息"}).click();'
        'await page.getByRole("button",{name:"已人工确认票据"}).waitFor();'
    )
    cli("snapshot")


def submit():
    run(
        'await page.getByRole("button",{name:"提交报销并启动审核"}).dblclick();'
        "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
        'await page.getByText("已确认的发票证据",{exact:true}).waitFor();'
        'await page.getByAltText("已提交发票第一页预览").waitFor();'
        'await page.getByAltText("已提交发票第一页预览").evaluate(img=>img.decode());'
    )
    cli("snapshot")


cli("open", BASE + "/claims/new", "--browser", "chrome")
cli("snapshot")
# CLI run-code globals are isolated; the page object persists between calls.
# Capture all unexpected console/page/CORS/404 errors throughout acceptance.
run(
    "page.__uiP2Evidence={pageErrors:[],consoleErrors:[],responses:[],failed:[]};"
    'page.on("pageerror",e=>page.__uiP2Evidence.pageErrors.push(e.message));'
    'page.on("console",m=>{if(m.type()==="error")page.__uiP2Evidence.consoleErro'
    "rs.push(m.text())});"
    'page.on("response",r=>{if(r.status()>=400)page.__uiP2Evidence.responses.pus'
    "h({url:r.url(),status:r.status()})});"
    'page.on("requestfailed",r=>page.__uiP2Evidence.failed.push({url:r.url(),error:r.failure()}));'
)

# A: real embedded PDF text; autofill; explicit confirmation; complete original workflow.
fresh("A")
upload("text-invoice.pdf")
run(
    'if(!(await page.getByText("识别路径：pdf-text",{exact:false}).count())) throw Er'
    'ror("PDF text path missing");'
    'await page.screenshot({path:"output/playwright/ui-p2-a-form.png",fullPage:true});'
)
run(
    'await page.getByRole("button",{name:"提交报销并启动审核"}).click();'
    'await page.getByRole("alert").filter({hasText:"请先核对并确认"}).waitFor();'
    'if(!page.url().endsWith("/new"))throw Error("Unconfirmed invoice submitted");'
)
confirm()
run(
    'await page.getByLabel("报销金额（元 / CNY）").fill("600.01");'
    'await page.getByLabel("报销金额（元 / CNY）").fill("600.00");'
    'await page.getByRole("button",{name:"核对并确认票据信息"}).waitFor();'
)
confirm()
submit()
run(
    'await page.getByText("mock: APPROVED",{exact:true}).waitFor();'
    'await page.reload(); await page.getByText("已确认的发票证据",{exact:true}).waitFor();'
    'await page.screenshot({path:"output/playwright/ui-p2-a-detail.png",fullPage:true});'
)

# B: genuine raster Chinese OCR; deletion/reupload; human correction; no bypass.
fresh("B")
upload("scan-chinese.png")
run(
    'if(await page.getByLabel("开票单位（销售方）").inputValue()!=="合成深圳酒店") throw Error('
    '"Chinese seller not autofilled");'
    'await page.getByText("tesseract 5.5.0",{exact:false}).first().waitFor();'
    'await page.getByRole("button",{name:"删除附件并重新上传"}).click();'
    'await page.getByLabel("选择发票附件（选填）").waitFor();'
)
upload("scan-chinese.png")
run(
    'await page.getByLabel("购买方名称",{exact:true}).fill("合成人工纠正公司");'
    'await page.screenshot({path:"output/playwright/ui-p2-b-correction.png",fullPage:true});'
)
confirm("已人工核对并修正购买方名称，合成票据测试")
submit()
run(
    'await page.getByRole("link",{name:"进入人工复核"}).waitFor();'
    'await page.getByRole("link",{name:"进入人工复核"}).click();'
)
cli("snapshot")
run(
    'await page.getByRole("textbox").last().fill("核对原始扫描与人工修正后批准");'
    'await page.getByRole("button",{name:"通过（Approve）",exact:true}).click();'
    'await page.waitForURL(url=>!url.pathname.endsWith("/review"));'
)
cli("snapshot")
run(
    'await page.reload();await page.getByRole("button",{name:"⟳ 恢复处理（Resume）"}).click();'
    'await page.getByText("mock: APPROVED",{exact:true}).waitFor();'
    'await page.getByText("已确认的发票证据",{exact:true}).waitFor();'
    'await page.screenshot({path:"output/playwright/ui-p2-b-resumed.png",fullPage:true});'
)

# C: original invoice amount remains visible; duplicate is a warning, no fraud assertion.
fresh("C")
upload("scan-invoice.png")
run(
    'await page.getByLabel("报销金额（元 / CNY）").fill("650.00");'
    'await page.getByRole("alert").filter({hasText:"金额差异"}).waitFor();'
)
confirm("发票与申报金额存在差异，需核实；合成重复票据用于测试")
run(
    'await page.getByText("已有使用记录",{exact:false}).waitFor();'
    'await page.screenshot({path:"output/playwright/ui-p2-c-mismatch.png",fullPage:true});'
)
submit()
run('await page.getByRole("link",{name:"进入人工复核"}).waitFor();')

# D: corrupt/illegal content is rejected by backend, with no business side effect.
fresh("D")
for name, content in [
    ("corrupt.pdf", b"%PDF-1.7\nwrong\n%%EOF"),
    ("invalid.png", b"HTML <script>x</script>"),
]:
    path = OUT / name
    path.write_bytes(content)
    run(
        'await page.getByLabel("选择发票附件（选填）").setInputFiles('
        + json.dumps(str(path.resolve()))
        + ");"
        'await page.getByRole("alert").filter({hasText:"文件内容损坏或格式不支持"}).waitFor();'
        'if(await page.getByRole("button",{name:"核对并确认票据信息"}).count()) throw Error("'
        'Invalid upload accepted");'
    )
cli("snapshot")
run('await page.screenshot({path:"output/playwright/ui-p2-d-rejection.png",fullPage:true});')

# E: actual upload replay and same-key submit replay, never fake an API success.
fresh("E")
upload("text-invoice.pdf")
pdf_bytes = base64.b64encode((FIXTURES / "text-invoice.pdf").read_bytes()).decode()
run(
    "await page.evaluate(async (encoded) => {"
    'const draft=JSON.parse(sessionStorage.getItem("ui-p2-invoice-draft"));'
    'const base="http://127.0.0.1:8001/api/v1.2/drafts/"+draft.draft_id;'
    'const read=await fetch(base+"/read",{method:"POST",'
    'headers:{"Content-Type":"application/json"},'
    "body:JSON.stringify({token:draft.token})});const attachment=(await read.json()).attachment;"
    "const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));"
    'for(let i=0;i<2;i++){const data=new FormData();data.append("token",draft.token);'
    'data.append("file",new Blob([bytes],{type:"application/pdf"}),"text-invoice.pdf");'
    'const r=await fetch(base+"/attachments",{method:"POST",body:data});'
    "if(!r.ok||(await r.json()).attachment_id!==attachment.attachment_id)"
    'throw Error("Upload replay duplicate");}},' + json.dumps(pdf_bytes) + ");"
)
confirm("已核对重复提示；合成测试票据")
run(
    'page.__uiP2LastRequest=null;page.on("request",r=>{if(r.url().endsWit'
    'h("/api/v1.2/claims")&&r.method()==="POST")page.__uiP2LastRequest=r.'
    "postDataJSON()});"
)
submit()
run(
    'if(!page.__uiP2LastRequest)throw Error("Missing actual submission");'
    'for(let i=0;i<2;i++){const r=await page.request.post("http://127.0.0.1:8001'
    '/api/v1.2/claims",{data:page.__uiP2LastRequest});'
    'if(!r.ok())throw Error("Submit replay failed");'
    'if(!page.url().includes((await r.json()).claim.claim_id))throw Error("Repla'
    'y created another claim");}'
)
# F: hard block still rejects even when OCR and manual confirmation succeeded.
fresh("F")
upload("text-invoice.pdf")
run('await page.getByLabel("报销金额（元 / CNY）").fill("780.00");')
confirm("合成超限与重复发票提示核实测试")
submit()
run(
    'await page.getByText("高风险",{exact:true}).waitFor();'
    'await page.getByText("未写入",{exact:true}).waitFor();'
    'await page.screenshot({path:"output/playwright/ui-p2-f-hard-block.png",fullPage:true});'
)
run(
    "await page.setViewportSize({width:390,height:844});"
    'await page.screenshot({path:"output/playwright/ui-p2-mobile-detail.png",fullPage:true});'
    "if(await page.evaluate(()=>document.documentElement.scrollWidth>window.inne"
    'rWidth)) throw Error("Mobile overflow");'
)
cli("snapshot")
console_result = run(
    "if(page.__uiP2Evidence.pageErrors.length||page.__uiP2Evidence.failed.length"
    ")throw Error(JSON.stringify(page.__uiP2Evidence));"
    'if(page.__uiP2Evidence.responses.some(r=>r.status!==422||!r.url.endsWith("/'
    'attachments")))throw Error("Unexpected HTTP error");'
    'if(page.__uiP2Evidence.consoleErrors.some(e=>!e.includes("422")))throw Erro'
    'r("Unexpected console error");'
    'if(page.__uiP2Evidence.responses.length!==2)throw Error("Missing expected t'
    'wo upload rejections");'
    "return page.__uiP2Evidence;"
)

console_match = re.search(r"### Result\n(.*?)(?:\n###|$)", console_result, re.S)
assert console_match, console_result
(OUT / "browser-console.json").write_text(
    json.dumps(json.loads(console_match[1]), indent=2), encoding="utf-8"
)

with sqlite3.connect(OUT / "browser.sqlite3") as db:
    rows = db.execute(
        "select s.claim_id,s.payload,c.body,e.body from submissions s "
        "join claims c on c.id=s.claim_id join invoice_evidence e on e.claim_id=s.claim_id"
    ).fetchall()
    report = []
    for cid, raw, body, invoice in rows:
        fields, claim, evidence = json.loads(raw), json.loads(body), json.loads(invoice)
        case = fields["applicant_name"].split()[-1]
        assert re.fullmatch(r"CLM-2026-[a-f0-9]{32}", cid)
        effects = db.execute(
            "select count(*) from erp_effects where claim_id=?", (cid,)
        ).fetchone()[0]
        events = [
            json.loads(r[0])["event_type"]
            for r in db.execute("select body from events where claim_id=?", (cid,))
        ]
        assert events.count("CLAIM_CREATED") == events.count("INVOICE_CONFIRMED") == 1
        expected = {
            "A": ("APPROVE", 1),
            "B": ("APPROVE", 1),
            "C": ("HUMAN_REVIEW", 0),
            "E": ("HUMAN_REVIEW", 0),
            "F": ("REJECT", 0),
        }[case]
        assert (claim["decision_result"]["decision"], effects) == expected
        if case == "B":
            assert evidence["attachment"]["ocr"]["methods"] == ["tesseract"]
            assert evidence["attachment"]["ocr"]["engine_version"].startswith("tesseract ")
            assert evidence["confirmed_fields"]["buyer_name"] == "合成人工纠正公司"
            assert evidence["attachment"]["ocr"]["fields"]["buyer_name"]["value"] == "合成演示公司"
            assert {"HUMAN_REVIEW_SUBMITTED", "WORKFLOW_RESUMED"} <= set(events)
        if case in ("C", "E", "F"):
            assert "DUPLICATE_INVOICE_WARNING" in {w["code"] for w in evidence["warnings"]}
        report.append(
            dict(
                case=case,
                claim_id=cid,
                decision=expected[0],
                erp_effects=effects,
                ocr_methods=evidence["attachment"]["ocr"]["methods"],
                warnings=[w["code"] for w in evidence["warnings"]],
            )
        )
    assert {r["case"] for r in report} == {"A", "B", "C", "E", "F"} and len(report) == 5
    assert (
        db.execute(
            "select count(*) from submissions where json_extract(payload,'$.applicant_na"
            "me') like '% D'"
        ).fetchone()[0]
        == 0
    )
(OUT / "ui-p2-browser-db-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("UI_P2_BROWSER_AND_SQLITE=PASS", flush=True)
