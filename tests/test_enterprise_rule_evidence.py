"""Adversarial local clauses must not become executable nightly thresholds."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_policy_publication import parsed

from apps.api.errors import APIError
from domain.enterprise_policies import EnterpriseRuleConfirmation
from domain.policy_publication import RuleConfirmation
from integrations.policies.enterprise_rules import execution_blockers, manual_candidate
from integrations.policies.rules import extract_hotel_rule


def row(quote):
    value = parsed([quote])
    value["extracted_metadata"] = {"region": {"value": "深圳"}}
    return SimpleNamespace(parsed=value, filename="synthetic.pdf", original=None)


@pytest.mark.parametrize(
    "quote",
    [
        "深圳每晚住宿上限1,800元",
        "深圳每晚住宿上限-800元",
        "深圳每晚住宿上限人民币-800元",
        "深圳每晚住宿上限CNY 1,800",
    ],
)
def test_partial_money_token_cannot_be_used_as_800(quote):
    source = row(quote)
    with pytest.raises(APIError, match="填写的金额未出现在"):
        manual_candidate(source, Decimal("800"), 0, len(quote))


@pytest.mark.parametrize(
    "quote,blocker",
    [
        ("深圳每晚住宿上限800至1000元", "RULE_MULTIPLE_AMOUNTS"),
        ("深圳每晚住宿上限800/1000元", "RULE_MULTIPLE_AMOUNTS"),
        ("深圳2026-12-01起每晚住宿上限1000元", "RULE_DATE_CONDITIONS"),
        ("深圳按员工等级每晚住宿上限1000元", "RULE_CONDITIONS_UNSUPPORTED"),
        ("深圳每人每晚住宿上限1000元", "RULE_CONDITIONS_UNSUPPORTED"),
        ("深圳每晚住宿上限1000元，另付USD 20", "RULE_CURRENCY_SOURCE_REQUIRED"),
    ],
)
def test_conditional_manual_source_stays_reference(quote, blocker):
    source = row(quote)
    candidate = manual_candidate(source, Decimal("1000"), 0, len(quote))
    assert blocker in execution_blockers(source, candidate, "", extract_hotel_rule(source.parsed))


def test_low_confidence_manual_source_cannot_be_saved():
    source = row("深圳每晚住宿上限800元")
    source.parsed["lines"][0]["confidence"] = 0.6
    with pytest.raises(APIError, match="置信度不足"):
        manual_candidate(source, Decimal("800"), 0, len(source.parsed["text"]))


def test_legacy_dto_does_not_accept_manual_mode():
    body = dict(
        token="synthetic-local-capability-0000000000",
        actor="synthetic",
        reason="原文核对",
        confirmed=True,
        amount="800.00",
        clause_start=0,
        clause_end=20,
        expected_revision=0,
    )
    assert EnterpriseRuleConfirmation(**body).source_mode == "AUTOMATIC"
    assert EnterpriseRuleConfirmation(**body, source_mode="MANUAL").source_mode == "MANUAL"
    with pytest.raises(ValidationError):
        RuleConfirmation(**body, source_mode="MANUAL")


@pytest.mark.parametrize(
    "header,expected", [(["部门", "工作说明"], 0), (["地区", "住宿每晚标准"], 2)]
)
def test_reference_table_headers_do_not_classify_every_hotel_mention(header, expected):
    from test_enterprise_rule_recovery import document

    from integrations.ocr.local import LocalOCRProvider
    from integrations.policies.enterprise_rules import reference_tables
    from integrations.policies.parser import PolicyParser

    raw = document(["第六章 组织职责"], [header, ["深圳", "住宿需核对 800 元"]])
    parsed_document = PolicyParser(LocalOCRProvider()).parse(raw, "synthetic.docx")
    source = SimpleNamespace(original=raw, filename="synthetic.docx", parsed=parsed_document)
    references = reference_tables(source)
    assert len(references) == expected
    for reference in references:
        assert parsed_document["text"][reference["start"] : reference["end"]] == reference["quote"]
