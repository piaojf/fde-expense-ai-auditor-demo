"""Authored offline advisory fixtures: no model inference or real HTTP."""

import json
from uuid import uuid4

import httpx
from p4b_fix2_support import create, login, reference
from test_p4b_fix2_postgres import COMMON

from integrations.llm.advisory import AdvisoryDeepSeekProvider

BASE = "/api/v1.5/p4b/v1"
V2 = "/api/v1.5/p4b/v2"


def positive(request):
    return dict(
        schema_version="p4b.advisory.1", risk_level="LOW", recommendation="RECOMMEND_APPROVAL",
        issues=[], missing_evidence=[], policy_ref_ids=[reference(request, {"HOTEL": "第十五条",
                            "ALLOWANCE": "第二十五条", "ENTERTAINMENT": "第二十九条"}
                                      [request.facts["trusted"]["expense_type"]])],
        fact_refs=["candidate.purpose", "candidate.notes", "positive.business_related",
                   "positive.policy_version", "rule_results"], requires_human_review=True,
    )


def issue(request, topic, basis, clause, text, check="请财务独立核查。", facts=None):
    return dict(topic=topic, basis=basis, severity="MEDIUM",
                policy_ref_ids=[reference(request, clause)],
                fact_refs=facts or ["candidate.notes", "rule_results"],
                explanation=text, finance_check=check)


class AdvisoryWire:
    protocol_version = "p4b.advisory.1"
    model = "offline-advisory-wire"

    def __init__(self, configure=None):
        self.configure = configure
        self.requests = []
        self.bodies = []
        self.mode = None

    async def analyze(self, request):
        self.requests.append(request)
        output = positive(request)
        if self.configure:
            self.configure(output, request)

        def handler(http_request):
            body = json.loads(http_request.content)
            self.bodies.append(body)
            assert len(http_request.content) <= 32768
            assert "tools" not in body
            if self.mode == "timeout":
                raise httpx.ReadTimeout("synthetic")
            content = "{" if self.mode == "invalid_json" else json.dumps(output, ensure_ascii=False)
            return httpx.Response(200, json=dict(
                model=self.model,
                choices=[dict(finish_reason="stop",
                              message=dict(role="assistant", content=content))],
            ))

        provider = AdvisoryDeepSeekProvider(api_key="offline-synthetic-key", model=self.model,
                                            max_tokens=2048, transport=httpx.MockTransport(handler))
        return await provider.analyze(request)


def prepare(env, *, amount="680.00", notes="正常公务住宿一晚。", facts=None):
    detail = create(env[0], dict(kind="HOTEL", amount=amount, notes=notes,
                                 facts=COMMON if facts is None else facts))
    return reaudit(env[0], detail)


def reaudit(c, detail):
    login(c, "finance")
    r = c.post(BASE + f"/claims/{detail['claim_id']}/reaudit", json=dict(
        expected_revision=detail["revision"], idempotency_key=str(uuid4())))
    assert r.status_code == 202, r.text
    current = c.get("/api/v1.5/claims/" + detail["claim_id"])
    assert current.status_code == 200
    return current.json()


def manual(c, detail, **overrides):
    report = detail["advisory_analysis"]
    body = dict(report_digest=report["report_digest"], expected_revision=detail["revision"],
                outcome="CLEAR", exception_required=False, unreliable_advice_acknowledged=True,
                source_reference="SYNTHETIC-INDEPENDENT-FINANCE-REVIEW",
                basis="独立核对原件、业务、制度、重复及必要授权；无未决问题、无例外付款需求。",
                idempotency_key=str(uuid4()))
    body.update(overrides)
    return c.post(V2 + f"/claims/{detail['claim_id']}/manual-review", json=body)
