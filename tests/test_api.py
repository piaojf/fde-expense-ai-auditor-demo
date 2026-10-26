import json
from pathlib import Path
from uuid import UUID

import pytest

CLAIM = "CLM-2026-001"
RUN = "00000000-0000-0000-0000-000000000001"


def test_health_exact_contract(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "fde-expense-ai-auditor",
        "version": "0.1.0",
        "milestone": "M0",
        "data_source": "mock",
        "database": "not_checked",
    }
    UUID(response.headers["X-Request-ID"])


def test_list_claims_pagination_and_decimal(client):
    body = client.get("/claims?limit=1&offset=5").json()
    assert body["total"] == 7
    assert len(body["items"]) == 1
    assert body["items"][0]["claim_id"] == "CLM-2026-002"
    assert body["items"][0]["expense"]["amount"] == "500.00"
    assert body["items"][0]["decision_result"] is None
    assert "documents" not in body["items"][0]
    assert client.get("/claims?offset=99").json()["items"] == []


def test_list_claims_filters(client):
    assert client.get("/claims?status=PENDING_HUMAN").json()["total"] == 0
    assert client.get("/claims?status=NEW").json()["total"] == 7


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=INVALID"])
def test_query_validation(client, query):
    response = client.get("/claims?" + query)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_claim_detail(client):
    body = client.get(f"/claims/{CLAIM}").json()
    assert body["claim_id"] == CLAIM
    assert body["expense"]["amount"] == "800.00"
    assert body["documents"][0]["parse_status"] == "UNPARSED"
    assert body["erp_status"] == "NOT_STARTED"
    assert body["active_run_id"] is None
    assert body["data_source"] == "mock"


@pytest.mark.parametrize("path", ["/claims/missing", "/claims/missing/audit-log"])
def test_missing_claim(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CLAIM_NOT_FOUND"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_empty_audit_log(client):
    body = client.get(f"/claims/{CLAIM}/audit-log?limit=1&offset=2").json()
    assert body == {
        "claim_id": CLAIM,
        "items": [],
        "total": 0,
        "limit": 1,
        "offset": 2,
        "data_source": "mock",
    }


def test_dashboard_is_not_fake_audit(client):
    body = client.get("/dashboard/metrics").json()
    assert body["total_claims"] == 7
    assert body["by_status"]["NEW"] == 7
    assert sum(body["by_status"].values()) == 7
    assert body["by_decision"] == {"APPROVE": 0, "REJECT": 0, "HUMAN_REVIEW": 0}
    assert body["pending_human"] == body["pending_erp"] == 0


WRITE_CASES = [
    ("audit", {"idempotency_key": "audit-001", "mode": "LIVE"}),
    (
        "human-review",
        {
            "run_id": RUN,
            "expected_revision": 1,
            "decision": "APPROVE",
            "reviewer_id": "FIN-001",
            "comment": "verified",
            "idempotency_key": "review-001",
        },
    ),
    ("resume", {"run_id": RUN, "expected_revision": 2, "idempotency_key": "resume-001"}),
]


@pytest.mark.parametrize(("operation", "payload"), WRITE_CASES[1:])
def test_nonexistent_run_writes_are_rejected_and_immutable(client, operation, payload):
    before = client.get(f"/claims/{CLAIM}").json()
    response = client.post(f"/claims/{CLAIM}/{operation}", json=payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert client.get(f"/claims/{CLAIM}").json() == before


@pytest.mark.parametrize(("operation", "payload"), WRITE_CASES)
def test_write_missing_claim(client, operation, payload):
    response = client.post(f"/claims/missing/{operation}", json=payload)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"idempotency_key": ""},
        {
            "idempotency_key": "audit",
            "sneaky_decision": "APPROVE",
        },
    ],
)
def test_write_body_validation(client, payload):
    response = client.post(f"/claims/{CLAIM}/audit", json=payload)
    assert response.status_code == 422
    assert all("input" not in issue for issue in response.json()["error"]["details"]["issues"])


def test_openapi_contract_frozen(client):
    schema = client.get("/openapi.json").json()
    expected = json.loads(Path("docs/openapi.json").read_text(encoding="utf-8"))
    assert schema == expected
    assert len(schema["paths"]) == 8
    for operation in ("audit", "human-review", "resume"):
        responses = schema["paths"][f"/claims/{{claim_id}}/{operation}"]["post"]["responses"]
        assert "501" in responses
    assert "DecisionResult" in schema["components"]["schemas"]


def test_cors_for_workbuddy(client):
    response = client.options(
        "/claims",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    response = client.get("/health", headers={"Origin": "https://unconfigured.example"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("get", "/missing", 404),
        ("delete", "/health", 405),
    ],
)
def test_http_error_envelope(client, method, path, expected):
    response = getattr(client, method)(path)
    assert response.status_code == expected
    assert response.json()["error"]["code"] == "HTTP_ERROR"
