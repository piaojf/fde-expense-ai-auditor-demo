"""Conservative source extraction and independent v1.4 request security."""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.config import Settings
from apps.api.main import create_app
from domain.policy_publication import RuleConfirmation
from integrations.ocr.local import LocalOCRProvider
from integrations.policies.parser import PolicyParser
from integrations.policies.rules import extract_hotel_rule

TOKEN = "synthetic-local-policy-test-capability-00000000"


def parsed(lines, confidence=None):
    content = "\n".join(lines)
    rows = []
    start = 0
    for i, line in enumerate(lines):
        rows.append(
            dict(
                text=line,
                start=start,
                end=start + len(line),
                line=i + 1,
                page=None,
                method="synthetic",
                confidence=confidence,
            )
        )
        start += len(line) + 1
    return dict(text=content, lines=rows)


@pytest.mark.parametrize(
    "clause",
    [
        "深圳住宿每晚限额 800 元",
        "深圳酒店每晚标准：800.50 元",
        "住宿限额800元/晚",
        "Article 1: hotel limit CNY 800 per night.",
        "Article 1: Shenzhen accommodation limit is CNY 800.50 per night.",
    ],
)
def test_explicit_decimal_rule_and_source(clause):
    result = extract_hotel_rule(parsed([clause]))
    assert not result["blockers"]
    c = result["candidates"][0]
    assert Decimal(c["amount"]) in (Decimal("800.00"), Decimal("800.50"))
    assert c["quote"] == clause and c["start"] == 0 and c["end"] == len(clause)


@pytest.mark.parametrize(
    "lines",
    [
        ["住宿费用按实际支出核算"],
        ["住宿每晚限额 UNKNOWN 元"],
        ["住宿每晚限额 800 USD"],
        ["每晚800元"],
        ["酒店限额800元"],
        ["住宿每晚限额700元", "住宿每晚限额800元"],
        ["住宿每晚限额0元"],
        ["酒店每晚限额700元；酒店每晚限额800元"],
        ["住宿每晚限额700元，特殊级别限额800元"],
        ["hotel limit CNY 800 per night; special employees CNY 900 per night"],
    ],
)
def test_uncertain_missing_conflicting_limit_blocked(lines):
    assert extract_hotel_rule(parsed(lines))["status"] == "UNKNOWN"


def test_low_confidence_and_tampered_source_blocked():
    value = parsed(["住宿每晚限额800元"], confidence=0.60)
    assert "EXTRACTION_UNCERTAIN" in extract_hotel_rule(value)["blockers"]
    value["lines"][0]["end"] -= 1
    assert "SOURCE_SPAN_INVALID" in extract_hotel_rule(value)["blockers"]


def test_real_future_docx_source():
    value = PolicyParser(LocalOCRProvider()).parse(
        Path("tests/fixtures/policies/publication-800.docx").read_bytes(), "future.docx"
    )
    r = extract_hotel_rule(value)
    assert not r["blockers"] and r["candidates"][0]["amount"] == "800.00"
    assert value["extracted_metadata"]["effective_from"]["value"] == "2026-11-01"


@pytest.mark.parametrize(
    "update",
    [
        dict(amount=800.1),
        dict(amount="NaN"),
        dict(amount="800.001"),
        dict(amount="0"),
        dict(currency="USD"),
        dict(confirmed=False),
        dict(rule_type="MEAL_LIMIT"),
        dict(actor=" "),
        dict(reason=" "),
    ],
)
def test_confirmation_decimal_and_explicit_consent(update):
    with pytest.raises(ValidationError):
        RuleConfirmation(
            **{
                **dict(
                    token=TOKEN,
                    actor="local-demo",
                    reason="source verified",
                    confirmed=True,
                    amount="800.00",
                    clause_start=0,
                    clause_end=10,
                    expected_revision=0,
                ),
                **update,
            }
        )


def test_default_publication_disabled_and_root_contract_unchanged():
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as c:
        assert c.post("/api/v1.4/policies/history", json={"token": TOKEN}).status_code == 503
        schema = c.get("/openapi.json").json()
        assert all("v1.4" not in p for p in schema["paths"])
        assert c.get("/api/v1.4/openapi.json").status_code == 503


def test_simulated_business_date_not_allowed_outside_test_configuration():
    with pytest.raises(ValueError, match="test configuration"):
        create_app(
            Settings(app_env="development", _env_file=None), business_date_provider=lambda: None
        )
