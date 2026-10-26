"""Validate a stored declaration, never fabricate invoice/OCR facts."""

from datetime import timedelta
from hashlib import sha256

from domain.schemas import Expense
from domain.submission import SubmissionFields


def parse_submission(state, raw, *, expected_legal_entity="DEMO-CN"):
    fields = SubmissionFields.model_validate(raw)
    expense = Expense(
        category=fields.category,
        city=fields.city,
        amount=fields.amount,
        incurred_on=fields.incurred_on,
        nights=fields.nights or 1,
    )
    matches = (
        expense == state.expense
        and len(state.documents) == 1
        and state.documents[0].content_type == "application/json"
        and state.documents[0].sha256 == sha256(fields.model_dump_json().encode()).hexdigest()
        and state.employee.name == fields.applicant_name
        and state.employee.department == fields.department
        and state.employee.legal_entity == expected_legal_entity
    )
    outside = not fields.trip_start <= fields.incurred_on <= fields.trip_end or (
        fields.nights is not None
        and fields.incurred_on + timedelta(days=fields.nights) > fields.trip_end
    )
    ambiguous = outside or bool(fields.notes)
    return {
        "documents": [
            d.model_copy(update={"parse_status": "PARSED" if matches else "FAILED"})
            for d in state.documents
        ],
        "audit_context": {
            "document_facts_match": matches,
            "document_parser": "structured-declaration; no invoice verification or OCR",
            "submitted_facts": fields.model_dump(mode="json"),
            "trip_context": {
                "approved_from": fields.trip_start.isoformat(),
                "approved_to": fields.trip_end.isoformat(),
                "stay_from": fields.incurred_on.isoformat(),
                "stay_to": (fields.incurred_on + timedelta(days=fields.nights or 0)).isoformat(),
                "employee_explanation": fields.notes,
            },
            "business_analysis": {
                "ambiguity": ambiguous,
                "flight_cancellation": False,
                "risk_factors": ["DECLARATION_REQUIRES_VERIFICATION"] if ambiguous else [],
                "automatic_exception_rule": False,
            },
        },
    }
