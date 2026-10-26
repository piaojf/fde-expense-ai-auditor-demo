"""Read actual OOXML cells only after the established DOCX safety validator."""

import hashlib
import io
import re
import unicodedata
import zipfile
from decimal import Decimal

from integrations.policies.worker import WORD_NS as W
from integrations.policies.worker import docx, xml

GRADES = ("A", "B", "C", "D")
TIERS = ("I", "II", "III", "IV")


def text(element):
    return "".join(e.text or "" for e in element.iter(W + "t")).strip()


def tier(value):
    normalized = unicodedata.normalize("NFKC", value).replace(" ", "")
    match = re.fullmatch(r"(IV|III|II|I)类(?:城市)?", normalized)
    return match[1] if match else None


def amount(value):
    # Thousands separators must form complete groups, never delete arbitrary punctuation.
    if not re.fullmatch(r"(?:[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d{1,2})?", value):
        raise ValueError("MATRIX_AMOUNT_INVALID")
    value = Decimal(value.replace(",", ""))
    if not value.is_finite() or not 0 < value < Decimal("10000000000"):
        raise ValueError("MATRIX_AMOUNT_INVALID")
    return f"{value:.2f}"


def extract_matrix(raw, parsed):
    """No executable rule is returned when source structure, currency or units are ambiguous."""
    blockers, rules, cities = [], [], []
    try:
        safe = docx(raw)  # Keep ZIP/XML/page/embedded object limits intact.
        if [x["text"] for x in safe["lines"]] != [x["text"] for x in parsed["lines"]]:
            raise ValueError("PARSE_INTEGRITY")
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            document = xml(package.read("word/document.xml"))
        body = document.find(W + "body")
        if body is None:
            raise ValueError("MATRIX_MISSING")
        table_no, matrices, section = 0, [], ""
        for element in body:
            if element.tag == W + "p":
                if re.match(r"第[一二三四五六七八九十]+章", text(element)):
                    section = text(element)
                continue
            if element.tag != W + "tbl":
                continue
            table_no += 1
            rows = element.findall(W + "tr")
            cells = [[text(c) for c in row.findall(W + "tc")] for row in rows]
            if not cells or not cells[0]:
                continue
            if (
                "城市分级" in section
                and "城市档位" == cells[0][0]
                and len(cells[0]) == 4
                and "城市范围" in cells[0][1]
                and len(cells) == 5
            ):
                if any(e.tag in {W + "vMerge", W + "gridSpan"} for e in element.iter()):
                    raise ValueError("CITY_SOURCE_AMBIGUOUS")
                for ri, row in enumerate(cells[1:], 2):
                    if len(row) != 4:
                        raise ValueError("CITY_SOURCE_AMBIGUOUS")
                    label = unicodedata.normalize("NFKC", row[0]).split("类")[0]
                    if label not in TIERS or len(row) != 4:
                        raise ValueError("CITY_SOURCE_AMBIGUOUS")
                    cities.append(
                        dict(
                            city_tier=label,
                            scope=row[1],
                            source_table=table_no,
                            source_row=ri,
                            original_cells=row,
                        )
                    )
            if sum(tier(c) is not None for c in cells[0]) < 2:
                continue
            if "住宿" not in section:
                continue
            if len(cells) != 5 or any(len(row) != 5 for row in cells):
                raise ValueError("MATRIX_SHAPE_INVALID")
            if any(e.tag in {W + "vMerge", W + "gridSpan"} for e in element.iter()):
                raise ValueError("MATRIX_MERGED_CELLS")
            headers = [tier(c) for c in cells[0][1:]]
            if set(headers) != set(TIERS) or len(set(headers)) != 4:
                raise ValueError("MATRIX_HEADERS_INVALID")
            if "每间每晚" not in cells[0][0]:
                raise ValueError("MATRIX_UNIT_UNKNOWN")
            if not re.search(r"人民币金额.*?货币单位为元", parsed["text"]):
                raise ValueError("MATRIX_CURRENCY_UNKNOWN")
            for ri, row in enumerate(cells[1:], 2):
                grade = re.match(r"^([A-D])(?:\s|$)", row[0])
                if grade is None:
                    raise ValueError("MATRIX_GRADE_INVALID")
                for ci, value in enumerate(row[1:], 2):
                    rules.append(
                        dict(
                            employee_grade=grade[1],
                            city_tier=headers[ci - 2],
                            currency="CNY",
                            limit_per_room_per_night=amount(value),
                            source_table=table_no,
                            source_row=ri,
                            source_column=ci,
                            original_cell=value,
                            original_row=row,
                            original_header=cells[0],
                            source_section=section,
                        )
                    )
            matrices.append(table_no)
        if len(matrices) != 1:
            raise ValueError("MATRIX_MISSING_OR_DUPLICATE")
        if {(r["employee_grade"], r["city_tier"]) for r in rules} != {
            (g, t) for g in GRADES for t in TIERS
        } or len(rules) != 16:
            raise ValueError("MATRIX_CELLS_MISSING_OR_DUPLICATE")
        if len(cities) != 4 or {r["city_tier"] for r in cities} != set(TIERS):
            raise ValueError("CITY_SOURCE_MISSING")
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        blockers.append(str(exc))
    sha = hashlib.sha256(raw).hexdigest()
    return dict(
        rule_type="HOTEL_LIMIT_MATRIX",
        status="UNKNOWN" if blockers else "EXTRACTED",
        file_sha256=sha,
        rules=[] if blockers else rules,
        city_classification=cities,
        blockers=blockers,
    )
