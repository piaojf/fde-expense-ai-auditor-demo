"""Independent P4A cases from the original synthetic Word; historic labels untouched."""

import io
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from domain.hotel_matrix import HotelFields, HotelSubmission
from integrations.policies.hotel_matrix import extract_matrix
from integrations.policies.parser import PolicyParser
from rules.hotel_matrix import calculate

SOURCE = Path(__file__).parent / "fixtures/policies/p4a-222.docx"
PARSER = PolicyParser(SimpleNamespace(binary="", languages="eng", tessdata=None))


def extraction(raw=None):
    raw = raw or SOURCE.read_bytes()
    return extract_matrix(raw, PARSER.parse(raw, "222.docx"))


def edit(old, new):
    out = io.BytesIO()
    with ZipFile(SOURCE) as z, ZipFile(out, "w") as target:
        for n in z.namelist():
            raw = z.read(n)
            if n == "word/document.xml":
                s = raw.decode("utf-8")
                assert old in s
                s = s.replace(old, new, 1)
                raw = s.encode()
            target.writestr(n, raw)
    return out.getvalue()


@pytest.mark.parametrize(
    "grade,expected",
    [
        ("A", ("1200.00", "1000.00", "850.00", "700.00")),
        ("B", ("1000.00", "850.00", "720.00", "600.00")),
        ("C", ("900.00", "750.00", "620.00", "500.00")),
        ("D", ("800.00", "650.00", "520.00", "420.00")),
    ],
)
def test_all_16_source_cells(grade, expected):
    x = extraction()
    assert x["status"] == "EXTRACTED"
    assert x["file_sha256"] == "65043838143a8d043130881b8fda35fd2573ed7fcd8716f1a33fc72f32bf1a98"
    row = [r for r in x["rules"] if r["employee_grade"] == grade]
    assert tuple(r["limit_per_room_per_night"] for r in row) == expected
    assert [r["source_column"] for r in row] == [2, 3, 4, 5]
    assert all(r["source_table"] == 5 and r["currency"] == "CNY" for r in row)


@pytest.mark.parametrize(
    "old,new",
    [
        ("1,200", "12,00"),
        ("D 普通员工", "C 普通员工"),
        ("Ⅳ类城市", "Ⅱ类城市"),
        ("每间每晚", "每人每天"),
        ("货币单位为元", "货币单位为美元"),
    ],
)
def test_ambiguous_source_never_produces_executable_cells(old, new):
    x = extraction(edit(old, new))
    assert x["status"] == "UNKNOWN" and not x["rules"]


def fields(city="深圳", amount="780.00", on="2026-11-02", nights=1):
    start = date.fromisoformat(on)
    from datetime import timedelta

    return HotelFields(
        applicant_name="张三",
        department="研发部",
        position="工程师",
        amount=amount,
        incurred_on=start,
        purpose="合成住宿",
        city=city,
        trip_start=start,
        trip_end=start + timedelta(days=nights),
        nights=nights,
        seller="合成酒店",
    )


def facts(grade="D", city="深圳", tier="I", level="PREFECTURE"):
    return dict(
        employee={"value": {"grade": grade}},
        city={"value": {"name": city, "tier": tier, "level": level}},
        blockers=[],
    )


def policy():
    return SimpleNamespace(
        authoritative_rules=dict(
            rule_type="HOTEL_LIMIT_MATRIX",
            matrix=extraction(),
            upload_id="synthetic-source",
            file_sha256=extraction()["file_sha256"],
        ),
        policy_version_id="synthetic-published-matrix",
        source_uri="upload://synthetic",
        title="XX公司费用报销与差旅管理制度",
        version="2026-11-V2",
    )


@pytest.mark.parametrize(
    "city,grade,tier,level,amount,limit,decision",
    [
        ("深圳", "D", "I", "PREFECTURE", "780", "800.00", "PASS"),
        ("深圳", "D", "I", "PREFECTURE", "850", "800.00", "FAIL"),
        ("深圳", "B", "I", "PREFECTURE", "980", "1000.00", "PASS"),
        ("杭州", "D", "II", "PREFECTURE", "680", "650.00", "FAIL"),
        ("青田县", "D", "IV", "COUNTY", "400", "420.00", "PASS"),
    ],
)
def test_t02_to_t06(city, grade, tier, level, amount, limit, decision):
    r = calculate(fields(city, amount), facts(grade, city, tier, level), policy())
    assert r["limit_per_night"] == limit and r["decision"] == decision
    assert r["source_reference"]["source_table"] == 5


def test_t01_old_700_and_decimal_multi_nights():
    old = SimpleNamespace(
        authoritative_rules={"hotel_limit_per_night": "700.00"},
        policy_version_id="POL-SZ-2026",
        source_uri="seed://2026",
        title="旧制度",
        version="2026",
    )
    r = calculate(fields(on="2026-10-20"), facts(), old)
    assert r["decision"] == "FAIL" and r["exceeded_amount"] == "80.00"
    r = calculate(fields(amount="1600.01", nights=2), facts(), policy())
    assert r["total_limit"] == "1600.00" and r["exceeded_amount"] == "0.01"


@pytest.mark.parametrize(
    "blocker",
    ["EMPLOYEE_GRADE_UNKNOWN", "CITY_TIER_UNKNOWN", "STAY_UNVERIFIED", "STAY_CITY_MISMATCH"],
)
def test_untrusted_facts_unknown(blocker):
    f = facts()
    f["blockers"] = [blocker]
    assert calculate(fields(), f, policy())["decision"] == "UNKNOWN"


def test_city_cross_tier_and_county_not_inferred():
    assert calculate(fields(), facts(tier="IV", level="COUNTY"), policy())["decision"] == "UNKNOWN"
    assert (
        calculate(fields("杭州"), facts(city="杭州", tier="I"), policy())["decision"] == "UNKNOWN"
    )


def test_client_cannot_set_authoritative_grade():
    with pytest.raises(ValidationError):
        HotelSubmission(
            fields=fields().model_dump(), stay_id="x", idempotency_key="p4a-forgery", grade="A"
        )


@pytest.mark.parametrize("grade", ["A", "B", "C", "D"])
@pytest.mark.parametrize(
    "city,tier,level",
    [
        ("深圳", "I", "PREFECTURE"),
        ("杭州", "II", "PREFECTURE"),
        ("合成地市城区", "III", "PREFECTURE"),
        ("青田县", "IV", "COUNTY"),
    ],
)
def test_each_original_cell_drives_decimal_boundary(grade, city, tier, level):
    from decimal import Decimal

    cell = next(
        r for r in extraction()["rules"] if (r["employee_grade"], r["city_tier"]) == (grade, tier)
    )
    limit = Decimal(cell["original_cell"].replace(",", ""))
    p = policy()
    exact = calculate(fields(city, str(limit * 3), nights=3), facts(grade, city, tier, level), p)
    over = calculate(
        fields(city, str(limit * 3 + Decimal("0.01")), nights=3), facts(grade, city, tier, level), p
    )
    assert exact["decision"] == "PASS" and exact["total_limit"] == f"{limit * 3:.2f}"
    assert over["decision"] == "FAIL" and over["exceeded_amount"] == "0.01"
    assert exact["source_reference"]["original_row"] == cell["original_row"]


def test_declared_twelve_pages_still_rejected_by_existing_safety_guard():
    from integrations.policies.worker import docx

    out = io.BytesIO()
    with ZipFile(SOURCE) as source, ZipFile(out, "w") as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == "docProps/app.xml":
                raw = raw.replace(b"</Properties>", b"<Pages>12</Pages></Properties>")
            target.writestr(name, raw)
    with pytest.raises(ValueError, match="FILE_COMPLEXITY_LIMIT"):
        docx(out.getvalue())


@pytest.mark.parametrize("mode", ["duplicate", "missing", "partial"])
def test_invalid_actual_table_structure_has_no_rules(mode):
    from copy import deepcopy
    from xml.etree.ElementTree import tostring

    from integrations.policies.worker import WORD_NS as W
    from integrations.policies.worker import xml

    out = io.BytesIO()
    with ZipFile(SOURCE) as source, ZipFile(out, "w") as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == "word/document.xml":
                root = xml(raw)
                body = root.find(W + "body")
                table = body.findall(W + "tbl")[4]
                if mode == "duplicate":
                    body.insert(list(body).index(table) + 1, deepcopy(table))
                elif mode == "missing":
                    body.remove(table)
                else:
                    table.remove(table.findall(W + "tr")[-1])
                raw = tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(name, raw)
    result = extraction(out.getvalue())
    assert result["status"] == "UNKNOWN" and result["rules"] == []
