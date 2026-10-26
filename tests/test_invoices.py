import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_submission import payload

from apps.api.config import Settings
from apps.api.errors import APIError
from apps.api.invoice_store import InvoiceStore
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.invoices import OCRResult
from integrations.ocr.base import MockOCRProvider
from integrations.ocr.fields import extract_fields
from integrations.ocr.local import LocalOCRProvider

FIXTURES = Path("tests/fixtures/invoices")
FIELDS = dict(
    invoice_number="DEMO202609200001",
    invoice_code="DEMO00123456",
    issue_date="2026-09-20",
    seller_name="Synthetic Shenzhen Hotel",
    buyer_name="Synthetic Demo Company",
    total_amount="600.00",
    tax_amount="33.96",
)


def fake_result():
    labels = [
        "Invoice Number",
        "Invoice Code",
        "Issue Date",
        "Seller Name",
        "Buyer Name",
        "Total Amount",
        "Tax Amount",
    ]
    lines = [
        dict(text=f"{label}: {value}", page=1, line=i + 1, method="mock-test", confidence=1.0)
        for i, (label, value) in enumerate(zip(labels, FIELDS.values(), strict=True))
    ]
    return OCRResult(
        provider="mock-test",
        engine_version="mock",
        methods=["mock-test"],
        lines=lines,
        fields=extract_fields(lines),
    )


@pytest.fixture
def invoice_client(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "business.sqlite3"))
    app = create_app(
        Settings(app_env="test", _env_file=None),
        repository=repo,
        ocr_provider=MockOCRProvider(fake_result()),
    )
    with TestClient(app) as client:
        yield client
    repo.close()


def reserve(client):
    draft = dict(draft_id=str(uuid4()), token=str(uuid4()))
    assert client.post("/api/v1.2/drafts", json=draft).status_code == 200
    return draft


def route(draft, suffix):
    return "/api/v1.2/drafts/" + draft["draft_id"] + suffix


def upload(client, draft, data=None, filename="../../invoice.pdf", mime="application/pdf"):
    return client.post(
        route(draft, "/attachments"),
        data={"token": draft["token"]},
        files={"file": (filename, data or (FIXTURES / "text-invoice.pdf").read_bytes(), mime)},
    )


def ready(client):
    draft = reserve(client)
    result = upload(client, draft)
    assert result.status_code == 200, result.text
    aid = result.json()["attachment_id"]
    r = client.post(route(draft, f"/attachments/{aid}/ocr"), json={"token": draft["token"]})
    assert r.status_code == 200, r.text
    return draft, r.json()


def request(draft, attachment, **updates):
    return {
        **payload(seller=FIELDS["seller_name"]),
        **draft,
        "attachment_id": attachment["attachment_id"],
        "confirmed": True,
        "confirmed_fields": FIELDS,
        "discrepancy_reason": "",
        **updates,
    }


def create(client, body):
    r = client.post("/api/v1.2/claims", json=body)
    assert r.status_code == 202, r.text
    return r.json()


def test_upload_digest_scope_preview_dedup_delete(invoice_client):
    c = invoice_client
    draft, attachment = ready(c)
    assert attachment["file_name"] == "invoice.pdf"
    assert (
        attachment["sha256"]
        == hashlib.sha256((FIXTURES / "text-invoice.pdf").read_bytes()).hexdigest()
    )
    assert upload(c, draft).json()["attachment_id"] == attachment["attachment_id"]
    assert attachment["ocr"]["provider"] == "mock-test"
    preview = c.post(
        route(draft, f"/attachments/{attachment['attachment_id']}/preview"),
        json={"token": draft["token"]},
    )
    assert preview.content.startswith(b"\x89PNG")
    assert preview.headers["cache-control"] == "no-store"
    other = reserve(c)
    for action in ("ocr", "preview", "delete"):
        assert (
            c.post(
                route(other, f"/attachments/{attachment['attachment_id']}/{action}"),
                json={"token": other["token"]},
            ).status_code
            == 404
        )
    assert c.post(route(draft, "/read"), json={"token": other["token"]}).status_code == 404
    assert c.post("/api/v1.2/drafts", json={**draft, "token": other["token"]}).status_code == 404
    assert upload(c, draft, (FIXTURES / "scan-invoice.png").read_bytes()).status_code == 409
    for _ in range(2):
        assert (
            c.post(
                route(draft, f"/attachments/{attachment['attachment_id']}/delete"),
                json={"token": draft["token"]},
            ).status_code
            == 200
        )
    assert (
        c.post(route(draft, "/read"), json={"token": draft["token"]}).json()["attachment"] is None
    )
    assert upload(c, draft).json()["attachment_id"] != attachment["attachment_id"]


@pytest.mark.parametrize(
    "data", [b"HTML <script>x</script>", b"%PDF-1.7\nwrong\n%%EOF", b"\x89PNG\r\n", b""]
)
def test_corrupt_format_no_persistence(invoice_client, data):
    c = invoice_client
    draft = reserve(c)
    r = c.post(
        route(draft, "/attachments"),
        data={"token": draft["token"]},
        files={"file": ("forged.pdf", data, "application/pdf")},
    )
    assert r.status_code == 422
    store = c.app.state.invoice_store
    assert store.db.execute("select count(*) from invoice_attachments").fetchone()[0] == 0
    assert list(store.root.iterdir()) == []


def test_size_limits_declared_chunked_file_and_pixels(invoice_client):
    c = invoice_client
    draft = reserve(c)
    assert (
        c.post(
            route(draft, "/attachments"),
            content=b"x",
            headers={"Content-Length": str(11 * 1024 * 1024)},
        ).status_code
        == 413
    )
    assert c.post(
        route(draft, "/attachments"),
        content=iter([b"x" * 1024 * 1024] * 11),
        headers={"Content-Type": "application/octet-stream"},
    ).status_code in (413, 422)
    # Multipart overhead below request cap, file exceeds exact 10 MiB cap.
    assert upload(c, draft, b"x" * (10 * 1024 * 1024 + 1)).status_code == 413
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("1", (5000, 5000)).save(buffer, format="PNG")
    assert upload(c, draft, buffer.getvalue(), "huge.png", "image/png").status_code == 422


def test_actual_mime_not_extension_or_header(invoice_client):
    c = invoice_client
    draft = reserve(c)
    r = upload(c, draft, (FIXTURES / "scan-invoice.jpg").read_bytes(), "invoice.pdf", "text/plain")
    assert r.status_code == 200 and r.json()["content_type"] == "image/jpeg"


def test_confirm_atomic_claim_idempotency_and_freeze(invoice_client):
    c = invoice_client
    draft, attachment = ready(c)
    body = request(draft, attachment)
    before = c.get("/dashboard/metrics").json()["total_claims"]
    assert c.post("/api/v1.2/claims", json={**body, "confirmed": False}).status_code == 422
    assert c.get("/dashboard/metrics").json()["total_claims"] == before
    result = create(c, body)
    cid = result["claim"]["claim_id"]
    assert result["claim"]["decision_result"]["decision"] == "APPROVE"
    assert len(result["erp_effects"]) == 1
    assert result["invoice_evidence"]["confirmed_fields"] == FIELDS
    assert create(c, body)["claim"]["claim_id"] == cid
    altered = {**body, "confirmed_fields": {**FIELDS, "buyer_name": "Changed"}}
    assert c.post("/api/v1.2/claims", json=altered).status_code == 409
    assert (
        c.post("/api/v1.2/claims", json={**body, "idempotency_key": "different"}).status_code == 409
    )
    assert c.get("/dashboard/metrics").json()["total_claims"] == before + 1
    assert (
        c.post(
            route(draft, f"/attachments/{attachment['attachment_id']}/delete"),
            json={"token": draft["token"]},
        ).status_code
        == 409
    )
    assert c.get(f"/api/v1.2/claims/{cid}/invoice-preview").status_code == 200
    events = c.app.state.repository.events(cid)
    assert sum(e.event_type == "INVOICE_CONFIRMED" for e in events) == 1
    assert not any(
        "Synthetic Shenzhen Hotel" in e.model_dump_json()
        for e in events
        if e.event_type == "INVOICE_CONFIRMED"
    )


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"amount": "650.00"}, "AMOUNT_MISMATCH"),
        (
            {"confirmed_fields": {**FIELDS, "total_amount": "650.00"}, "amount": "650.00"},
            "OCR_CORRECTED",
        ),
        ({"confirmed_fields": {**FIELDS, "issue_date": "2099-01-01"}}, "ISSUE_DATE_UNUSUAL"),
        ({"confirmed_fields": {**FIELDS, "buyer_name": ""}}, "MISSING_BUYER_NAME"),
        ({"seller": "Different Seller"}, "SELLER_MISMATCH"),
    ],
)
def test_warnings_require_reason_preserve_raw_human(invoice_client, updates, code):
    c = invoice_client
    draft, attachment = ready(c)
    body = request(draft, attachment, **updates)
    before = c.get("/dashboard/metrics").json()["total_claims"]
    assert c.post("/api/v1.2/claims", json=body).status_code == 422
    assert c.get("/dashboard/metrics").json()["total_claims"] == before
    result = create(c, {**body, "discrepancy_reason": "Synthetic invoice needs verification"})
    assert code in {w["code"] for w in result["invoice_evidence"]["warnings"]}
    assert result["claim"]["status"] == "PENDING_HUMAN"
    assert result["erp_effects"] == []
    assert (
        result["invoice_evidence"]["attachment"]["ocr"]["fields"]["total_amount"]["value"]
        == "600.00"
    )


def test_duplicate_warning_cannot_be_hidden_by_correction(invoice_client):
    c = invoice_client
    d, a = ready(c)
    first = create(c, request(d, a))
    d2, a2 = ready(c)
    body = request(
        d2,
        a2,
        idempotency_key="second",
        discrepancy_reason="Verify prior usage",
        confirmed_fields={**FIELDS, "invoice_number": "NEW001234"},
    )
    second = create(c, body)
    assert "DUPLICATE_INVOICE_WARNING" in {
        w["code"] for w in second["invoice_evidence"]["warnings"]
    }
    assert second["claim"]["status"] == "PENDING_HUMAN"
    assert second["claim"]["claim_id"] != first["claim"]["claim_id"]


def test_hard_block_dominates_invoice_warnings_and_human(invoice_client):
    c = invoice_client
    d, a = ready(c)
    result = create(c, request(d, a, amount="780.00", discrepancy_reason="Verify mismatch"))
    dr = result["claim"]["decision_result"]
    assert dr["risk_level"] == "HIGH" and dr["decision"] == "REJECT"
    assert result["erp_effects"] == []
    cid = result["claim"]["claim_id"]
    op = result["operation"]
    assert (
        c.post(
            f"/claims/{cid}/human-review",
            json=dict(
                run_id=op["run_id"],
                expected_revision=op["revision"],
                decision="APPROVE",
                reviewer_id="FIN",
                comment="override",
                idempotency_key="override",
            ),
        ).status_code
        == 409
    )


def test_nonhotel_invoice_cannot_approve_unknown_policy(invoice_client):
    d, a = ready(invoice_client)
    result = create(invoice_client, request(d, a, category="MEAL", nights=None))
    assert result["claim"]["decision_result"]["decision"] == "HUMAN_REVIEW"
    assert result["claim"]["decision_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


def test_upload_and_submit_parallel_no_duplicate_effects(invoice_client):
    c = invoice_client
    d = reserve(c)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: upload(c, d), range(3)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["attachment_id"] for r in responses}) == 1
    a = responses[0].json()
    c.post(route(d, f"/attachments/{a['attachment_id']}/ocr"), json={"token": d["token"]})
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: create(c, request(d, a)), range(3)))
    assert len({r["claim"]["claim_id"] for r in results}) == 1
    assert all(len(r["erp_effects"]) == 1 for r in results)


def test_mutation_prevents_ocr_preview_submission(invoice_client):
    c = invoice_client
    d, a = ready(c)
    c.app.state.invoice_store.path(a["attachment_id"]).write_bytes(b"MUTATED")
    for action in ("ocr", "preview"):
        assert (
            c.post(
                route(d, f"/attachments/{a['attachment_id']}/{action}"), json={"token": d["token"]}
            ).status_code
            == 409
        )
    assert c.post("/api/v1.2/claims", json=request(d, a)).status_code == 409


def test_crash_recovery_staging_tombstones_and_restart(invoice_client, monkeypatch):
    c = invoice_client
    d = reserve(c)
    store = c.app.state.invoice_store
    original = store.sync_dir

    def fail():
        raise OSError("simulated interruption after rename")

    monkeypatch.setattr(store, "sync_dir", fail)
    with pytest.raises(OSError):
        upload(c, d)
    row = store.db.execute("select id,state from invoice_attachments").fetchone()
    assert row[1] == "STAGING" and store.path(row[0]).is_file()
    monkeypatch.setattr(store, "sync_dir", original)
    InvoiceStore(c.app.state.repository, store.processor)
    assert store.attachment(d["draft_id"])["state"] == "READY"
    with store.db:
        store.db.execute("update invoice_attachments set state='DELETED'")
    InvoiceStore(c.app.state.repository, store.processor)
    assert not store.path(row[0]).exists()
    # Crash between staging commit and rename (part still exists).
    aid = str(uuid4())
    part = store.path(aid, ".part")
    shutil.copyfile(FIXTURES / "text-invoice.pdf", part)
    metadata = dict(attachment_id=aid, sha256=hashlib.sha256(part.read_bytes()).hexdigest())
    with store.db:
        store.db.execute(
            "insert into invoice_attachments values (?,?,?,'STAGING',?,NULL)",
            (aid, d["draft_id"], metadata["sha256"], json.dumps(metadata)),
        )
    InvoiceStore(c.app.state.repository, store.processor)
    assert store.path(aid).exists() and not part.exists()
    store.path(aid).unlink()
    InvoiceStore(c.app.state.repository, store.processor)
    assert store.attachment(d["draft_id"])["state"] == "MISSING"


def test_legacy_key_cannot_attach_evidence_to_completed_claim(invoice_client):
    c = invoice_client
    assert c.post("/api/v1.1/claims", json=payload(seller=FIELDS["seller_name"])).status_code == 202
    d, a = ready(c)
    assert c.post("/api/v1.2/claims", json=request(d, a)).status_code == 409
    assert (
        c.app.state.invoice_store.db.execute("select count(*) from invoice_evidence").fetchone()[0]
        == 0
    )


def test_fields_unknown_ambiguity_invalid_date_low_confidence():
    rows = [
        dict(text="Invoice Number: DEMO1234", page=1, line=1, method="test", confidence=0.9),
        dict(text="Invoice Number: DEMO5678", page=2, line=1, method="test", confidence=0.9),
        dict(text="Issue Date: 2026-02-30", page=1, line=2, method="test", confidence=None),
        dict(text="Total Amount: 600.00", page=1, line=3, method="test", confidence=0.3),
    ]
    fields = extract_fields(rows)
    assert all(v.value is None and v.status == "UNKNOWN" for v in fields.values())
    assert "purpose" not in fields and "nights" not in fields


@pytest.mark.parametrize(
    "name,method",
    [
        ("text-invoice.pdf", "pdf-text"),
        ("scan-invoice.png", "tesseract"),
        ("scan-invoice.jpg", "tesseract"),
        ("scan-invoice.pdf", "tesseract"),
    ],
)
def test_real_engine_pixels_and_pdf(name, method):
    provider = LocalOCRProvider()
    result = provider.recognize(str(FIXTURES / name))
    assert result.provider == "local-hybrid" and result.methods == [method]
    assert {k: v.value for k, v in result.fields.items()} == FIELDS
    if method == "tesseract":
        assert result.engine_version.startswith("tesseract ")
        assert result.fields["total_amount"].confidence is not None
        assert result.lines[0]["bbox"]
    else:
        assert result.engine_version == "not-invoked"


def test_missing_ocr_engine_is_user_action_required_not_mock():
    with pytest.raises(APIError) as error:
        LocalOCRProvider(binary="/nonexistent/tesseract").recognize(
            str(FIXTURES / "scan-invoice.png")
        )
    assert error.value.code == "OCR_UNAVAILABLE" and error.value.status == 503


def test_streaming_multipart_limit_without_content_length(invoice_client):
    c = invoice_client
    d = reserve(c)
    prefix = (
        f'--invoice-boundary\r\nContent-Disposition: form-data; name="token"\r\n\r\n'
        f"{d['token']}\r\n--invoice-boundary\r\nContent-Disposition: form-data; "
        'name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode()

    def chunks():
        yield prefix
        for _ in range(11):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--invoice-boundary--\r\n"

    response = c.post(
        route(d, "/attachments"),
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=invoice-boundary"},
    )
    assert response.status_code == 413
    assert list(c.app.state.invoice_store.root.iterdir()) == []


def test_real_chinese_scan_and_provenance():
    import os

    tessdata = os.getenv("TEST_OCR_TESSDATA", "output/ui-p2/tessdata")
    result = LocalOCRProvider(languages="chi_sim+eng", tessdata=tessdata).recognize(
        str(FIXTURES / "scan-chinese.png")
    )
    assert result.methods == ["tesseract"] and result.engine_version.startswith("tesseract ")
    assert result.fields["seller_name"].value == "合成深圳酒店"
    assert result.fields["buyer_name"].value == "合成演示公司"
    assert result.fields["invoice_number"].value == "DEMO202609200002"
    assert result.fields["total_amount"].value == "600.00"
    assert result.fields["total_amount"].confidence >= 0.55


def test_binding_failure_rolls_back_all_business_records(invoice_client, monkeypatch):
    c = invoice_client
    d, a = ready(c)
    store = c.app.state.invoice_store
    original = store.bind
    before = c.get("/dashboard/metrics").json()["total_claims"]

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("simulated transaction failure")

    monkeypatch.setattr(store, "bind", fail)
    with pytest.raises(OSError):
        c.post("/api/v1.2/claims", json=request(d, a))
    assert store.db.execute("select count(*) from invoice_evidence").fetchone()[0] == 0
    assert store.read(d["draft_id"], d["token"])["claim_id"] is None
    assert c.get("/dashboard/metrics").json()["total_claims"] == before
    monkeypatch.setattr(store, "bind", original)
    assert len(create(c, request(d, a))["erp_effects"]) == 1


def test_invoice_claim_restart_and_human_resume(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    settings = Settings(app_env="test", _env_file=None)
    repo = MockClaimRepository(store_path=path)
    with TestClient(
        create_app(settings, repository=repo, ocr_provider=MockOCRProvider(fake_result()))
    ) as c:
        d, a = ready(c)
        body = request(d, a, amount="650.00", discrepancy_reason="Partial invoice reimbursement")
        result = create(c, body)
        cid = result["claim"]["claim_id"]
        op = result["operation"]
        assert result["claim"]["status"] == "PENDING_HUMAN"
    repo.close()
    repo = MockClaimRepository(store_path=path)
    with TestClient(create_app(settings, repository=repo)) as c:
        assert create(c, body)["claim"]["claim_id"] == cid
        assert c.get(f"/api/v1.2/claims/{cid}/invoice-preview").status_code == 200
        review = c.post(
            f"/claims/{cid}/human-review",
            json=dict(
                run_id=op["run_id"],
                expected_revision=op["revision"],
                decision="APPROVE",
                reviewer_id="FIN",
                comment="Checked synthetic invoice",
                idempotency_key="review",
            ),
        )
        assert review.status_code == 200
        resume = dict(
            run_id=op["run_id"],
            expected_revision=review.json()["revision"],
            idempotency_key="resume",
        )
        assert c.post(f"/claims/{cid}/resume", json=resume).status_code == 202
        assert c.post(f"/claims/{cid}/resume", json=resume).status_code == 202
        final = c.get(f"/api/v1.2/claims/{cid}").json()
        assert final["claim"]["decision_result"]["decision"] == "APPROVE"
        assert len(final["erp_effects"]) == 1
        assert final["invoice_evidence"]["attachment"]["ocr"]["provider"] == "mock-test"
    repo.close()


def test_delete_during_slow_ocr_never_restores_deleted_attachment(invoice_client):
    from threading import Event

    c = invoice_client
    d = reserve(c)
    a = upload(c, d).json()
    store = c.app.state.invoice_store
    started, proceed = Event(), Event()

    class SlowProvider:
        def recognize(self, path):
            started.set()
            assert proceed.wait(5)
            return fake_result()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            store.recognize, d["draft_id"], a["attachment_id"], d["token"], SlowProvider()
        )
        assert started.wait(3)
        try:
            deletion = pool.submit(store.delete, d["draft_id"], a["attachment_id"], d["token"])
            assert deletion.result(timeout=2)["deleted"]
        finally:
            proceed.set()
        with pytest.raises(APIError) as error:
            future.result()
        assert error.value.code == "ATTACHMENT_NOT_FOUND"
    assert store.read(d["draft_id"], d["token"])["attachment"] is None


def test_explicit_ocr_refresh_preserves_cache_failure_and_submitted_evidence(invoice_client):
    c = invoice_client
    d, attachment = ready(c)
    store = c.app.state.invoice_store
    provider = MockOCRProvider(fake_result())
    aid = attachment["attachment_id"]
    store.recognize(d["draft_id"], aid, d["token"], provider)
    assert provider.calls == 0  # Existing normal route retains idempotent cache.
    refreshed = store.recognize(d["draft_id"], aid, d["token"], provider, refresh=True)
    assert provider.calls == 1 and refreshed["ocr"] == attachment["ocr"]

    class Unavailable:
        def recognize(self, path):
            raise APIError(503, "OCR_UNAVAILABLE", "Unavailable")

    with pytest.raises(APIError):
        store.recognize(d["draft_id"], aid, d["token"], Unavailable(), refresh=True)
    assert store.attachment(d["draft_id"], aid)["ocr"] == attachment["ocr"]
    other = reserve(c)
    retry = f"/attachments/{aid}/ocr-retry"
    assert c.post(route(other, retry), json={"token": other["token"]}).status_code == 404
    assert c.post(route(d, retry), json={"token": d["token"]}).status_code == 200
    create(c, request(d, attachment))
    response = c.post(route(d, retry), json={"token": d["token"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DRAFT_SUBMITTED"


def test_refresh_replaces_unknown_fields_without_reupload(invoice_client):
    c = invoice_client
    d, attachment = ready(c)
    store = c.app.state.invoice_store
    with store.db:
        store.db.execute("UPDATE invoice_attachments SET ocr=? WHERE id=?",
                         (OCRResult(provider="local-hybrid", engine_version="old", methods=[],
                                    fields=extract_fields([]), lines=[]).model_dump_json(),
                          attachment["attachment_id"]))
    result = c.post(route(d, f"/attachments/{attachment['attachment_id']}/ocr-retry"),
                    json={"token": d["token"]})
    assert result.status_code == 200
    assert result.json()["sha256"] == attachment["sha256"]
    assert result.json()["ocr"]["fields"]["total_amount"]["value"] == "600.00"
