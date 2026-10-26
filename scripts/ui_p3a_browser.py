"""Real Chrome acceptance; requires explicitly authorized isolated local services.

No fake upload/parser/RAG responses. CLI assertions plus PostgreSQL/SQLite checks.
"""

import io
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, text

CLI = Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh"
OUT = Path("output/ui-p3a/browser")
FIXTURES = Path("tests/fixtures/policies").resolve()
BASE = os.getenv("UI_P3A_BASE", "http://127.0.0.1:3000")
SESSION = "ui-p3a"
TOKEN = (OUT / "demo-token.txt").read_text(encoding="utf-8").strip()
URL = (OUT / "database-url.txt").read_text(encoding="utf-8").strip()


def cli(*args):
    result = subprocess.run(
        [str(CLI), "-s=" + SESSION, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    safe = (result.stdout + result.stderr).replace(TOKEN, "[REDACTED]")
    with (OUT / "browser-cli.txt").open("a", encoding="utf-8") as log:
        log.write(safe)
    if result.returncode or "### Error" in result.stdout:
        raise AssertionError(safe)
    return result.stdout.replace(TOKEN, "[REDACTED]")


def run(code):
    result = cli("run-code", "async () => {" + code + "}")
    print(result, flush=True)
    return result


def get_result(code):
    result = run(code)
    match = re.search(r"### Result\n(.*?)(?:\n###|$)", result, re.S)
    assert match, result
    return json.loads(match[1])


def upload(name, *, path=None):
    cli("snapshot")
    run(
        'await page.getByLabel("选择制度文件").setInputFiles('
        + json.dumps(str(path or FIXTURES / name))
        + ");"
        'await page.getByText("文件已保存为 DRAFT",{exact:false}).waitFor({timeout:120000});'
    )
    cli("snapshot")
    return get_result(
        'return await page.evaluate(()=>document.querySelector(".policy-detail '
        '.policy-source").innerText.split("\\n")[0]);'
    )


def confirm(*, company="DEMO-CN", version="2026-draft-800"):
    run(
        'await page.getByLabel("公司",{exact:true}).fill(' + json.dumps(company) + ");"
        'await page.getByLabel("地区",{exact:true}).fill("深圳");'
        'await page.getByLabel("制度版本",{exact:true}).fill(' + json.dumps(version) + ");"
        'await page.getByLabel("我已对照原文核对上述元数据").check();'
        'await page.getByRole("button",{name:"确认草稿元数据",exact:true}).click();'
        'await page.getByText("元数据已人工确认并锁定",{exact:false}).waitFor();'
    )
    cli("snapshot")


def ingest_preview():
    run(
        'await page.getByRole("button",{name:"建立草稿向量索引",exact:true}).click();'
        'await page.getByRole("button",{name:"重复导入（幂等核对）",exact:true}).waitFor();'
    )
    cli("snapshot")
    run(
        'await page.getByRole("button",{name:"仅检索此草稿制度",exact:true}).click();'
        'await page.getByRole("heading",{name:"草稿检索结果 · 1 条",exact:true}).waitFor();'
    )
    cli("snapshot")
    return get_result('return await page.locator(".policy-results").innerText();')


engine = create_engine(URL)
with engine.connect() as c:
    active_before = (
        c.execute(
            text(
                "SELECT row_to_json(p)::text FROM policy_versions p "
                "WHERE status='ACTIVE' ORDER BY policy_version_id"
            )
        )
        .scalars()
        .all()
    )
cli("open", BASE + "/policies", "--browser", "chrome")
cli("snapshot")
run(
    "page.__p3a={errors:[],failed:[],responses:[]};"
    'page.on("pageerror",e=>page.__p3a.errors.push(e.message));'
    'page.on("requestfailed",r=>page.__p3a.failed.push(r.url()));'
    'page.on("response",r=>{if(r.status()>=400)page.__p3a.responses.push('
    "{url:r.url(),status:r.status()})});"
    'await page.getByText("本机受控 Demo，非正式制度发布",{exact:true}).waitFor();'
    'await page.getByLabel("本机演示凭证").fill(' + json.dumps(TOKEN) + ");"
    'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
    'await page.getByLabel("选择制度文件").waitFor();'
)
cli("snapshot")
ids = {}
for name, method in [
    ("draft-800.pdf", "pdf-text"),
    ("draft-800.docx", "docx-text"),
    ("scan-draft-800.pdf", "tesseract"),
]:
    uid = upload(name)
    ids[name] = uid
    run(
        'await page.getByText("解析路径：' + method + '",{exact:false}).waitFor();'
        'await page.getByRole("button",{name:"确认草稿元数据",exact:true}).click();'
        'await page.getByRole("alert").filter({hasText:"请先核对制度原文"}).waitFor();'
        'if(!(await page.getByRole("button",{name:"建立草稿向量索引",exact:true}).isDisabled()))'
        'throw Error("unconfirmed draft can index");'
    )
    confirm()
    result = ingest_preview()
    assert "800 per night" in result and uid in result
    run(
        'await page.getByRole("button",{name:"重复导入（幂等核对）",exact:true}).dblclick();'
        'await page.getByText("DRAFT 索引已就绪",{exact:false}).waitFor();'
    )
    assert upload(name) == uid
    # Replay retains locked metadata and retrieves the same single clause.
    run(
        'if(!(await page.getByLabel("公司",{exact:true}).isDisabled()))'
        'throw Error("metadata unlocked on replay");'
        'await page.getByRole("button",{name:"仅检索此草稿制度",exact:true}).click();'
        'await page.getByRole("heading",{name:"草稿检索结果 · 1 条",exact:true}).waitFor();'
    )
    cli("snapshot")
    run(
        "await page.screenshot({path:"
        + json.dumps(str(Path("output/playwright") / ("ui-p3a-" + method + ".png")))
        + ",fullPage:true});"
    )
    # An actual authenticated original download must match bytes.
    run(
        'const [download]=await Promise.all([page.waitForEvent("download"),'
        'page.getByRole("button",{name:"下载原文件核对",exact:true}).click()]);'
        "await download.saveAs(" + json.dumps(str((OUT / ("download-" + name)).resolve())) + ");"
    )
    assert (OUT / ("download-" + name)).read_bytes() == (FIXTURES / name).read_bytes()

# Invalid raw content is rejected by the server, not just the input accept hint.
for name, raw in [("invalid.pdf", b"%PDF-1.7\nwrong\n%%EOF"), ("invalid.docx", b"not OOXML")]:
    path = (OUT / name).resolve()
    path.write_bytes(raw)
    cli("snapshot")
    run(
        'await page.getByLabel("选择制度文件").setInputFiles(' + json.dumps(str(path)) + ");"
        'await page.getByRole("alert").filter({hasText:"文件内容损坏"}).waitFor();'
    )

# Same title/other company/version never mixes. Genuine new OOXML, no patched API.

buffer = io.BytesIO()
with (
    zipfile.ZipFile(FIXTURES / "draft-800.docx") as source,
    zipfile.ZipFile(
        buffer,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as target,
):
    for entry in source.infolist():
        content = source.read(entry)
        if entry.filename == "word/document.xml":
            content = (
                content.replace(b"DEMO-CN", b"OTHER-CN")
                .replace(b"2026-draft-800", b"2026-other")
                .replace(b"800 per night", b"999 per night")
            )
        target.writestr(entry.filename, content)
other = (OUT / "same-title-other.docx").resolve()
other.write_bytes(buffer.getvalue())
other_id = upload(other.name, path=other)
confirm(company="OTHER-CN", version="2026-other")
assert "999 per night" in ingest_preview()
run(
    'await page.getByRole("button",{name:"删除草稿及索引",exact:true}).click();'
    'await page.getByText("草稿和索引已删除",{exact:false}).waitFor();'
)
cli("snapshot")
with engine.connect() as c:
    assert (
        c.scalar(text("SELECT state FROM policy_uploads WHERE upload_id=:u"), dict(u=other_id))
        == "DELETED"
    )
    assert (
        c.scalar(
            text("SELECT count(*) FROM policy_chunks WHERE policy_version_id=:p"),
            dict(p="DRAFT-" + other_id),
        )
        == 0
    )

# Formal workflow from real new-claim form after all draft indexes exist.
cli("goto", BASE + "/claims/new")
cli("snapshot")
fields = {
    "申请人姓名": "UI-P3A 780 governance",
    "所属部门": "Synthetic Demo",
    "职位": "Demo",
    "报销金额（元 / CNY）": "780.00",
    "费用发生日期": "2026-09-20",
    "报销事项 *": "合成草稿隔离治理测试",
    "出差开始日期": "2026-09-20",
    "出差结束日期": "2026-09-21",
    "开票单位（销售方）": "Synthetic Hotel",
}
run(
    "".join(
        "await page.getByLabel(" + json.dumps(key) + ").fill(" + json.dumps(value) + ");"
        for key, value in fields.items()
    )
    + 'await page.getByLabel("出差城市 *").selectOption("深圳");'
    'await page.getByRole("button",{name:"提交报销并启动审核",exact:true}).click();'
    "await page.waitForURL(/CLM-2026-[a-f0-9]{32}/);"
    'await page.getByText("高风险",{exact:true}).waitFor();'
    'await page.getByText("未写入",{exact:true}).waitFor();'
)
cli("snapshot")
run('await page.screenshot({path:"output/playwright/ui-p3a-780-hard-block.png",fullPage:true});')
claim_url = get_result("return page.url();")
cid = claim_url.split("/")[-1]
with sqlite3.connect(OUT / "browser.sqlite3") as db:
    claim = json.loads(db.execute("SELECT body FROM claims WHERE id=?", (cid,)).fetchone()[0])
    assert claim["decision_result"]["decision"] == "REJECT"
    rule = claim["decision_result"]["rule_results"][0]
    assert rule["limit"] == "700.00" and rule["actual"] == "780.00"
    assert (
        db.execute("SELECT count(*) FROM erp_effects WHERE claim_id=?", (cid,)).fetchone()[0] == 0
    )

cli("goto", BASE + "/policies")
cli("snapshot")
run(
    'await page.getByRole("button",{name:"连接制度库",exact:true}).click();'
    'await page.getByLabel("选择制度文件").waitFor();'
    'await page.locator(".policy-list-item").filter({hasText:"Synthetic reimbursement policy"})'
    '.first().click();await page.getByText("提取的制度文字",{exact:true}).waitFor();'
    "await page.setViewportSize({width:390,height:844});"
    'await page.screenshot({path:"output/playwright/ui-p3a-mobile.png",fullPage:true});'
    "if(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth))"
    'throw Error("Mobile overflow");'
)
cli("snapshot")
console = get_result("return page.__p3a;")
assert not console["errors"] and not console["failed"]
assert len(console["responses"]) == 2 and all(r["status"] == 422 for r in console["responses"])
with engine.connect() as c:
    assert (
        c.execute(
            text(
                "SELECT row_to_json(p)::text FROM policy_versions p "
                "WHERE status='ACTIVE' ORDER BY policy_version_id"
            )
        )
        .scalars()
        .all()
        == active_before
    )
    assert c.scalar(text("SELECT count(*) FROM policy_uploads WHERE state='INDEXED'")) == 3
    assert (
        c.scalar(
            text(
                "SELECT count(*) FROM policy_chunks c JOIN policy_versions p "
                "USING(policy_version_id) WHERE p.status='DRAFT' AND "
                "p.source_uri LIKE 'upload://%'"
            )
        )
        == 3
    )
report = dict(
    status="PASS",
    ids=ids,
    deleted_other_id=other_id,
    claim_id=cid,
    decision="REJECT",
    limit="700.00",
    amount="780.00",
    erp_effects=0,
    console=console,
)
(OUT / "browser-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
engine.dispose()
print("UI_P3A_BROWSER_AND_DATABASE=PASS", flush=True)
