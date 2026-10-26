"""Decimal hotel check; a monetary PASS grants no approval authority."""

from decimal import Decimal

from domain.schemas import Outcome, RuleResult, Severity


def calculate(fields, facts, policy):
    employee, city = facts.get("employee"), facts.get("city")
    grade = employee["value"]["grade"] if employee else "UNKNOWN"
    tier = city["value"]["tier"] if city else "UNKNOWN"
    result = dict(
        rule_id="HOTEL_LIMIT_MATRIX",
        policy_version_id=None,
        employee_grade=grade,
        city=fields.city,
        city_tier=tier,
        limit_per_night=None,
        nights=fields.nights,
        total_limit=None,
        claimed_amount=f"{fields.amount:.2f}",
        exceeded_amount=None,
        decision="UNKNOWN",
        source_reference=None,
        reason="关键事实或制度未核实",
    )
    blockers = list(facts.get("blockers", []))
    if not policy:
        blockers.append("POLICY_UNKNOWN_OR_CONFLICT")
    if blockers:
        result["reason"] = "; ".join(blockers)
        return result
    rules = policy.authoritative_rules
    if rules.get("rule_type") == "HOTEL_LIMIT_MATRIX":
        matrix = rules["matrix"]
        entries = [
            r for r in matrix["rules"] if r["employee_grade"] == grade and r["city_tier"] == tier
        ]
        # Known tier I/II cities must agree with the original controlled policy list.
        import re

        scopes = {c["city_tier"]: c["scope"] for c in matrix["city_classification"]}
        canonical = city["value"]["name"].removesuffix("市")
        listed = {t: set(re.split("[、，,]", scopes[t])) for t in ("I", "II")}
        listed_tier = next((t for t, names in listed.items() if canonical in names), None)
        valid = listed_tier == tier if tier in ("I", "II") else listed_tier is None
        valid &= (
            city["value"]["level"] in ("MUNICIPALITY", "PREFECTURE")
            if tier in ("I", "II", "III")
            else city["value"]["level"] in ("COUNTY", "TOWN")
        )
        if len(entries) != 1 or not valid:
            result["reason"] = "CITY_CLASSIFICATION_CONFLICT"
            return result
        cell = entries[0]
        limit = Decimal(cell["limit_per_room_per_night"])
        source = dict(
            **cell,
            source_document_id=rules["upload_id"],
            file_sha256=rules["file_sha256"],
            source_uri=policy.source_uri,
            title=policy.title,
            version=policy.version,
        )
    elif grade == "D" and city["value"]["name"].removesuffix("市") == "深圳":
        value = rules.get("hotel_limit_per_night")
        if not value:
            return result
        limit = Decimal(value)
        source = dict(
            source_uri=policy.source_uri,
            title=policy.title,
            version=policy.version,
            legacy_rule=True,
        )
    else:
        result["reason"] = "LEGACY_GRADE_SCOPE_UNKNOWN"
        return result
    total = limit * fields.nights
    result.update(
        policy_version_id=policy.policy_version_id,
        limit_per_night=f"{limit:.2f}",
        total_limit=f"{total:.2f}",
        exceeded_amount=f"{max(fields.amount - total, Decimal(0)):.2f}",
        decision="PASS" if fields.amount <= total else "FAIL",
        source_reference=source,
        reason=f"{grade}级 / {fields.city} / {tier}类：{limit:.2f} × {fields.nights} 晚 = "
        f"{total:.2f} 元；申请 {fields.amount:.2f} 元",
    )
    return result


def frozen_result(report):
    return RuleResult(
        rule_id=report["rule_id"],
        outcome=Outcome(report["decision"]),
        severity=Severity.BLOCK,
        reason=report["reason"],
        actual=report["claimed_amount"],
        limit=report["total_limit"],
        policy_version_id=report["policy_version_id"],
    )
