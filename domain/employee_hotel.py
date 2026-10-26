"""Employee-owned hotel declarations; trusted facts remain server-owned."""

from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import Field, PrivateAttr, model_validator

from domain.enterprise import PortalInvoice
from domain.hotel_matrix import HotelResubmission, HotelSubmission
from domain.schemas import DTO, IdempotencyKey, Money


class EmployeeHotel(DTO):
    city: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=2000)
    check_in: date
    check_out: date
    amount: Money = Field(gt=0)
    purpose: str = Field(min_length=1, max_length=2000)
    seller: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
    invoice: PortalInvoice | None = None
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def validate_stay(self):
        if not 1 <= (self.check_out - self.check_in).days <= 365:
            raise ValueError("退房日期须晚于入住日期，住宿不得超过 365 晚")
        for key in ("city", "address", "purpose", "seller"):
            if not getattr(self, key).strip():
                raise ValueError("城市、实际住宿地址、用途和酒店名称不能为空")
        return self


class EmployeeHotelResubmission(EmployeeHotel):
    expected_revision: int = Field(ge=0)


class DeclarationSubmission(HotelSubmission):
    _authority_check: Callable | None = PrivateAttr(default=None)
    stay_id: Literal[""] = ""
    address: str
    submission_schema: Literal["p4a.employee.2"] = "p4a.employee.2"


class DeclarationResubmission(HotelResubmission):
    _authority_check: Callable | None = PrivateAttr(default=None)
    stay_id: Literal[""] = ""
    address: str
    submission_schema: Literal["p4a.employee.2"] = "p4a.employee.2"


class VerifyEmployeeStay(DTO):
    expected_revision: int = Field(ge=0)
    confirmed: Literal[True]
    source_reference: str = Field(min_length=1, max_length=2000)
    basis: str = Field(min_length=1, max_length=2000)
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def evidence(self):
        if not self.source_reference.strip() or not self.basis.strip():
            raise ValueError("请填写住宿证明来源及核实依据")
        return self
