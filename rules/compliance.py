"""Deterministic P4B monetary extensions; candidate employee facts never pay money."""

from decimal import Decimal

from domain.schemas import RuleResult
from rules.allowance import full_day_allowance


def calculate(expense, kind, verified, source, city_tier, trip_days):
    common = dict(
        rule_id="P4B_" + kind,
        outcome="UNKNOWN",
        severity="REVIEW",
        reason="缺少已核实事实或已确认规则",
        actual=expense.amount,
        limit=None,
        policy_version_id=source["policy_version_id"],
    )
    rules = source["rules"]
    if kind == "ALLOWANCE":
        days, lunches, dinners = (verified.get(k) for k in ("full_days", "lunches", "dinners"))
        if (
            days is None
            or lunches is None
            or dinners is None
            or city_tier not in rules["allowances"]
        ):
            return RuleResult(**common)
        if days > trip_days:
            return RuleResult(**{**common, "reason": "核实补助天数超出行程"})
        base = Decimal(rules["allowances"][city_tier]["amount"])
        rates = rules["meal_rates"]
        limit = full_day_allowance(
            base,
            days,
            lunches=lunches,
            dinners=dinners,
            lunch_rate=Decimal(rates["lunch"]) / 100,
            dinner_rate=Decimal(rates["dinner"]) / 100,
        )
        reason = f"{base} × ({days} − {lunches}×{rates['lunch']}% − {dinners}×{rates['dinner']}%)"
    elif kind == "ENTERTAINMENT":
        if verified.get("entertainment_category") != "GENERAL":
            return RuleResult(**{**common, "reason": "一般客户招待类别未核实或不属本轮规则范围"})
        people = verified.get("participants")
        if people is None:
            return RuleResult(**{**common, "reason": "缺少经核实的参与人数，不能计算人均限额"})
        standard = rules["entertainment"]
        per_person, per_event = Decimal(standard["per_person"]), Decimal(standard["per_event"])
        limit = min(per_person * people, per_event)
        reason = (
            f"一般客户招待 {people} 人；人均 {expense.amount / people:.2f} 元；"
            f"上限 min({per_person}×{people}, {per_event})"
        )
    else:
        return RuleResult(**common)
    return RuleResult(
        **{
            **common,
            "limit": limit,
            "severity": "BLOCK",
            "outcome": "PASS" if expense.amount <= limit else "FAIL",
            "reason": reason + f" = {limit:.2f}元；申报{expense.amount:.2f}元",
        }
    )
