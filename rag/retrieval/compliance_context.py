"""Expense-specific sufficient original evidence. Never prune required text for size."""

from decimal import Decimal
from hashlib import sha256

from rag.retrieval.compliance import HOTEL_2020_2030_SHA256, SOURCE_INTERVALS

COMMON = frozenset(
    {
        "第二条",
        "第三条",
        "第五条",
        "第六条",
        "第七条",
        "第八条",
        "第九条",
        "第三十六条",
        "第三十七条",
        "第三十八条",
        "第三十九条",
        "第四十条",
        "第四十一条",
        "第四十三条",
        "第四十四条",
        "第四十五条",
        "第五十一条",
        "第五十二条",
        "第五十三条",
        "第五十四条",
        "第五十六条",
        "第五十七条",
    }
)
CATEGORY = {
    "HOTEL": frozenset(
        {
            "第十一条",
            "第十二条",
            "第十三条",
            "第十四条",
            "第十五条",
            "第十六条",
            "第十七条",
            "第十八条",
        }
    ),
    "ALLOWANCE": frozenset(
        {
            "第十一条",
            "第十二条",
            "第十三条",
            "第十四条",
            "第二十五条",
            "第二十六条",
            "第二十七条",
            "第二十八条",
        }
    ),
    "ENTERTAINMENT": frozenset({"第二十九条", "第三十条", "第三十一条"}),
}
BASIS_CLAUSES = {
    "BUSINESS": {"第二条", "第三条", "第十二条", "第五十一条"},
    "POLICY": {
        "第五条",
        "第八条",
        "第九条",
        "第三十七条",
        "第四十五条",
        "第五十三条",
        "第五十四条",
        "第五十五条",
        "第五十六条",
        "第五十七条",
    },
    "EXCEPTION": {"第十一条", "第十三条", "第十六条", "第五十二条"},
    "HOTEL": {"第十四条", "第十五条", "第十六条", "第十七条", "第十八条", "第三十九条"},
    "ALLOWANCE": {"第二十五条", "第二十六条", "第二十七条", "第二十八条"},
    "ENTERTAINMENT": {"第二十九条", "第三十条", "第三十一条", "第三十九条"},
    "INVOICE": {"第三十八条", "第三十九条", "第四十条", "第四十一条"},
    "AUTHORIZATION": {
        "第二条",
        "第六条",
        "第七条",
        "第十一条",
        "第二十九条",
        "第三十条",
        "第三十一条",
        "第四十三条",
        "第四十四条",
        "第四十五条",
        "第五十二条",
    },
    "DUPLICATE": {"第十四条", "第十七条", "第二十八条", "第四十一条", "第四十三条", "第五十一条"},
}


def permits_basis(evidence, basis):
    table = evidence.source_position.get("table_index")
    return (
        evidence.clause_id in BASIS_CLAUSES[basis]
        or table
        in {
            "BUSINESS": {14},
            "POLICY": {3, 4, 6},
            "EXCEPTION": {15},
            "HOTEL": {5, 6},
            "ALLOWANCE": {10},
            "ENTERTAINMENT": {11},
            "INVOICE": set(),
            "AUTHORIZATION": {3, 14, 15},
            "DUPLICATE": set(),
        }[basis]
    )


def minimal_evidence(
    chunks,
    *,
    kind,
    grade,
    city_tier,
    amount,
    department="",
    entertainment_category=None,
    city="",
):
    if kind not in CATEGORY:
        raise ValueError("P4B_UNSUPPORTED_CONTEXT_CATEGORY")
    if not chunks or any(c.document_sha256 not in SOURCE_INTERVALS
                         or "附录" in c.section for c in chunks):
        raise ValueError("P4B_CONTEXT_SOURCE_INVALID")
    amendment = chunks[0].document_sha256 == HOTEL_2020_2030_SHA256
    if amendment and kind != "HOTEL":
        raise ValueError("P4B_HOTEL_AMENDMENT_ONLY")
    identities = {
        (c.document_id, c.document_sha256, c.policy_version_id, c.effective_from, c.effective_to)
        for c in chunks
    }
    if len(identities) != 1 or len({c.chunk_id for c in chunks}) != len(chunks):
        raise ValueError("P4B_CONTEXT_IDENTITY_CONFLICT")
    for c in chunks:
        position = {k: v for k, v in c.source_position.items() if k != "part"}
        digest = sha256((c.policy_version_id + str(position) + c.source_text).encode()).hexdigest()
        if c.chunk_id != "P4B-" + digest:
            raise ValueError("P4B_CONTEXT_CHUNK_INTEGRITY")
    transition = (not amendment and kind == "HOTEL" and grade in {"D", "UNKNOWN"}
                  and city in {"深圳", "深圳市", ""})
    clauses = COMMON | CATEGORY[kind] | ({"第五十五条"} if transition or amendment else set())
    if not clauses <= {c.clause_id for c in chunks}:
        raise ValueError("P4B_ESSENTIAL_EVIDENCE_MISSING")
    result = [c for c in chunks if c.clause_id in clauses]

    def table_rows(table, predicate):
        rows = [c for c in chunks if c.source_position.get("table_index") == table]
        selected = [c for c in rows if predicate(c.source_position["cells"])]
        if not selected:
            raise ValueError("P4B_ESSENTIAL_EVIDENCE_MISSING")
        result.extend(selected)

    tier = {"I": "Ⅰ", "II": "Ⅱ", "III": "Ⅲ", "IV": "Ⅳ"}.get(city_tier)
    table_rows(3, lambda r: grade == "UNKNOWN" or r[0] == grade)
    table_rows(4, lambda r: tier is None or r[0].startswith(tier + "类"))
    if kind == "HOTEL":
        table_rows(5, lambda r: grade == "UNKNOWN" or r[0].startswith(grade + " "))
        # The governed current interval; never pass historical/future amounts as simultaneous rules.
        if transition:
            table_rows(6, lambda r: r[0].startswith("2026-11-01"))
    elif kind == "ALLOWANCE":
        table_rows(10, lambda r: tier is None or r[0].startswith(tier + "类"))
    else:
        # Only GENERAL is machine executable. Other categories remain visible as unsupported scope.
        table_rows(
            11, lambda r: entertainment_category != "GENERAL" or r[0] == "一般客户/供应商工作餐"
        )
    value = Decimal(str(amount))
    bracket = 1 if value <= 1000 else 2 if value <= 5000 else 3 if value <= 20000 else 4
    authorization_rows = {f"TABLE-15-ROW-{bracket}", "TABLE-15-ROW-6"}
    if kind == "HOTEL":
        authorization_rows.add("TABLE-15-ROW-5")
    if not authorization_rows <= {c.clause_id for c in chunks}:
        raise ValueError("P4B_ESSENTIAL_EVIDENCE_MISSING")
    result.extend(c for c in chunks if c.clause_id in authorization_rows)
    department_rows = {
        row
        for name, row in {
            "销售": 1,
            "研发": 2,
            "供应链": 3,
            "采购": 3,
            "生产": 4,
            "交付": 4,
            "行政": 5,
            "人力": 5,
            "财务": 6,
            "法务": 6,
        }.items()
        if name in department
    }
    required_departments = {f"TABLE-14-ROW-{row}" for row in department_rows or range(1, 7)}
    if not required_departments <= {c.clause_id for c in chunks}:
        raise ValueError("P4B_ESSENTIAL_EVIDENCE_MISSING")
    result.extend(c for c in chunks if c.clause_id in required_departments)
    expected_tables = {3, 4, 14, 15} | (
        ({5, 6} if transition else {5})
        if kind == "HOTEL"
        else {10}
        if kind == "ALLOWANCE"
        else {11}
    )
    if not expected_tables <= {c.source_position.get("table_index") for c in result}:
        raise ValueError("P4B_ESSENTIAL_EVIDENCE_MISSING")
    return list({c.chunk_id: c for c in result}.values())
