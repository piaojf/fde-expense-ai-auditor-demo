"""Independent enterprise travel contracts; frozen expense DTOs are not extended."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from domain.schemas import DTO, IdempotencyKey, Money

ExpenseType = Literal["HOTEL", "TRANSPORT", "ALLOWANCE"]


class TravelDetails(DTO):
    expense_type: ExpenseType
    transport_mode: str = Field(default="", max_length=50)
    seat: str = Field(default="", max_length=50)
    transport_count: int = Field(default=1, strict=True, ge=1, le=366)
    allowance_days: int | None = Field(default=None, strict=True, ge=1, le=366)
    breakfasts: int = Field(default=0, strict=True, ge=0, le=366)
    lunches: int = Field(default=0, strict=True, ge=0, le=366)
    dinners: int = Field(default=0, strict=True, ge=0, le=366)

    @model_validator(mode="after")
    def relevant(self):
        if self.expense_type != "TRANSPORT" and (
            self.transport_mode or self.seat or self.transport_count != 1
        ):
            raise ValueError("交通信息只适用于交通费")
        if self.expense_type != "ALLOWANCE" and (
            self.allowance_days is not None or self.breakfasts or self.lunches or self.dinners
        ):
            raise ValueError("补助信息只适用于差旅补助")
        if (
            self.allowance_days
            and max(self.breakfasts, self.lunches, self.dinners) > self.allowance_days
        ):
            raise ValueError("供餐次数不得超过申报天数")
        return self


class TravelProfile(DTO):
    grade: Literal["A", "B", "C", "D", "UNKNOWN"]
    effective_on: date
    reason: str = Field(min_length=1, max_length=1000)


class TravelRule(DTO):
    expense_type: ExpenseType
    cities: list[str] = Field(min_length=1, max_length=100)
    departments: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)
    positions: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)
    grades: list[Literal["*", "A", "B", "C", "D"]] = Field(default_factory=lambda: ["*"])
    unit: Literal["NIGHT", "DAY", "TRIP"]
    limit: Money | None = Field(default=None, gt=0)
    transport_mode: str = Field(default="", max_length=50)
    seats: list[str] = Field(default_factory=list, max_length=20)
    breakfast_rate: Decimal = Field(
        default=Decimal("0"), ge=0, le=1, max_digits=3, decimal_places=2
    )
    lunch_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=3, decimal_places=2)
    dinner_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=3, decimal_places=2)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    review_required: bool = False

    @field_validator("cities", "departments", "positions", "seats")
    @classmethod
    def visible_list(cls, value):
        if any(not v.strip() or len(v) > 128 or any(ord(c) < 32 for c in v) for v in value):
            raise ValueError("适用范围须为可见文字")
        values = [v.strip() for v in value]
        if len(set(values)) != len(values) or ("*" in values and len(values) > 1):
            raise ValueError("全部不能和具体范围同时选择")
        return values

    @model_validator(mode="after")
    def units(self):
        if not self.grades or ("*" in self.grades and len(self.grades) > 1):
            raise ValueError("请选择职级范围")
        if self.expense_type == "HOTEL" and (self.unit != "NIGHT" or self.limit is None):
            raise ValueError("住宿标准需要每晚限额")
        if self.expense_type == "ALLOWANCE" and (self.unit != "DAY" or self.limit is None):
            raise ValueError("差旅补助需要每日标准")
        if self.expense_type == "TRANSPORT" and (
            self.unit == "NIGHT" or not self.transport_mode.strip() or not self.seats
        ):
            raise ValueError("交通标准需要工具和允许席别，单位为每次或每天")
        if self.expense_type != "TRANSPORT" and (self.transport_mode or self.seats):
            raise ValueError("交通条件仅适用交通费")
        total = self.breakfast_rate + self.lunch_rate + self.dinner_rate
        if total > 1 or (self.expense_type != "ALLOWANCE" and total):
            raise ValueError("供餐扣减比例仅用于补助且合计不超过100%")
        return self


class PublishTravelPolicy(DTO):
    rules: list[TravelRule] = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: IdempotencyKey


class RetireTravelPolicy(DTO):
    expected_revision: int = Field(ge=1)
    effective_on: date
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: IdempotencyKey
