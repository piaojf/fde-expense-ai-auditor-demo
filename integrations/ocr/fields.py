"""Conservative label-based extraction. Ambiguous candidates remain UNKNOWN."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from domain.invoices import RecognizedField

LABELS = {
    "invoice_number": r"(?:Invoice\s*(?:Number|No\.?|#)|发票号码|票据号码)",
    "invoice_code": r"(?:Invoice\s*Code|发票代码)",
    "issue_date": r"(?:Issue\s*Date|Invoice\s*Date|开票日期)",
    "seller_name": r"(?:Seller(?:\s*Name)?|销售方(?:名称)?|开票单位)",
    "buyer_name": r"(?:Buyer(?:\s*Name)?|购买方(?:名称)?)",
    "total_amount": r"(?:Total\s*Amount|Grand\s*Total|价税合计(?:\s*\(小写\)|\s*（小写）)?)",
    "tax_amount": r"(?:Tax\s*Amount|税额)",
}
AMOUNT = r"\d+(?:,\d{3})*(?:\s*\.\s*\d{1,2})?(?![\d.,]|\s+\d)"


def normalize(key, value):
    value = value.strip().strip(":：").strip()
    if key in ("invoice_number", "invoice_code"):
        value = re.sub(r"\s", "", value)
        return value if re.fullmatch(r"[A-Za-z0-9-]{4,64}", value) else None
    if key == "issue_date":
        match = re.fullmatch(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?", value)
        if not match:
            return None
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            return None
    if key.endswith("amount"):
        if re.search(r"\d\s+\d", value):
            return None  # Separate numeric fragments are not a verified amount.
        value = re.sub(r"(?:CNY|RMB|元|[¥￥,\s])", "", value, flags=re.I)
        if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", value):
            return None
        try:
            number = Decimal(value)
            if number < 0 or (key == "total_amount" and number == 0):
                return None
            return f"{number:.2f}"
        except InvalidOperation:
            return None
    if any(mask in value for mask in ("*", "�", "█", "...", "…")):
        return None
    return value if 1 <= len(value) <= 200 else None


def chinese_spacing(text):
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)


def value_confidence(row, value):
    # A seal, QR code or a neighbouring table cell must not dilute legible digits.
    # Keep word confidence as OCR evidence, never as business correctness.
    compact = re.sub(r"\s+", "", value)
    words = row.get("words", [])
    for start in range(len(words)):
        joined, scores = "", []
        for word in words[start:]:
            joined += re.sub(r"\s+", "", word["text"])
            scores.append(word["confidence"])
            if compact in joined:
                return sum(scores) / len(scores)
            if len(joined) > len(compact) + 4:
                break
    return row.get("confidence")


def aligned(left, right):
    if left["page"] != right["page"] or not left.get("bbox") or not right.get("bbox"):
        return False
    a, b = left["bbox"], right["bbox"]
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap > 0.4 * min(a[3] - a[1], b[3] - b[1])


def extract_fields(lines):
    candidates = {key: [] for key in LABELS}

    def add(key, raw, row, *, strict_name=False):
        value = normalize(key, raw)
        confidence = value_confidence(row, raw)
        threshold = 0.8 if strict_name else 0.55
        if value and (confidence is None or confidence >= threshold):
            candidates[key].append((value, row, confidence))

    # Several labels may share a line in a tax invoice. Match their own value,
    # stopping at the next label rather than requiring a whole-line match.
    stops = "|".join(LABELS.values()) + r"|校验码|机器编号|纳税人识别号"
    for key, label in LABELS.items():
        for row in lines:
            text = chinese_spacing(row["text"])
            if text in ("销售方信息", "购买方信息"):
                continue
            for match in re.finditer(label + r"\s*[:：]?\s*", text, re.I):
                tail = re.split(stops, text[match.end() :], maxsplit=1, flags=re.I)[0].strip()
                if key == "issue_date":
                    date_value = re.match(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?", tail)
                    if date_value:
                        add(key, date_value[0], row)
                elif key == "total_amount" and "价税合计" in match[0]:
                    small = re.search(
                        r"[（(]?小写[）)]?\s*[:：]?\s*([¥￥]?\s*" + AMOUNT + ")", tail
                    )
                    currency = re.findall(r"[¥￥]\s*" + AMOUNT, tail)
                    if small:
                        add(key, small[1], row)
                    elif len(currency) == 1:
                        add(key, currency[0], row)
                    else:
                        add(key, tail, row)
                else:
                    # The footer's 销售方：(章) is a signature, not its name.
                    if key.endswith("name") and re.match(r"[（(]?\s*章", tail):
                        continue
                    add(key, tail, row)

    # Split-column scans can put the total label and numeric amount in separate
    # OCR blocks. Require a same-page, same-row currency value to its right.
    totals = [r for r in lines if "价税合计" in chinese_spacing(r["text"]) and r.get("bbox")]
    for anchor in totals:
        for row in lines:
            if row is anchor or not aligned(anchor, row) or row["bbox"][0] <= anchor["bbox"][2]:
                continue
            amounts = re.findall(r"[¥￥]\s*" + AMOUNT, row["text"])
            if len(amounts) == 1:
                add("total_amount", amounts[0], row)

    # In standard VAT tables, the buyer name is above the grand total and the
    # seller below it. Use an explicit 名称 label and a complete organisation
    # suffix; never copy an address, masked name or arbitrary OCR text as a name.
    for row in lines:
        if not row.get("bbox"):
            continue
        text = chinese_spacing(row["text"])
        if re.search(LABELS["seller_name"] + "|" + LABELS["buyer_name"], text, re.I):
            continue  # Explicit role labels take precedence over table position.
        name = re.search(
            r"名称\s*[:：]?\s*([^:：|*�]{2,100}?"
            r"(?:有限责任公司|有限公司|酒店(?!有限公司)|宾馆(?!有限公司)|公司|中心))",
            text,
        )
        if not name:
            continue
        # Prefer explicit vertical roles beside a name over legacy table position.
        role_anchors = [
            a
            for a in lines
            if a.get("bbox")
            and a["page"] == row["page"]
            and chinese_spacing(a["text"]) in ("销售方信息", "购买方信息")
            and a["bbox"][2] < row["bbox"][2]
            and 0 <= row["bbox"][0] - a["bbox"][0] < 60
            and a["bbox"][1] <= row["bbox"][3]
            and a["bbox"][3] >= row["bbox"][1]
        ]
        if role_anchors:
            roles = {"seller_name" if "销售" in a["text"] else "buyer_name" for a in role_anchors}
            if len(roles) == 1:
                add(roles.pop(), name[1], row, strict_name=True)
            continue
        anchors = [
            a
            for a in totals
            if a["page"] == row["page"] and not aligned(a, row) and row["bbox"][0] < a["bbox"][2]
        ]
        roles = {"buyer_name" if row["bbox"][3] < a["bbox"][1] else "seller_name" for a in anchors}
        if len(roles) == 1:
            add(roles.pop(), name[1], row, strict_name=True)

    fields = {}
    for key, values in candidates.items():
        if len({v for v, _, _ in values}) == 1:
            value, row, confidence = max(values, key=lambda item: item[2] or 0)
            fields[key] = RecognizedField(
                value=value,
                status="RECOGNIZED",
                confidence=confidence,
                page=row["page"],
                line=row["line"],
                source=row["method"],
            )
        else:
            fields[key] = RecognizedField()
    return fields
