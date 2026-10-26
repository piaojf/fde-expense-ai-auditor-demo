"""Short evidence selection followed by unchanged v2 business attribution."""

import json

from domain.advisory_v2 import AdvisoryOutputV2
from domain.advisory_v3 import AdvisoryOutputV3
from integrations.llm.advisory_v2 import (
    AdvisoryDeepSeekProviderV2,
    eligible_finding_specs,
    validate_advisory_v2,
)
from integrations.llm.compliance_v2 import AttributionError, _lean_schema, compact_json
from integrations.llm.deepseek import DeepSeekOutputError
from integrations.llm.evidence_selector import prepare_selector, resolve_keys, selector_manifest

PROTOCOL = "p4b.advisory.3"


class AdvisoryAnalysisV3(AdvisoryOutputV3):
    provider: str
    model: str


class AdvisoryDeepSeekProviderV3(AdvisoryDeepSeekProviderV2):
    protocol_version = PROTOCOL

    def _parse_output(self, normalized):
        output = AdvisoryOutputV3.model_validate_json(normalized, strict=True)
        return AdvisoryAnalysisV3(**output.model_dump(), provider="deepseek", model=self.model)

    def _body(self, request):
        # Reuse v2 instructions exactly, replacing only the explicit wire contract/catalog/schema.
        body = super()._body(request)
        manifest = prepare_selector(request)
        data = json.loads(body["messages"][1]["content"])
        keys = [e["short_id"] for e in manifest["entries"]]
        scope = manifest["entries"][0]["applicable_scope"]
        data.update(schema_version="p4b.input.3",
                    evidence_snapshot_id=manifest["evidence_snapshot_id"],
                    evidence_scope={k: v for k, v in scope.items()
                                    if k != "positive_scope"},
                    evidence=[dict(short_id=e["short_id"], clause_id=e["clause_id"],
                                   source_text=e["source_text"],
                                   applicable_scope_ref="evidence_scope")
                              for e in manifest["entries"]])
        schema = _lean_schema(AdvisoryOutputV3.model_json_schema())
        specs = eligible_finding_specs(request)
        schema["$defs"]["AdvisoryFindingV3"]["properties"]["code"]["enum"] = list(specs)
        for props in (schema["properties"], schema["$defs"]["AdvisoryFindingV3"]["properties"]):
            props["policy_ref_keys"]["items"] = {"type": "string", "enum": keys}
        schema["properties"]["evidence_snapshot_id"] = {
            "type": "string", "const": manifest["evidence_snapshot_id"]}
        guide = {code: dict(classification=spec[0], policy_ref_keys=[
            e["short_id"] for e in manifest["entries"] if e["clause_id"] in spec[3]],
            fact_refs=sorted(spec[4])) for code, spec in specs.items()}
        instructions = body["messages"][0]["content"].split("目录：", 1)[0]
        instructions = instructions.replace("p4b.advisory.2", PROTOCOL).replace(
            "policy_ref_ids", "policy_ref_keys")
        body["messages"] = [dict(role="system", content=instructions + (
            "短编号仅选择本请求evidence所列E编号；不得输出条款名称或原始ID代替编号。"
            "原样返回evidence_snapshot_id，不得跨请求复用。目录列出可用候选，不是事实或授权；"
            "即使编号存在，仍必须支持对应发现。条款原文完整保留，最终审批始终属于财务。目录："
        ) + compact_json(guide) + "；schema：" + compact_json(schema)),
            dict(role="user", content=compact_json(data))]
        size = len(compact_json(body).encode("utf-8"))
        request._provider_evidence.update(input_schema="p4b.input.3", output_schema=PROTOCOL,
                                         evidence_snapshot_id=manifest["evidence_snapshot_id"],
                                         request_bytes=size)
        if size > self.MAX_INPUT_BYTES:
            raise DeepSeekOutputError("INPUT_TOO_LARGE")
        return body


def validate_advisory_v3(output, request):
    output = AdvisoryOutputV3.model_validate(output.model_dump(), strict=True)
    manifest = selector_manifest(request)
    if output.evidence_snapshot_id != manifest["evidence_snapshot_id"]:
        raise AttributionError("EVIDENCE_SNAPSHOT_MISMATCH", "evidence_snapshot_id")
    data = output.model_dump(mode="json", exclude={"evidence_snapshot_id"})
    data["schema_version"] = "p4b.advisory.2"
    data["policy_ref_ids"] = resolve_keys(data.pop("policy_ref_keys"), manifest, "report")
    for n, issue in enumerate(data["issues"]):
        issue["policy_ref_ids"] = resolve_keys(
            issue.pop("policy_ref_keys"), manifest, f"issues.{n}")
    # No output edits, clause-name matching, search, missing-citation repair or discarded failures.
    valid, errors = validate_advisory_v2(
        AdvisoryOutputV2.model_validate(data, strict=True), request)
    # Global references need their own bounded attribution; existence alone is insufficient.
    allowed = {ref for finding in valid
               if set(finding["fact_refs"]) <= set(data["fact_refs"])
               for ref in finding["policy_ref_ids"]}
    kind = request.facts["trusted"]["expense_type"]
    amount_clauses = {"HOTEL": {"第十五条", "TABLE-5-ROW-4"},
                      "ALLOWANCE": {"第二十五条", "第二十六条"},
                      "ENTERTAINMENT": {"第二十九条"}}[kind]
    if "rule_results" in data["fact_refs"]:
        allowed.update(e.chunk_id for e in request.evidence if e.clause_id in amount_clauses)
    if "positive.policy_version" in data["fact_refs"]:
        allowed.update(e.chunk_id for e in request.evidence
                       if e.clause_id in {"第五十三条", "第五十五条"})
    if not set(data["policy_ref_ids"]) <= allowed:
        errors.append(dict(code="CITATION_ATTRIBUTION_FAILURE", field="report"))
    return valid, errors
