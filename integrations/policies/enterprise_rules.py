"""Source-backed enterprise references; conditional tables never imply execution."""

import re
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

from apps.api.policy_publication_store import fail
from integrations.policies.worker import WORD_NS, xml

CONDITIONAL = re.compile(
    r"职级|城市分级|城市档位|[A-DＡ-Ｄ]\s*[级档]|高管|特殊|例外|旧标准|原标准|旧版本"
    r"|最低|至少|不得低于|不低于|押金|补贴|折扣|仅限|级别|等级|岗位|每人|每位|人均"
    r"|\b(?:grade|level|exception|special)\b"
    r"|(?:hotel|accommodation|limit)[^\n]{0,60}\b(?:unless|only|if)\b"
    r"|\b(?:unless|only|if)\b[^\n]{0,60}(?:hotel|accommodation|limit)"
    r"|\b(?:manager|director)\s+(?:rate|limit|standard)\b",
    re.I,
)
MONEY = re.compile(
    r"(?:CNY|人民币)\s*(\d+(?:\.\d+)?)(?![\d.,，])"
    r"|(?<![-−\d.,，])(\d+(?:\.\d+)?)\s*(?:元|人民币)",
    re.I,
)


def money_values(quote):
    return {Decimal(a or b) for a, b in MONEY.findall(quote)}


def reference_tables(row, *, all_tables=False):
    """Match actual DOCX table paragraphs to existing immutable parser positions."""
    if not row.filename.lower().endswith(".docx") or not row.original:
        return []
    with ZipFile(BytesIO(row.original)) as package:
        document = xml(package.read("word/document.xml"))
    paragraphs = list(document.iter(WORD_NS + "p"))
    positions = {id(p): i + 1 for i, p in enumerate(paragraphs)}
    lines = {line["line"]: line for line in row.parsed["lines"]}
    result, hotel_section = [], False
    body = document.find(WORD_NS + "body")
    if body is None:
        return []
    for child in body:
        if child.tag == WORD_NS + "p":
            title = "".join(e.text or "" for e in child.iter(WORD_NS + "t"))
            if re.match(r"第[一二三四五六七八九十]+章|\d+\.\d+", title):
                hotel_section = bool(re.search(r"住宿|酒店", title))
        elif child.tag == WORD_NS + "tbl":
            header = child.find(WORD_NS + "tr")
            header_text = (
                "" if header is None else "".join(e.text or "" for e in header.iter(WORD_NS + "t"))
            )
            if (
                not all_tables
                and not hotel_section
                and not (
                    re.search(r"住宿|酒店", header_text)
                    and re.search(r"标准|限额|上限", header_text)
                )
            ):
                continue
            table_lines = [
                lines[positions[id(p)]]
                for p in child.iter(WORD_NS + "p")
                if positions[id(p)] in lines
            ]
            for tr in child.findall(WORD_NS + "tr"):
                source = [
                    lines[positions[id(p)]]
                    for p in tr.iter(WORD_NS + "p")
                    if positions[id(p)] in lines
                ]
                if not source:
                    continue
                start, end = source[0]["start"], source[-1]["end"]
                quote = row.parsed["text"][start:end]
                cells = [
                    " ".join(e.text or "" for e in tc.iter(WORD_NS + "t"))
                    for tc in tr.findall(WORD_NS + "tc")
                ]
                result.append(
                    dict(
                        cells=cells,
                        quote=quote,
                        start=start,
                        end=end,
                        line=source[0]["line"],
                        end_line=source[-1]["line"],
                        **(
                            {
                                "table_start": table_lines[0]["start"],
                                "table_end": table_lines[-1]["end"],
                                "header": header_text,
                            }
                            if all_tables and table_lines
                            else {}
                        ),
                    )
                )
    return result


def manual_candidate(row, amount, start, end):
    lines = row.parsed["lines"]
    selected = [line for line in lines if start <= line["start"] and line["end"] <= end]
    if (
        not selected
        or end <= start
        or end - start > 2000
        or selected[0]["start"] != start
        or selected[-1]["end"] != end
    ):
        fail("RULE_SOURCE_MISMATCH", "请选择连续的完整原文段落，引用长度须在 2000 字以内")
    if any(line.get("confidence") is not None and line["confidence"] < 0.85 for line in selected):
        fail("RULE_SOURCE_UNCERTAIN", "所选原文 OCR 置信度不足，请上传清晰文件后重新核对")
    quote = row.parsed["text"][start:end]
    values = money_values(quote)
    # Bare numeric table cells are evidence for a reference draft, never a nightly rule.
    for table in reference_tables(row):
        if start <= table["start"] and table["end"] <= end:
            values.update(
                Decimal(line["text"].strip())
                for line in selected
                if table["start"] <= line["start"]
                and line["end"] <= table["end"]
                and re.fullmatch(r"\d+(?:\.\d{1,2})?", line["text"].strip())
            )
    if amount not in values:
        fail("RULE_SOURCE_MISMATCH", "填写的金额未出现在所选原文中，请重新选择条款或表格完整行")
    return dict(
        amount=f"{amount:.2f}",
        currency="CNY",
        quote=quote,
        start=start,
        end=end,
        source={**selected[0], "end_line": selected[-1]["line"]},
    )


def execution_blockers(row, candidate, conditions, extraction):
    quote = candidate["quote"]
    blockers = []
    if conditions or CONDITIONAL.search(row.parsed["text"]):
        blockers.append("RULE_CONDITIONS_UNSUPPORTED")
    if re.search(r"\d{4}\s*[-/.年]\s*\d{1,2}|\d+\s*月\s*\d+\s*日", quote):
        blockers.append("RULE_DATE_CONDITIONS")
    if re.search(r"\d+(?:\.\d+)?\s*(?:至|到|[~～—–/、])\s*\d", quote):
        blockers.append("RULE_MULTIPLE_AMOUNTS")
    if reference_tables(row):
        blockers.append("RULE_TABLE_REVIEW_REQUIRED")
    if not re.search(r"住宿|酒店|hotel|accommodation", quote, re.I):
        blockers.append("RULE_HOTEL_SOURCE_REQUIRED")
    if not re.search(r"每(?:间|人)?(?:每)?晚|每夜|/\s*(?:晚|夜)|per\s*night", quote, re.I):
        blockers.append("RULE_NIGHT_UNIT_REQUIRED")
    if not re.search(r"限额|上限|标准|不(?:得)?超过|limit", quote, re.I):
        blockers.append("RULE_LIMIT_SOURCE_REQUIRED")
    if re.search(r"USD|EUR|港币|美元|欧元", quote, re.I) or not re.search(
        r"元|人民币|CNY", quote, re.I
    ):
        blockers.append("RULE_CURRENCY_SOURCE_REQUIRED")
    values = money_values(quote)
    if len(values) != 1 or Decimal(candidate["amount"]) not in values:
        blockers.append("RULE_MULTIPLE_AMOUNTS")
    if candidate["source"].get("end_line") is not None:
        region = row.parsed["extracted_metadata"]["region"]["value"]
        if region not in quote:
            blockers.append("RULE_REGION_SOURCE_REQUIRED")
    if "CONFLICTING_HOTEL_LIMITS" in extraction["blockers"]:
        blockers.append("CONFLICTING_HOTEL_LIMITS")
    # A manually selected phrase must not hide another unexplained numeric hotel standard.
    for line in row.parsed["lines"]:
        if candidate["start"] <= line["start"] and line["end"] <= candidate["end"]:
            continue
        if (
            re.search(r"住宿|酒店|hotel|accommodation", line["text"], re.I)
            and re.search(r"限额|上限|标准|limit", line["text"], re.I)
            and re.search(r"\d", line["text"])
        ):
            other = money_values(line["text"])
            if other != {Decimal(candidate["amount"])}:
                blockers.append("RULE_OTHER_STANDARDS")
                break
    return list(dict.fromkeys(blockers))
