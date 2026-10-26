"""Authored typed judgments; all HTTP responses are synthetic MockTransport."""

import json

import httpx
from advisory_support import positive
from p4b_fix2_support import reference

from integrations.llm.advisory_v2 import SPECS, AdvisoryDeepSeekProviderV2


def finding_v2(request, code="GENERAL_FINANCE_CHECK", **overrides):
    classification, _, _, clauses, paths, _ = SPECS[code]
    clause = next(e.clause_id for e in request.evidence if e.clause_id in clauses)
    result = dict(code=code, classification=classification,
                  resolution="NOT_APPLICABLE" if classification == "INFO" else "UNRESOLVED",
                  policy_ref_ids=[reference(request, clause)],
                  fact_refs=sorted(paths), rule_refs=[])
    if code in {"AMOUNT_WITHIN_LIMIT", "RULE_FAILURE"}:
        result["rule_refs"] = [request.rule_results[0]["rule_id"]]
    if code == "BUSINESS_PURPOSE_UNRESOLVED":
        result.update(source_quote=request.facts["candidate"]["notes"],
                      question="请核查该业务目的是否有独立材料支持？")
    result.update(overrides)
    return result


def output_v2(request, codes=("GENERAL_FINANCE_CHECK",)):
    result = positive(request)
    result.update(schema_version="p4b.advisory.2",
                  issues=[finding_v2(request, code) for code in codes])
    missing = [k for k in request.facts["trusted"]["positive_required"]
               if request.facts["positive"][k]["status"] != "VERIFIED"
               or request.facts["positive"][k]["value"] is False]
    result["missing_evidence"] = missing
    if missing:
        result.update(risk_level="UNDETERMINED", recommendation="REQUEST_EVIDENCE")
    return result


class AdvisoryWireV2:
    protocol_version = "p4b.advisory.2"
    model = "offline-advisory-v2-wire"

    def __init__(self, configure=None):
        self.configure, self.mode = configure, None
        self.requests, self.bodies = [], []

    async def analyze(self, request):
        self.requests.append(request)
        data = output_v2(request)
        if self.configure:
            self.configure(data, request)

        def handler(wire):
            self.bodies.append(json.loads(wire.content))
            assert len(wire.content) <= 32768
            content = json.dumps(data, ensure_ascii=False)
            if self.mode == "invalid_json":
                content = "{"
            return httpx.Response(200, json=dict(model=self.model, choices=[dict(
                finish_reason="stop", message=dict(role="assistant", content=content))]))

        return await AdvisoryDeepSeekProviderV2(
            api_key="offline-only-key", model=self.model, max_tokens=2048,
            transport=httpx.MockTransport(handler),
        ).analyze(request)
