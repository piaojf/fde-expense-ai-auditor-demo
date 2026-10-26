"""Versioned structured declaration; frozen v1 DTOs remain unchanged."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from domain.schemas import DTO, ClaimDetail, IdempotencyKey, LLMAnalysis, Money, OperationResponse

CITIES = (
    "深圳",
    "广州",
    "北京",
    "上海",
    "杭州",
    "南京",
    "苏州",
    "成都",
    "重庆",
    "武汉",
    "西安",
    "天津",
    "长沙",
    "厦门",
    "福州",
    "郑州",
    "济南",
    "青岛",
    "合肥",
    "宁波",
    "东莞",
    "佛山",
    "珠海",
    "昆明",
    "南宁",
    "贵阳",
    "海口",
    "沈阳",
    "大连",
    "长春",
    "哈尔滨",
    "石家庄",
    "太原",
    "南昌",
    "南通",
    "无锡",
    "温州",
    "兰州",
    "银川",
    "西宁",
    "乌鲁木齐",
    "拉萨",
    "呼和浩特",
)
Text = Annotated[str, Field(min_length=1, max_length=200)]


class SubmissionFields(DTO):
    applicant_name: Text
    department: Annotated[str, Field(min_length=1, max_length=128)]
    position: Annotated[str, Field(min_length=1, max_length=128)]
    category: Annotated[str, Field(pattern="^(HOTEL|TRANSPORT|MEAL|OTHER)$")]
    amount: Money = Field(gt=0)
    incurred_on: date
    purpose: Annotated[str, Field(min_length=1, max_length=2000)]
    city: str
    trip_start: date
    trip_end: date
    nights: int | None = Field(default=None, strict=True, ge=1, le=366)
    seller: Text
    notes: Annotated[str, Field(max_length=2000)] = ""

    @field_validator(
        "applicant_name", "department", "position", "purpose", "seller", "notes", mode="before"
    )
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("city")
    @classmethod
    def standard_city(cls, value):
        if value not in CITIES:
            raise ValueError("Select a standardized city")
        return value

    @model_validator(mode="after")
    def applicable_fields(self):
        today = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
        if self.incurred_on > today:
            raise ValueError("Expense date cannot be in the future")
        if self.trip_end < self.trip_start or (self.trip_end - self.trip_start).days >= 366:
            raise ValueError("Trip must be ordered and at most 366 days")
        if (self.category == "HOTEL") != (self.nights is not None):
            raise ValueError("Nights are required only for HOTEL")
        return self


class CreateClaimRequest(SubmissionFields):
    idempotency_key: IdempotencyKey


class SubmissionResponse(DTO):
    schema_version: str = "1.1.0"
    claim: ClaimDetail
    submission: SubmissionFields
    trip_days: int
    operation: OperationResponse | None
    human_review_submitted: bool
    llm_analysis: LLMAnalysis | None
    erp_effects: list[dict]
