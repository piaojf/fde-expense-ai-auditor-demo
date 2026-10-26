"""Published city provenance is stored beside value, not inside it."""

from copy import deepcopy

import pytest
from advisory_v4_support import output_v4
from test_p4b_p2 import hotel_request

from domain.advisory_v4 import AdvisoryOutputV4
from integrations.llm.advisory_v4 import validate_advisory_v4
from integrations.llm.evidence_selector import prepare_selector


def published_city_request():
    request = hotel_request()
    row = next(e for e in request.evidence if e.clause_id == "TABLE-4-ROW-1")
    request.facts["trusted"]["city_fact"] = dict(
        value=dict(name="深圳", tier="I"),
        original_cells=deepcopy(row.source_position["cells"]),
        policy_version_id=request.policy_version_id,
        file_sha256=row.document_sha256,
    )
    request.facts["candidate"].update(amount="680.00", nights=1)
    request.facts["declared_amount_check"] = dict(decision="PASS")
    prepare_selector(request)
    return request


def test_published_city_row_is_accepted_without_rewriting_output():
    request = published_city_request()
    data = output_v4(request)
    before = deepcopy(data)
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert errors == []
    assert data == before


@pytest.mark.parametrize("fault", ["wrong_row", "wrong_tier", "wrong_city", "unknown_key"])
def test_city_provenance_fix_does_not_accept_invalid_classification(fault):
    request = published_city_request()
    data = output_v4(request)
    city = data["city_classification"]
    if fault == "wrong_row":
        city["policy_ref_keys"] = data["policy_ref_keys"]
    elif fault == "wrong_tier":
        city["tier"] = "IV"
    elif fault == "wrong_city":
        city["city"] = "合成错误城市"
    else:
        city["policy_ref_keys"] = ["E999"]
    if fault == "unknown_key":
        with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_KEY"):
            validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    else:
        _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
        expected = ("CITY_CLASSIFICATION_CITATION_INVALID" if fault == "wrong_row"
                    else "CITY_CLASSIFICATION_CONFLICT")
        assert any(error["code"] == expected for error in errors)
