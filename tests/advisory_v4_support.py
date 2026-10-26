"""Authored current-data responses over the actual v4 HTTP/schema path."""

import json

import httpx

from integrations.llm.advisory_v4 import AdvisoryDeepSeekProviderV4
from integrations.llm.evidence_selector import selector_manifest


def output_v4(request):
    manifest = selector_manifest(request)
    relevant = [e["short_id"] for e in manifest["entries"] if e["clause_id"] == "第十五条"]
    trusted = request.facts["trusted"]
    city_fact = trusted.get("city_fact") or {}
    city = city_fact.get("value", {})
    city_entry = next((e for e in manifest["entries"]
                       if e.get("source_position", {}).get("cells")
                       == city_fact.get("original_cells")),
                      None)
    amount = request.facts["declared_amount_check"]
    decision = amount["decision"]
    return dict(
        schema_version="p4b.advisory.4", evidence_snapshot_id=manifest["evidence_snapshot_id"],
        risk_level="LOW" if decision == "PASS" else "HIGH" if decision == "FAIL" else "MEDIUM",
        recommendation="RECOMMEND_APPROVAL" if decision == "PASS"
        else "RECOMMEND_REJECTION" if decision == "FAIL" else "ESCALATE",
        summary="依据现有申报与有效制度进行金额比较，最终由财务决定。",
        city_classification=dict(city=city.get("name", "深圳"), tier=city.get("tier", "UNKNOWN"),
                                 explanation="引用制度原文中的城市分类。",
                                 policy_ref_keys=[city_entry["short_id"]] if city_entry else [
                                     e["short_id"] for e in manifest["entries"]
                                     if e.get("source_position", {}).get("table_index") == 4
                                 ][:1]),
        issues=[], missing_evidence=[], policy_ref_keys=relevant,
        fact_refs=["candidate.amount", "account.grade", "declared_amount_check"],
        requires_human_review=True,
    )


class AdvisoryWireV4:
    protocol_version = "p4b.advisory.4"
    model = "mock-authored-advisory-v4"

    def __init__(self):
        self.requests, self.bodies = [], []
        self.configure = None

    async def analyze(self, request):
        self.requests.append(request)

        def handler(wire):
            self.bodies.append(json.loads(wire.content))
            data = output_v4(request)
            if self.configure:
                self.configure(data, request)
            return httpx.Response(200, json=dict(model=self.model, choices=[dict(
                finish_reason="stop", message=dict(role="assistant", content=json.dumps(
                    data, ensure_ascii=False)))]))

        try:
            return await AdvisoryDeepSeekProviderV4(
                api_key="unit-test-only", model=self.model, max_tokens=4096,
                transport=httpx.MockTransport(handler),
            ).analyze(request)
        except Exception as exc:
            request._provider_evidence["synthetic_failure"] = str(exc)
            raise
