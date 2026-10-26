"""Independent public matrix tests. These never impersonate the excluded policy."""

import hashlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from domain.hotel_matrix import HotelFields
from integrations.policies.hotel_matrix import extract_matrix
from integrations.policies.parser import PolicyParser
from rag.retrieval.compliance import original_chunks
from rules.hotel_matrix import calculate
from scripts.generate_public_policy import GRADES, LIMITS, OUT, build

PARSER = PolicyParser(SimpleNamespace(binary="", languages="eng", tessdata=None))


def matrix(raw=None):
    raw = build() if raw is None else raw
    return extract_matrix(raw, PARSER.parse(raw, OUT.name))


def test_synthetic_provenance_and_reproducible_package():
    assert OUT.read_bytes() == build()
    parsed = PARSER.parse(build(), OUT.name)
    assert "Synthetic Policy Fixture" in parsed["text"]
    assert "SYNTHETIC-LABS" in parsed["text"]
    assert "[2026-11-01, 2027-01-01)" in parsed["text"]
    assert matrix()["file_sha256"] == hashlib.sha256(build()).hexdigest()
    assert matrix()["status"] == "EXTRACTED"


@pytest.mark.parametrize("grade", GRADES)
@pytest.mark.parametrize("tier", ("I", "II", "III", "IV"))
def test_every_synthetic_cell_boundary_and_source_attribution(grade, tier):
    from decimal import Decimal

    x = matrix()
    cell = next(r for r in x["rules"] if (r["employee_grade"], r["city_tier"]) == (grade, tier))
    limit = Decimal(LIMITS[GRADES.index(grade)][("I", "II", "III", "IV").index(tier)])
    assert cell["limit_per_room_per_night"] == f"{limit:.2f}"
    cities = {
        "I": ("深圳", "PREFECTURE"),
        "II": ("杭州", "PREFECTURE"),
        "III": ("示例地市", "PREFECTURE"),
        "IV": ("示例县", "COUNTY"),
    }
    city, level = cities[tier]
    policy = SimpleNamespace(
        authoritative_rules={
            "rule_type": "HOTEL_LIMIT_MATRIX",
            "matrix": x,
            "upload_id": "public-synthetic",
            "file_sha256": x["file_sha256"],
        },
        policy_version_id="PUBLIC-SYNTHETIC-V1",
        source_uri="synthetic://public-v1",
        title="虚构示例实验室",
        version="V1",
    )
    fields = HotelFields(
        applicant_name="合成员工",
        department="示例部",
        position="职员",
        amount=str(limit),
        incurred_on=date(2026, 11, 2),
        purpose="合成差旅",
        city=city,
        trip_start=date(2026, 11, 2),
        trip_end=date(2026, 11, 2) + timedelta(days=1),
        nights=1,
        seller="合成酒店",
    )
    facts = {
        "employee": {"value": {"grade": grade}},
        "city": {"value": {"name": city, "tier": tier, "level": level}},
        "blockers": [],
    }
    result = calculate(fields, facts, policy)
    assert result["decision"] == "PASS"
    assert result["source_reference"]["source_table"] == 2
    assert result["source_reference"]["original_row"] == cell["original_row"]
    fields.amount = limit + Decimal("0.01")
    assert calculate(fields, facts, policy)["decision"] == "FAIL"
    facts["blockers"] = ["EMPLOYEE_GRADE_UNKNOWN"]
    assert calculate(fields, facts, policy)["decision"] == "UNKNOWN"


def test_public_fixture_cannot_bypass_private_p4b_source_pin():
    with pytest.raises(ValueError, match="P4B_SOURCE_SHA_MISMATCH"):
        original_chunks(
            build(),
            document_id="public",
            policy_version_id="public",
            effective_from=date(2026, 11, 1),
            effective_to=date(2027, 1, 1),
        )
