"""Bounded report projection, only called after semantic validation succeeds."""


def compose_summary(semantic, contract, rules, status, action):
    checks = semantic.checks.model_dump()
    unresolved = [topic for topic, state in checks.items() if state != "CLEAR"]
    checked = (
        "模型八项检查均标记CLEAR" if not unresolved else "模型未决主题：" + "、".join(unresolved)
    )
    facts = [key for key in contract.required if contract.entries[key].status == "VERIFIED"]
    rule_text = "；".join(
        f"{r['rule_id']}={r['outcome']}（实际{r['actual']}，限额{r['limit']}）" for r in rules
    )
    text = (
        f"服务端摘要：{checked}；保留{len(semantic.findings)}项模型发现；"
        f"本次核验范围内必要事实已核实{len(facts)}/{len(contract.required)}项；"
        f"规则结果：{rule_text}；整单状态{status}，建议{action}。"
        "检查结果限于当前证据与核验范围；财务仍须单独批准。"
    )
    # Separate domains. A policy chunk never establishes a verified fact or rule computation.
    bindings = {
        "policy_evidence": list(semantic.evidence_refs),
        "verified_facts": [
            {"path": "positive." + key, "evidence_id": contract.entries[key].evidence_id}
            for key in facts
        ],
        "rule_results": [{"index": i, **r} for i, r in enumerate(rules)],
        "semantic_checks": checks,
        "semantic_findings": [f"F{i + 1}" for i in range(len(semantic.findings))],
    }
    return text, bindings
