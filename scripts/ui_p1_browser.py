"""Real Chrome UI acceptance via installed Playwright CLI, never synthesizing API success.

Run local Mock LLM API on 8000 with a fresh output/ui-p1/browser.sqlite3 store,
and Vite real-API mode on 5173 first. Open Playwright CLI session ui-p1 with Chrome.
Uses synthetic names only. Response delay/abort deliberately exercises browser failures.
Artifacts are local-only; sqlite assertions run independently of page rendering.
"""

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

CLI = Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"
OUT = Path(os.getenv("UI_P1_OUT", "output/ui-p1"))
BASE = os.getenv("UI_P1_BASE", "http://127.0.0.1:5173")
SESSION = os.getenv("UI_P1_SESSION", "ui-p1")
OUT.mkdir(parents=True, exist_ok=True)
SHOTS = Path("output/playwright")
SHOTS.mkdir(parents=True, exist_ok=True)


def cli(*args):
    r = subprocess.run(
        [str(CLI), f"-s={SESSION}", *args],
        capture_output=True, text=True, encoding="utf-8", check=True
    )
    with (OUT / "browser-cli.txt").open("a", encoding="utf-8") as f:
        f.write(r.stdout)
    if "### Error" in r.stdout:
        raise AssertionError(r.stdout)
    return r.stdout


def run(code):
    result = cli("run-code", "async () => { " + code + " }")
    print(result, flush=True)
    return result


def fresh():
    cli("goto", BASE + "/claims/new")
    cli("snapshot")


def fill(case, amount="600.00", notes=""):
    values = {
        "申请人姓名": f"UI-P1 浏览器申请人 {case}",
        "所属部门": "华南销售部",
        "职位": "客户经理",
        "报销金额（元 / CNY）": amount,
        "费用发生日期": "2026-09-20",
        "报销事项 *": "深圳客户拜访住宿",
        "出差开始日期": "2026-09-20",
        "出差结束日期": "2026-09-21",
        "开票单位（销售方）": "深圳浏览器验收酒店",
        "备注或特殊情况说明（选填）": notes,
    }
    code = "".join(
        f"await page.getByRole('textbox',{{name:{json.dumps(k)}}}).fill({json.dumps(v)});"
        for k, v in values.items()
    )
    code += "await page.getByLabel('出差城市 *').selectOption('深圳');"
    run(code)


fresh()
fill("A")
run(
    "await page.screenshot({path:'output/playwright/ui-p1-form.png',fullPage:true});"
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    "await page.getByText('mock: APPROVED',{exact:true}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-a-result.png',fullPage:true});"
    "return {case:'A',url:page.url()};"
)
cli("snapshot")

fresh()
fill("B", "780.00")
run(
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    "await page.getByText('高风险',{exact:true}).waitFor();"
    "await page.getByText('未写入',{exact:true}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-b-block.png',fullPage:true});"
    "return {case:'B',url:page.url()};"
)
cli("snapshot")

fresh()
fill("C", notes="航班取消导致行程变更，需人工核实")
run(
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    "await page.getByRole('link',{name:'进入人工复核'}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-c-pending.png',fullPage:true});"
)
cli("snapshot")
run("await page.getByRole('link',{name:'进入人工复核'}).click();")
cli("snapshot")
run(
    "await page.getByRole('textbox').last().fill('核实行程变更及金额后批准');"
    "await page.getByRole('button',{name:'通过（Approve）',exact:true}).click();"
    "await page.waitForURL(url=>!url.pathname.endsWith('/review'));"
    "await page.getByRole('button',{name:'⟳ 恢复处理（Resume）'}).waitFor();"
)
cli("snapshot")
run(
    "await page.reload();"
    "await page.getByRole('button',{name:'⟳ 恢复处理（Resume）'}).click();"
    "await page.getByText('mock: APPROVED',{exact:true}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-c-resumed.png',fullPage:true});"
    "return {case:'C',url:page.url()};"
)
cli("snapshot")
run("await page.getByRole('link',{name:'审计追踪'}).click();")
cli("snapshot")
run(
    "await page.getByText('CLAIM_CREATED',{exact:true}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-audit.png',fullPage:true});"
)

# D: blank/illegal server validation, dynamic category field, local invalid amount.
fresh()
run(
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "if(!page.url().endsWith('/new')) throw Error('Required fields bypassed');"
    "await page.getByLabel('费用类型 *').selectOption('MEAL');"
    "if(await page.getByRole('spinbutton',{name:'住宿晚数 *'}).count())"
    " throw Error('Nights shown for meal');"
    "await page.getByLabel('费用类型 *').selectOption('HOTEL');"
)
fill("D-invalid", "0.00")
run(
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "await page.getByRole('alert').filter({hasText:'金额必须大于 0'}).waitFor();"
    "await page.getByRole('textbox',{name:'报销金额（元 / CNY）'}).fill('600.00');"
    "await page.getByRole('textbox',{name:'申请人姓名'}).fill(' ');"
    "const validation = page.waitForResponse(r=>r.url().endsWith('/api/v1.1/claims')"
    "&&r.status()===422);"
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click(); await validation;"
    "await page.getByRole('alert').filter({hasText:'字段校验失败'}).waitFor();"
    "if(await page.getByRole('textbox',{name:'申请人姓名'}).isDisabled())"
    " throw Error('Cannot correct validation');"
)

# D: actual upstream creates one row, response withheld > client timeout, retry same key.
fresh()
fill("D-timeout")
run(
    "await page.route('**/api/v1.1/claims',async route=>{"
    "const real = await route.fetch(); await new Promise(resolve=>setTimeout(resolve,21000));"
    "try {await route.fulfill({response:real});} catch {} });"
    "const button=page.getByRole('button',{name:'提交报销并启动审核'});"
    "await button.dblclick();"
    "await page.getByRole('alert').filter({hasText:'请求超时'}).waitFor({timeout:25000});"
    "return {case:'D-timeout',url:page.url(),pending:await page.evaluate("
    "()=>JSON.parse(sessionStorage.getItem('ui-p1-pending-submission')))};"
)
cli("snapshot")
run(
    "await page.unroute('**/api/v1.1/claims'); await page.reload();"
    "await page.getByRole('button',{name:'使用原提交键重试'}).click();"
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    "await page.getByText('mock: APPROVED',{exact:true}).waitFor();"
    "await page.screenshot({path:'output/playwright/ui-p1-d-timeout-retry.png',fullPage:true});"
    "return {case:'D-retry',url:page.url()};"
)

# D: network request aborted before transmission; retained request is retried honestly.
fresh()
fill("D-network")
run(
    "await page.route('**/api/v1.1/claims',route=>route.abort('failed'));"
    "await page.getByRole('button',{name:'提交报销并启动审核'}).click();"
    "await page.getByRole('alert').filter({hasText:'连接失败'}).waitFor();"
    "await page.unroute('**/api/v1.1/claims');"
)
cli("snapshot")
run(
    "await page.getByRole('button',{name:'使用原提交键重试'}).click();"
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    "await page.getByText('mock: APPROVED',{exact:true}).waitFor();"
    "return {case:'D-network',url:page.url()};"
)

with sqlite3.connect(OUT / "browser.sqlite3") as db:
    rows = db.execute(
        "SELECT s.claim_id,s.payload,c.body FROM submissions s JOIN claims c ON c.id=s.claim_id"
    ).fetchall()
    report = []
    for cid, raw, body in rows:
        fields, claim = json.loads(raw), json.loads(body)
        case = fields["applicant_name"].split()[-1]
        assert re.fullmatch(r"CLM-2026-[a-f0-9]{32}", cid)
        count = db.execute("SELECT count(*) FROM erp_effects WHERE claim_id=?", (cid,)).fetchone()[
            0
        ]
        events = [
            json.loads(r[0])["event_type"]
            for r in db.execute("SELECT body FROM events WHERE claim_id=?", (cid,))
        ]
        assert events.count("CLAIM_CREATED") == 1
        assert claim["decision_result"]["decision"] == ("REJECT" if case == "B" else "APPROVE")
        assert count == (0 if case == "B" else 1)
        if case == "C":
            assert {"HUMAN_REVIEW_SUBMITTED", "WORKFLOW_RESUMED"} <= set(events)
        report.append(
            {
                "case": case,
                "claim_id": cid,
                "status": claim["status"],
                "decision": claim["decision_result"]["decision"],
                "erp_effect_count": count,
                "creation_events": events.count("CLAIM_CREATED"),
            }
        )
    assert {r["case"] for r in report} == {"A", "B", "C", "D-timeout", "D-network"}
    assert len(report) == 5
(OUT / "browser-db-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("BROWSER_E2E_AND_DATABASE=PASS", flush=True)
