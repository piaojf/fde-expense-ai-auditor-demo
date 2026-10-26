"""P4A v1 contracts; no changes to frozen submission or rule DTOs."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from domain.enterprise import PortalInvoice
from domain.schemas import DTO, IdempotencyKey, Money


class HotelFields(DTO):
    applicant_name: str = Field(min_length=1, max_length=200)
    department: str = Field(min_length=1, max_length=128)
    position: str = Field(min_length=1, max_length=128)
    category: Literal["HOTEL"] = "HOTEL"
    amount: Money = Field(gt=0)
    incurred_on: date
    purpose: str = Field(min_length=1, max_length=2000)
    city: str = Field(min_length=1, max_length=128)
    trip_start: date
    trip_end: date
    nights: int = Field(strict=True, ge=1, le=365)
    seller: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def dates(self):
        if (self.trip_end - self.trip_start).days != self.nights:
            raise ValueError("退房日期减入住日期必须等于住宿晚数")
        if self.incurred_on != self.trip_start:
            raise ValueError("费用日期必须为入住日期；跨制度日期住宿须人工核实")
        return self


class HotelSubmission(DTO):
    fields: HotelFields
    stay_id: str = Field(min_length=1, max_length=128)
    invoice: PortalInvoice | None = None
    idempotency_key: IdempotencyKey


class TrustedFact(DTO):
    kind: Literal["EMPLOYEE", "CITY", "STAY"]
    key: str = Field(min_length=1, max_length=128)
    effective_from: date
    effective_to: date
    value: dict
    source: str = Field(min_length=1, max_length=2000)
    version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def interval(self):
        if self.effective_to <= self.effective_from:
            raise ValueError("有效区间必须为左闭右开且非空")
        if self.kind == "EMPLOYEE" and self.value.get("grade") not in ("A", "B", "C", "D"):
            raise ValueError("职级必须为 A/B/C/D")
        if self.kind == "CITY" and (
            self.value.get("tier") not in ("I", "II", "III", "IV")
            or self.value.get("level") not in ("MUNICIPALITY", "PREFECTURE", "COUNTY", "TOWN")
            or not self.value.get("name")
        ):
            raise ValueError("城市必须有明确行政层级、名称及等级")
        if self.kind == "STAY" and not all(
            self.value.get(k)
            for k in (
                "employee_id",
                "city_key",
                "address",
                "check_in",
                "check_out",
                "evidence_reference",
            )
        ):
            raise ValueError("住宿核验必须关联员工、城市、实际地址、日期和原始证据")
        if self.kind == "EMPLOYEE" and set(self.value) != {"grade"}:
            raise ValueError("员工主数据仅接受职级字段")
        if self.kind == "CITY":
            if set(self.value) - {"name", "tier", "level", "aliases"}:
                raise ValueError("城市主数据包含未知字段")
            aliases = self.value.get("aliases", [])
            if (
                not isinstance(self.value["name"], str)
                or not self.value["name"].strip()
                or len(self.value["name"]) > 128
                or not isinstance(aliases, list)
                or len(aliases) > 20
                or any(not isinstance(v, str) or not v.strip() or len(v) > 128 for v in aliases)
            ):
                raise ValueError("城市名称及明确别名必须为有界文本")
        if self.kind == "STAY":
            required = {
                "employee_id",
                "city_key",
                "address",
                "check_in",
                "check_out",
                "evidence_reference",
            }
            if set(self.value) != required or any(
                not isinstance(v, str) or not v.strip() or len(v) > 2000
                for v in self.value.values()
            ):
                raise ValueError("住宿证据字段须为完整有界文本")
            start, end = (
                date.fromisoformat(self.value["check_in"]),
                date.fromisoformat(self.value["check_out"]),
            )
            if (
                start.isoformat() != self.value["check_in"]
                or end.isoformat() != self.value["check_out"]
                or end <= start
            ):
                raise ValueError("住宿日期须为有效 ISO 日期且退房晚于入住")
        return self


class MatrixConfirm(DTO):
    expected_revision: int = Field(ge=0)
    extraction_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=2000)


class MatrixPreview(DTO):
    predecessor_id: str | None = None
    predecessor_ids: list[str] = Field(default_factory=list, max_length=100)


class MatrixPublish(MatrixPreview):
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    idempotency_key: IdempotencyKey
    reason: str = Field(min_length=1, max_length=2000)


class MatrixLifecyclePreview(DTO):
    kind: Literal["RETIRE", "ROLLBACK"]
    effective_on: date
    restore_id: str | None = None


class MatrixLifecycle(MatrixLifecyclePreview):
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    idempotency_key: IdempotencyKey
    reason: str = Field(min_length=1, max_length=2000)


class HotelResubmission(HotelSubmission):
    expected_revision: int = Field(ge=0)
