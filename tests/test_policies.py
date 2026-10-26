"""Offline parsing, local access and frozen API checks; never bills a provider."""

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.config import Settings
from apps.api.errors import APIError
from apps.api.main import create_app
from apps.api.policies import create_policy_app
from domain.policies import PolicyMetadata
from integrations.ocr.local import LocalOCRProvider
from integrations.policies.parser import PolicyParser, extract_metadata
from integrations.policies.worker import docx, xml

FIXTURES = Path("tests/fixtures/policies")
TOKEN = "synthetic-local-policy-test-capability-00000000"
META = dict(
    title="Synthetic reimbursement policy",
    legal_entity="DEMO-CN",
    region="深圳",
    expense_type="HOTEL",
    version="2026-draft-800",
    effective_from="2026-01-01",
    effective_to="2027-01-01",
)


def parser():
    return PolicyParser(LocalOCRProvider(languages="eng"))


@pytest.mark.parametrize(
    "filename,method",
    [
        ("draft-800.pdf", "pdf-text"),
        ("draft-800.docx", "docx-text"),
        ("scan-draft-800.pdf", "tesseract"),
    ],
)
def test_genuine_pdf_docx_scan_extraction(filename, method):
    data = parser().parse((FIXTURES / filename).read_bytes(), filename)
    assert data["methods"] == [method]
    assert "800" in data["text"] and "700" in data["text"]
    assert data["extracted_metadata"]["legal_entity"]["value"] == "DEMO-CN"
    assert data["extracted_metadata"]["version"]["value"] == "2026-draft-800"
    for line in data["lines"]:
        assert data["text"][line["start"] : line["end"]] == line["text"]
    if method == "docx-text":
        assert data["pages"] is None
    if method == "tesseract":
        assert all(line["method"] == "tesseract" for line in data["lines"])
        assert any(line["confidence"] is not None for line in data["lines"])


@pytest.mark.parametrize(
    "name,raw",
    [
        ("spoof.docx", b"<html>not office</html>"),
        ("spoof.pdf", b"PKwrong"),
        ("corrupt.pdf", b"%PDF-1.7\nwrong\n%%EOF"),
        ("not.doc", b"anything"),
        ("empty.pdf", b""),
        ("image.pdf", Path("tests/fixtures/invoices/scan-invoice.png").read_bytes()),
    ],
)
def test_content_validation(name, raw):
    with pytest.raises(APIError) as error:
        parser().parse(raw, name)
    assert error.value.status == 422


def amend_docx(name, content, *, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as z:
        with zipfile.ZipFile(FIXTURES / "draft-800.docx") as source:
            for entry in source.infolist():
                if entry.filename != name:
                    z.writestr(entry.filename, source.read(entry))
        z.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "name,content",
    [
        ("../outside", b"bad"),
        ("/outside", b"bad"),
        ("word\\bad", b"bad"),
        ("word/vbaProject.bin", b"macro"),
        ("word/embeddings/evil.bin", b"payload"),
        ("word/activeX/control.xml", b"<x/>"),
        (
            "word/_rels/document.xml.rels",
            b'<Relationships><Relationship TargetMode="External" '
            b'Target="http://127.0.0.1/private"/></Relationships>',
        ),
        ("word/document.xml", b'<!DOCTYPE x [<!ENTITY a "payload">]><x>&a;</x>'),
        ("word/document.xml", b"A" * (6 * 1024 * 1024)),
        ("word/document.xml", b"A" * 100000),
        ("docProps/app.xml", b'<Properties xmlns="app"><Pages>4</Pages></Properties>'),
    ],
)
def test_docx_traversal_active_content_entities_bombs_and_page_limits(name, content):
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        docx(amend_docx(name, content))


def test_docx_member_paragraph_and_text_limits():
    raw = amend_docx(
        "word/document.xml",
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
        b'wordprocessingml/2006/main">' + b"<w:p/>" * 2001 + b"</w:document>",
        compression=zipfile.ZIP_STORED,
    )
    with pytest.raises(ValueError):
        docx(raw)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for n in range(201):
            z.writestr(f"member{n}", "text")
    with pytest.raises(ValueError):
        docx(buffer.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xml_entities_rejected_for_non_utf8(encoding):
    with pytest.raises(ValueError):
        xml('<!DOCTYPE x [<!ENTITY e "hi">]><x>&e;</x>'.encode(encoding))


def test_uncertain_metadata_remains_unknown():
    lines = [
        dict(text="Company: A", confidence=None),
        dict(text="Company: B", confidence=None),
        dict(text="Region: 深圳", confidence=0.7),
        dict(text="Version: guessed by arbitrary prose", confidence=0.2),
        dict(text="Effective From: invalid", confidence=None),
    ]
    data = extract_metadata(lines)
    assert all(v["value"] == "UNKNOWN" and v["source"] is None for v in data.values())


@pytest.mark.parametrize(
    "updates",
    [
        {"legal_entity": "UNKNOWN"},
        {"region": ""},
        {"version": "x\nattack"},
        {"effective_to": "2026-01-01"},
        {"hotel_limit": "800"},
        {"status": "ACTIVE"},
        {"effective_from": "UNKNOWN"},
    ],
)
def test_confirm_metadata_cannot_guess_or_publish(updates):
    with pytest.raises(ValidationError):
        PolicyMetadata(**(META | updates))
    assert PolicyMetadata(**META).effective_from == date(2026, 1, 1)


def test_demo_disabled_and_no_default_provider_calls(monkeypatch):
    monkeypatch.setattr(
        "rag.embeddings.providers.RemoteEmbeddingProvider.embed_documents",
        lambda *a: pytest.fail("unapproved remote call"),
    )
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as client:
        response = client.post("/api/v1.3/policies/list", json={"token": TOKEN})
        assert response.status_code == 503
        assert client.app.state.policy_app.state.engine is None


@pytest.mark.parametrize(
    "peer,headers,token,status",
    [
        ("198.51.100.1", {}, TOKEN, 403),
        ("127.0.0.1", {"host": "public.example"}, TOKEN, 403),
        ("127.0.0.1", {"origin": "https://evil.example"}, TOKEN, 403),
        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, TOKEN, 403),
        ("127.0.0.1", {"Forwarded": "for=127.0.0.1"}, TOKEN, 403),
        ("127.0.0.1", {}, "incorrect", 403),
        ("127.0.0.1", {}, TOKEN, 200),
    ],
)
def test_local_gate_and_capability(peer, headers, token, status):
    class FakeStore:
        def list(self):
            return {"items": []}

    settings = Settings(
        app_env="test", policy_demo_enabled=True, policy_demo_token=TOKEN, _env_file=None
    )
    app = create_policy_app(settings, LocalOCRProvider(), store=FakeStore())
    with TestClient(app, base_url="http://127.0.0.1", client=(peer, 10000)) as client:
        response = client.post("/policies/list", json={"token": token}, headers=headers)
        assert response.status_code == status


def test_request_limit_before_multipart_decode():
    settings = Settings(
        app_env="test", policy_demo_enabled=True, policy_demo_token=TOKEN, _env_file=None
    )
    app = create_policy_app(settings, LocalOCRProvider(), store=object())
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 10000)) as client:
        r = client.post("/policies/upload", content=b"a", headers={"content-length": "11000000"})
        assert r.status_code == 413
        r = client.post("/policies/upload", content=iter([b"a" * 10485760, b"b" * 65537]))
        assert r.status_code == 413


def test_pdf_page_limit_and_ocr_unavailable():
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(4):
        page = doc.new_page(600, 600)
        page.close()
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    with pytest.raises(APIError):
        parser().parse(buffer.getvalue(), "four.pdf")
    with pytest.raises(APIError) as error:
        PolicyParser(LocalOCRProvider(binary="/missing/tesseract")).parse(
            (FIXTURES / "scan-draft-800.pdf").read_bytes(),
            "scan.pdf",
        )
    assert error.value.code == "OCR_UNAVAILABLE"


def test_unlabelled_policy_keeps_metadata_unknown_and_no_model_guess():
    data = extract_metadata(
        [dict(text="The Shenzhen hotel allowance is 800 in 2026.", confidence=None)]
    )
    assert all(v["value"] == "UNKNOWN" for v in data.values())


def test_large_page_dimensions_rejected():
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    page = doc.new_page(2500, 600)
    page.close()
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    with pytest.raises(APIError):
        parser().parse(buffer.getvalue(), "oversize-page.pdf")
