"""Shared Decimal full-day allowance arithmetic; callers verify the facts and source."""

from decimal import Decimal


def full_day_allowance(
    base,
    days,
    *,
    breakfasts=0,
    lunches=0,
    dinners=0,
    breakfast_rate=Decimal(0),
    lunch_rate=Decimal(0),
    dinner_rate=Decimal(0),
):
    if type(days) is not int or days < 1:
        raise ValueError("Verified full days required")
    counts = (breakfasts, lunches, dinners)
    rates = tuple(Decimal(r) for r in (breakfast_rate, lunch_rate, dinner_rate))
    if any(type(c) is not int or not 0 <= c <= days for c in counts):
        raise ValueError("Meal counts outside verified trip")
    if any(not r.is_finite() or not 0 <= r <= 1 for r in rates) or sum(rates) > 1:
        raise ValueError("Invalid published meal rates")
    units = Decimal(days) - sum((Decimal(c) * r for c, r in zip(counts, rates)), Decimal(0))
    return (Decimal(base) * max(Decimal(0), units)).quantize(Decimal("0.01"))
