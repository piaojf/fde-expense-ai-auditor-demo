"""Conservative, local extraction. Clause provenance is mandatory, never invented."""

import re
from decimal import Decimal, InvalidOperation

# Explicit currency and per-night unit are required. No general number guessing.
PATTERNS = (
    r"(?:住宿|酒店)[^\n。;；]{0,40}?(?:每晚|每夜)[^\n。;；]{0,15}?"
    r"(?:限额|标准|上限)[\s:：]*(\d+(?:\.\d{1,2})?)\s*(?:元|人民币|CNY)",
    r"(?:住宿|酒店)[^\n。;；]{0,40}?(?:限额|标准|上限)[\s:：]*"
    r"(\d+(?:\.\d{1,2})?)\s*(?:元|人民币|CNY)\s*(?:/\s*(?:晚|夜)|每晚|每夜)",
    r"(?:hotel|accommodation)[^\n.;]{0,60}?limit[^\n.;]{0,20}?"
    r"CNY\s*(\d+(?:\.\d{1,2})?)\s*per\s*night",
)


def extract_hotel_rule(parsed):
    text, candidates, uncertain = parsed.get("text", ""), [], False
    for line in parsed.get("lines", []):
        if text[line["start"] : line["end"]] != line["text"]:
            return dict(status="UNKNOWN", candidates=[], blockers=["SOURCE_SPAN_INVALID"])
        matches = [m for p in PATTERNS for m in re.finditer(p, line["text"], re.I)]
        values = set()
        for m in matches:
            try:
                amount = Decimal(m[1])
                if amount <= 0 or amount >= Decimal("10000000000"):
                    uncertain = True
                    continue
                values.add(f"{amount:.2f}")
            except InvalidOperation:
                uncertain = True
        if values:
            # One hotel clause mentioning multiple monetary standards is ambiguous,
            # even if only the first repeats the hotel/per-night label.
            money_mentions = re.findall(
                r"CNY\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|人民币)",
                line["text"],
                re.I,
            )
            for english, chinese in money_mentions:
                try:
                    mentioned = Decimal(english or chinese)
                    if (
                        mentioned <= 0
                        or mentioned >= Decimal("10000000000")
                        or mentioned.as_tuple().exponent < -2
                    ):
                        uncertain = True
                    elif mentioned > 0:
                        values.add(f"{mentioned:.2f}")
                except InvalidOperation:
                    uncertain = True
            uncertain |= bool(re.search(r"USD|EUR|港币|美元", line["text"], re.I))
            uncertain |= line.get("confidence") is not None and line["confidence"] < 0.85
            # Multiple values in one line are conflict, never select a convenient number.
            for amount in sorted(values):
                candidates.append(
                    dict(
                        amount=amount,
                        currency="CNY",
                        quote=line["text"],
                        start=line["start"],
                        end=line["end"],
                        source=line,
                    )
                )
        elif re.search(r"住宿|酒店|hotel|accommodation", line["text"], re.I) and re.search(
            r"限额|标准|上限|limit", line["text"], re.I
        ):
            uncertain = True
    amounts = {c["amount"] for c in candidates}
    blockers = []
    if not candidates:
        blockers.append("HOTEL_LIMIT_MISSING")
    if len(amounts) > 1:
        blockers.append("CONFLICTING_HOTEL_LIMITS")
    if uncertain:
        blockers.append("EXTRACTION_UNCERTAIN")
    return dict(
        status="UNKNOWN" if blockers else "EXTRACTED",
        candidates=candidates,
        blockers=blockers,
        rule_type="HOTEL_LIMIT_PER_NIGHT",
    )
