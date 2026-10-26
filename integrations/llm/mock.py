from domain.schemas import LLMAnalysis, RiskLevel
from integrations.faults import FailureMode, FaultInjector
from integrations.llm.base import AnalysisRequest


class MockLLMProvider:
    """Structured offline analysis of rule facts; makes no approval decision."""

    def __init__(self, failure_mode: FailureMode = FailureMode.NONE):
        self.faults = FaultInjector(failure_mode)

    async def analyze(self, request: AnalysisRequest) -> LLMAnalysis:
        self.faults.before_call()
        if self.faults.mode == FailureMode.INVALID_OUTPUT:
            return {"summary": "missing required structured fields"}
        if self.faults.mode == FailureMode.INVALID_RESPONSE:
            return "{not-json"
        anomalies = []
        if not request.evidence:
            anomalies.append("No authoritative policy evidence")
        if request.facts.get("rules_passed") != "true":
            anomalies.append("Deterministic rules did not pass")
        if request.facts.get("hard_gates_passed") != "true":
            anomalies.append("Hard gates did not pass")
        if request.facts.get("business_ambiguity") == "true":
            anomalies.append(
                "DECLARATION_REQUIRES_VERIFICATION" if request.facts.get("declaration") == "true"
                else "STAY_OUTSIDE_APPROVED_TRIP_DATES"
            )
            if request.facts.get("flight_cancellation") == "true":
                anomalies.append("FLIGHT_CANCELLATION_EXCEPTION")
        summary = f"{request.facts.get('city')}住宿费用{request.facts.get('amount')}元；"
        if request.facts.get("declaration") == "true":
            summary = f"结构化申报金额{request.facts.get('amount')}元；未核验发票。"
            if request.facts.get("business_ambiguity") == "true":
                summary += "特殊说明或行程日期存在待核实事项，需要人工复核。"
            return LLMAnalysis(
                summary=summary, anomalies=anomalies,
                suggested_risk=RiskLevel.MEDIUM if anomalies else RiskLevel.LOW,
                confidence=0.0 if anomalies else 1.0, provider="mock", model="m1-structured-mock",
            )
        return LLMAnalysis(
            summary=summary
            + f"确定性规则限额{request.facts.get('limit')}元；"
            f"制度版本{request.evidence[0].version if request.evidence else '未找到'}。"
            + (
                (
                    "金额符合住宿限额；"
                    if request.facts.get("rules_passed") == "true"
                    else "金额不符合住宿规则；"
                )
                + "住宿超出原出差期间，航班取消说明属于待核实业务歧义，"
                "现有制度未提供自动批准或自动拒绝该日期例外的规则。"
                if request.facts.get("business_ambiguity") == "true"
                else ""
            ),
            anomalies=anomalies,
            suggested_risk=RiskLevel.MEDIUM if anomalies else RiskLevel.LOW,
            confidence=0.0 if anomalies else 1.0,
            provider="mock",
            model="m1-structured-mock",
        )
