"""Independent optimistic wire fixture; never a model, classifier or production fallback."""

import json
from copy import deepcopy
from uuid import uuid4

import httpx
from test_p4a_matrix import fields

from domain.compliance_v2 import CompactOutput
from integrations.llm.compliance import SEMANTIC_TOPICS
from integrations.llm.compliance_v2 import CompactComplianceDeepSeekProvider
from tests.test_enterprise import login as authenticate


def reference(request, clause):
    return next(e.chunk_id for e in request.evidence if e.clause_id == clause)


def positive_output(request):
    kind = request.facts["trusted"]["expense_type"]
    fact = {"HOTEL": "stay", "ALLOWANCE": "full_days", "ENTERTAINMENT": "participants"}[kind]
    return dict(
        schema_version="p4b.semantic.2",
        checks={t: "CLEAR" for t in SEMANTIC_TOPICS},
        findings=[],
        evidence_refs=[
            reference(request, "第二条"),
            reference(request, "第三十八条"),
            reference(request, "第四十四条"),
            reference(
                request,
                {"HOTEL": "第十五条", "ALLOWANCE": "第二十五条", "ENTERTAINMENT": "第二十九条"}[
                    kind
                ],
            ),
        ],
        factual_basis=[
            "candidate.purpose",
            "candidate.notes",
            "positive.business_related",
            "positive.policy_version",
            "rule_results",
            "positive." + fact,
        ],
        exceptions=[],
        suggested_risk="LOW",
        recommended_action="CHECK_PASSED",
        explanation="必要核验均已完成，合规检查通过，仍须财务最终审批。",
    )


def finding(request, *, topic, basis, clause, text, needs=(), conclusion="INSUFFICIENT_EVIDENCE"):
    return dict(
        topic=topic,
        basis=basis,
        conclusion=conclusion,
        evidence_refs=[reference(request, clause)],
        factual_basis=["candidate.notes", "rule_results"],
        explanation=text,
        required_evidence=list(needs),
    )


class CompactWireFixture:
    """HTTP MockTransport only. Test explicitly supplies mutations; no keyword/ID answers."""

    protocol_version = "p4b.semantic.2"
    model = "offline-wire-fixture"

    def __init__(self, configure=None):
        self.configure = configure
        self.requests = []
        self.bodies = []
        self.http_attempts = 0
        self.mode = None

    async def analyze(self, request):
        self.requests.append(request)
        output = positive_output(request)
        if self.configure:
            self.configure(output, request)
        CompactOutput.model_validate(output)

        def handler(http_request):
            self.http_attempts += 1
            self.bodies.append(json.loads(http_request.content))
            assert len(http_request.content) <= 32768
            if self.mode == "timeout":
                raise httpx.ReadTimeout("synthetic")
            content = "{" if self.mode == "invalid_json" else json.dumps(output, ensure_ascii=False)
            return httpx.Response(
                200,
                json=dict(
                    model=self.model,
                    choices=[
                        dict(finish_reason="stop", message=dict(role="assistant", content=content))
                    ],
                ),
            )

        provider = CompactComplianceDeepSeekProvider(
            api_key="synthetic-offline-fixture-key",
            model=self.model,
            max_tokens=4096,
            transport=httpx.MockTransport(handler),
        )
        return await provider.analyze(request)


def authored_review(request, findings, *, exceptions=()):
    output = deepcopy(positive_output(request))
    output.update(
        findings=findings,
        exceptions=list(exceptions),
        suggested_risk="MEDIUM",
        recommended_action="REQUEST_EVIDENCE",
        explanation="存在明确未决事项，须人工核实。",
    )
    for item in findings:
        output["checks"][item["topic"]] = (
            "UNKNOWN" if item["conclusion"] in {"UNSUPPORTED", "INSUFFICIENT_EVIDENCE"} else "ISSUE"
        )
    return output


def login(c, username="employee"):
    # Reuse actual authenticated sessions; retain production login rate limits.
    cache = getattr(c, "_retest_sessions", {})
    if username not in cache:
        c.cookies.clear()
        r = authenticate(c, username)
        assert r.status_code == 200, r.status_code
        cache[username] = dict(c.cookies)
        c._retest_sessions = cache
    c.cookies.clear()
    c.cookies.update(cache[username])


def pdf_bytes(rows):
    # Small actual text PDF; OCR extracts fields from bytes, never injected OCR results.
    stream = (
        "BT /F1 14 Tf 35 520 Td "
        + " ".join(("0 -42 Td " if n else "") + "(" + s + ") Tj" for n, s in enumerate(rows))
        + " ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 700 550] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode(),
    ]
    raw = b"%PDF-1.4\n"
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(raw)
    raw += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        raw += f"{offset:010d} 00000 n \n".encode()
    return raw + f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def invoice(c, case):
    login(c)
    did = str(uuid4())
    r = c.post("/api/v1.5/invoices/drafts", json=dict(draft_id=did))
    assert r.status_code == 200
    f = dict(
        invoice_number=f"DEMOFIX2{uuid4().hex[:16]}",
        invoice_code="DEMO00123456",
        issue_date="2026-10-12",
        seller_name="Synthetic Shenzhen Hotel",
        buyer_name="Synthetic Demo Company",
        total_amount=case["amount"],
        tax_amount="0.00",
    )
    raw = pdf_bytes(
        [
            "SYNTHETIC INVOICE - DEMO ONLY",
            "NOT A VALID TAX INVOICE",
            f"Invoice Number: {f['invoice_number']}",
            f"Invoice Code: {f['invoice_code']}",
            f"Issue Date: {f['issue_date']}",
            f"Seller Name: {f['seller_name']}",
            f"Buyer Name: {f['buyer_name']}",
            f"Total Amount: CNY {f['total_amount']}",
            f"Tax Amount: CNY {f['tax_amount']}",
        ]
    )
    base = "/api/v1.5/invoices/drafts/" + did
    r = c.post(
        base + "/attachments", files={"file": ("synthetic-retest.pdf", raw, "application/pdf")}
    )
    assert r.status_code == 200
    aid = r.json()["attachment_id"]
    r = c.post(base + "/attachments/" + aid + "/ocr", json={})
    assert r.status_code == 200
    assert r.json()["ocr"]["fields"]["total_amount"]["value"] == case["amount"]
    return dict(
        draft_id=did, attachment_id=aid, confirmed=True, confirmed_fields=f, discrepancy_reason=""
    )


def create(c, case):
    inv = invoice(c, case) if case["kind"] != "ALLOWANCE" else None
    login(c)
    if case["kind"] == "HOTEL":
        declared = fields(amount=case["amount"]).model_dump(mode="json")
        declared.update(
            notes=case["notes"], purpose="客户项目现场工作", seller="Synthetic Shenzhen Hotel"
        )
        r = c.post(
            "/api/v1.5/p4a/v1/claims",
            json=dict(fields=declared, stay_id="stay-1", invoice=inv, idempotency_key=str(uuid4())),
        )
    else:
        r = c.post(
            "/api/v1.5/p4b/v1" + "/claims",
            json=dict(
                expense_type=case["kind"],
                city="深圳",
                amount=case["amount"],
                incurred_on="2026-11-02",
                trip_start="2026-11-02",
                trip_end="2026-11-02",
                purpose="客户项目现场工作",
                notes=case["notes"],
                seller="Synthetic Shenzhen Hotel",
                invoice=inv,
                candidate_full_days=1 if case["kind"] == "ALLOWANCE" else None,
                candidate_lunches=1 if case["kind"] == "ALLOWANCE" else None,
                candidate_dinners=1 if case["kind"] == "ALLOWANCE" else None,
                idempotency_key=str(uuid4()),
            ),
        )
    assert r.status_code == 202, r.json().get("error", {})
    cid = r.json()["claim_id"]
    detail = c.get("/api/v1.5/claims/" + cid).json()
    login(c, "finance")
    r = c.post(
        "/api/v1.5/p4b/v1" + f"/claims/{cid}/verify",
        json=dict(
            facts=case["facts"],
            expected_revision=0,
            source_reference="SYNTHETIC-INDEPENDENT-RECORD-" + uuid4().hex,
            basis="隔离合成材料独立核验：仅核实本请求明确列出的字段；未列字段未核实，例外另行授权。",
            confirmed=True,
            idempotency_key=str(uuid4()),
        ),
    )
    assert r.status_code == 200, r.status_code
    return detail
