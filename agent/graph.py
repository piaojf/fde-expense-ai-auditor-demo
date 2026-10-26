"""M0 regression graph and M1 injected Happy Path graph."""

from langgraph.graph import END, START, StateGraph

from agent.state import ClaimState
from apps.api.repository import MockClaimRepository

PLANNED_NODES = (
    "load_claim",
    "fetch_erp_context",
    "parse_documents",
    "rule_check",
    "retrieve_policy",
    "llm_analysis",
    "risk_assessment",
    "decision_router",
    "auto_approve",
    "human_review",
    "reject",
    "write_erp",
    "audit_log",
)


def build_m0_graph(repository: MockClaimRepository | None = None):
    repo = repository or MockClaimRepository()

    def load_claim(state: ClaimState):
        claim = repo.get(state.claim_id)
        if claim is None:
            raise ValueError("Claim not found")
        return {
            **state.model_dump(),
            "employee": claim.employee,
            "expense": claim.expense,
            "documents": claim.documents,
            "audit_context": {"milestone": "M0", "data_source": "mock"},
        }

    graph = StateGraph(ClaimState)
    graph.add_node("load_claim", load_claim)
    graph.add_edge(START, "load_claim")
    graph.add_edge("load_claim", END)
    return graph.compile()


def build_m1_graph(repository: MockClaimRepository, **adapters):
    from agent.nodes.happy_path import build_happy_path

    return build_happy_path(repository, **adapters)


def build_m2_graph(repository: MockClaimRepository, *, checkpointer, **adapters):
    from agent.nodes.happy_path import build_happy_path

    return build_happy_path(
        repository, checkpointer=checkpointer, human_in_loop=True, **adapters
    )
