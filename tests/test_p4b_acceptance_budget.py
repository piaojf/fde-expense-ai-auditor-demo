"""Synthetic transport only: every budget test is forbidden to use a real network."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from evals.p4b_acceptance import AUTHORIZATION, AcceptanceBudget, live_provider
from integrations.llm.compliance import ComplianceDeepSeekProvider
from integrations.llm.deepseek import DeepSeekError
from tests.test_p4b_compliance import request


def guarded(tmp_path, *, maximum=2, handler=None, **changes):
    provider = ComplianceDeepSeekProvider(
        api_key="synthetic-key-not-a-real-credential",
        model="deepseek-flash",
        max_tokens=4096,
        transport=httpx.MockTransport(handler or (lambda _: httpx.Response(503))),
    )
    return AcceptanceBudget(
        provider,
        tmp_path / "batch.sqlite3",
        **{
            "consent": AUTHORIZATION,
            "input_price": "0.30",
            "output_price": "1.20",
            "maximum": maximum,
            **changes,
        },
    )


def test_default_factory_does_not_read_credentials_or_activate_network(monkeypatch):
    monkeypatch.delenv("P4B_LIVE_AUTHORIZATION", raising=False)
    with pytest.raises(DeepSeekError, match="EXPLICIT_LIVE_CONSENT_REQUIRED"):
        live_provider()


@pytest.mark.parametrize(
    "changes",
    [
        {"consent": None},
        {"maximum": 21},
        {"input_price": "NaN"},
        {"input_price": "100"},
        {"output_price": "0"},
    ],
)
def test_invalid_consent_caps_or_prices_never_create_a_ledger(tmp_path, changes):
    with pytest.raises(DeepSeekError):
        guarded(tmp_path, **changes)
    assert not (tmp_path / "batch.sqlite3").exists()


def test_failures_reserve_before_http_and_survive_restart(tmp_path):
    seen = []

    def reject(req):
        seen.append(req)
        return httpx.Response(503)

    for _ in range(2):
        p = guarded(tmp_path, handler=reject)
        try:
            with pytest.raises(DeepSeekError):
                asyncio.run(p.analyze(request()))
        finally:
            p.db.close()
    p = guarded(tmp_path, handler=reject)
    try:
        with pytest.raises(DeepSeekError, match="LOCAL_AI_BUDGET_EXHAUSTED"):
            asyncio.run(p.analyze(request()))
        assert len(seen) == p.usage()["used"] == 2
        assert p.usage()["reserved_upper_usd"] == "0.0398304"
    finally:
        p.db.close()
    assert b"synthetic-key" not in (tmp_path / "batch.sqlite3").read_bytes()


def test_existing_batch_cannot_be_increased_or_repriced(tmp_path):
    p = guarded(tmp_path)
    p.db.close()
    with pytest.raises(ValueError):
        guarded(tmp_path, maximum=3)
    with pytest.raises(DeepSeekError, match="CONFIGURATION_CHANGED"):
        guarded(tmp_path, input_price="0.31")


@pytest.mark.parametrize("mutation", ["source", "version", "empty", "oversize"])
def test_unverifiable_or_oversize_inputs_do_not_consume_budget(tmp_path, mutation):
    p = guarded(tmp_path)
    req = request()
    if mutation == "source":
        req.evidence[0] = req.evidence[0].model_copy(update={"source_text": "invented"})
    if mutation == "version":
        req.policy_version_id = "WRONG"
    if mutation == "empty":
        req.evidence = []
    if mutation == "oversize":
        req.facts["candidate"]["notes"] = "x" * 50000
    try:
        with pytest.raises(DeepSeekError):
            asyncio.run(p.analyze(req))
        assert p.usage()["used"] == 0
    finally:
        p.db.close()


def test_separate_connections_cannot_race_over_budget(tmp_path):
    first = guarded(tmp_path, maximum=1)
    second = guarded(tmp_path, maximum=1)

    def attempt(p):
        try:
            asyncio.run(p.analyze(request()))
        except DeepSeekError:
            pass

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, (first, second)))
        assert first.usage()["used"] == second.usage()["used"] == 1
    finally:
        first.db.close()
        second.db.close()
