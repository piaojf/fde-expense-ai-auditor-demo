"""Snapshot-only graph factory with no business write or checkpoint capability."""

from copy import deepcopy

from agent.nodes.happy_path import _build_audit_graph
from domain.schemas import ClaimDetail


class _SnapshotJournal:
    def __init__(self, claim):
        self._claim = ClaimDetail.model_validate(claim.model_dump())
        self.diagnostics = []

    def get(self, claim_id):
        return self._claim.model_copy(deep=True) if claim_id == self._claim.claim_id else None

    def append_events(self, events):
        # Shared dependency instrumentation is local; never a formal audit repository.
        self.diagnostics.extend(e.model_copy(deep=True) for e in events)


class _SnapshotContext:
    def __init__(self, claim_id, context):
        self._claim_id, self._context = claim_id, deepcopy(context)

    async def fetch_claim_context(self, claim_id):
        if claim_id != self._claim_id:
            raise ValueError("Snapshot claim mismatch")
        return deepcopy(self._context)


def build_shadow_graph(claim: ClaimDetail, *, erp_context: dict, catalog, retriever, provider,
                       parser, retry_policies=None):
    """Only detached inputs and analysis adapters; no repository/ERP/action injection."""
    return _build_audit_graph(
        _SnapshotJournal(claim),
        erp=_SnapshotContext(claim.claim_id, erp_context),
        catalog=catalog,
        retriever=retriever,
        provider=provider,
        parser=parser,
        retry_policies=retry_policies,
        shadow_boundary=True,
    )
