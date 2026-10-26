"""Opt-in synthetic acceptance guard; importing this module never opens a socket."""

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from integrations.llm.budgeted import BudgetedLLM
from integrations.llm.compliance import ComplianceDeepSeekProvider
from integrations.llm.deepseek import DeepSeekError
from rag.retrieval.compliance import original_chunks

ROOT = Path(__file__).resolve().parents[1]
AUTHORIZATION = "APPROVED_20_REQUESTS_USD1"


class AcceptanceBudget(BudgetedLLM):
    """One durable budget shared by semantic tests and a future consented browser runner.

    The reused provider makes at most one HTTP request per analyze attempt. The graph's
    retries invoke this wrapper again, so every possible HTTP attempt reserves a slot.
    Unknown billed usage after a failure never returns a reservation to the batch.
    """

    def __init__(self, provider, path, *, consent, input_price, output_price, maximum=20):
        if consent != AUTHORIZATION:
            raise DeepSeekError("P4B_EXPLICIT_LIVE_CONSENT_REQUIRED")
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise DeepSeekError("P4B_INVALID_BATCH_CAP")
        if not isinstance(provider, ComplianceDeepSeekProvider) or (
            provider.model != "deepseek-flash"
            or provider._url != "https://api.deepseek.com/chat/completions"
            or provider.max_tokens != 4096
        ):
            raise DeepSeekError("P4B_UNAPPROVED_PROVIDER_OR_OUTPUT_BUDGET")
        prices = tuple(Decimal(p) for p in (input_price, output_price))
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise DeepSeekError("P4B_PRICING_REQUIRED")
        self.reserved_usd = (Decimal(50000) * prices[0] + Decimal(4096) * prices[1]) / 1000000
        if self.reserved_usd * maximum > Decimal("1"):
            raise DeepSeekError("P4B_COST_CAP_EXCEEDED")
        config = json.dumps(
            dict(
                model=provider.model,
                maximum=maximum,
                input_tokens=50000,
                output_tokens=4096,
                input_price=str(prices[0]),
                output_price=str(prices[1]),
                total_usd_cap="1",
            ),
            sort_keys=True,
        )
        super().__init__(provider, path, maximum)
        try:
            with self.lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("CREATE TABLE IF NOT EXISTS p4b_acceptance_config (body TEXT)")
                prior = self.db.execute("SELECT body FROM p4b_acceptance_config").fetchone()
                if prior and prior[0] != config:
                    raise DeepSeekError("P4B_EXISTING_BATCH_CONFIGURATION_CHANGED")
                if not prior:
                    self.db.execute("INSERT INTO p4b_acceptance_config VALUES (?)", (config,))
        except BaseException:
            self.db.close()
            raise

    async def analyze(self, request):
        if not request.evidence:
            raise DeepSeekError("P4B_NO_ORIGINAL_EVIDENCE")
        first = request.evidence[0]
        original = original_chunks(
            (ROOT / "tests/fixtures/policies/p4a-222.docx").read_bytes(),
            document_id=first.document_id,
            policy_version_id=request.policy_version_id,
            effective_from=date(2026, 11, 1),
            effective_to=date(2027, 1, 1),
        )
        by_id = {c.chunk_id: c for c in original}
        if any(c != by_id.get(c.chunk_id) for c in request.evidence):
            raise DeepSeekError("P4B_UNVERIFIABLE_ORIGINAL_EVIDENCE")
        body = self.provider._body(request)
        wire = httpx.Request("POST", self.provider._url, json=body).content
        # UTF-8 bytes conservatively bound ordinary text tokens. Reserve additional
        # message framing capacity without claiming this is actual metered usage.
        if len(wire) + 256 > 50000:
            raise DeepSeekError("P4B_INPUT_BUDGET_EXCEEDED")
        return await super().analyze(request)

    def usage(self):
        result = super().usage()
        return {
            **result,
            "reserved_upper_usd": str(self.reserved_usd * result["used"]),
            "actual_tokens": "SEE_PROVIDER_USAGE; failures may have unknown usage",
        }


def live_provider():
    """Operator may call only after the human approves this batch and prices are rechecked.

    No .env read or edit, model fallback, ledger reset, network setup or service activation.
    Price arguments must be freshly confirmed official peak/non-cache rates before execution.
    """
    consent = os.getenv("P4B_LIVE_AUTHORIZATION")
    if consent != AUTHORIZATION:
        raise DeepSeekError("P4B_EXPLICIT_LIVE_CONSENT_REQUIRED")
    if os.getenv("P4B_PRICING_CHECKED") != "true":
        raise DeepSeekError("P4B_FRESH_PRICING_CHECK_REQUIRED")
    provider = ComplianceDeepSeekProvider(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        model="deepseek-flash",
        max_tokens=4096,
    )
    return AcceptanceBudget(
        provider,
        ROOT / "output/p4b/p4b-live-acceptance.sqlite3",
        consent=consent,
        input_price=os.environ["P4B_CHECKED_INPUT_PRICE_PER_MILLION"],
        output_price=os.environ["P4B_CHECKED_OUTPUT_PRICE_PER_MILLION"],
    )
