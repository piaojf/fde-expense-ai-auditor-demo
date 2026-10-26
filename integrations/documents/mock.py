"""Parse explicit mock invoice JSON; no OCR or real invoice authenticity claim."""

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from domain.schemas import DTO, Expense


class TripContext(DTO):
    approved_from: date
    approved_to: date
    stay_from: date
    stay_to: date
    employee_explanation: str
    cancellation_document_id: str

    @model_validator(mode="after")
    def valid_dates(self):
        if self.approved_to < self.approved_from or self.stay_to <= self.stay_from:
            raise ValueError("Invalid approved trip or hotel stay dates")
        return self


class MockInvoice(DTO):
    document_id: str
    claim_id: str
    kind: Literal["INVOICE", "FLIGHT_CANCELLATION"] = "INVOICE"
    expense: Expense | None = None
    trip_context: TripContext | None = None
    cancelled_on: date | None = None
    flight_reference: str | None = None


class MockDocumentParser:
    def __init__(self, fixture_path: Path | None = None):
        self.path = fixture_path or Path(__file__).parents[2] / "apps/api/mock_data/documents.json"

    def parse(self, document_id: str) -> MockInvoice | None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        matches = [d for d in data if d["document_id"] == document_id]
        return MockInvoice.model_validate(matches[0]) if len(matches) == 1 else None
