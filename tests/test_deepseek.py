"""Offline transport tests; these are not real API evidence."""

import asyncio
import json

import httpx
import pytest

from integrations.llm.base import AnalysisRequest
from integrations.llm.deepseek import DeepSeekLLMProvider, DeepSeekOutputError


def test_json_mode_strict_schema_and_provider_identity():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 1024
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "summary": "金额合规，住宿日期例外需人工核实。",
                                    "anomalies": ["STAY_OUTSIDE_APPROVED_TRIP_DATES"],
                                    "suggested_risk": "MEDIUM",
                                    "confidence": 0.8,
                                    "provider": "deepseek",
                                    "model": "deepseek-flash",
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            },
        )

    provider = DeepSeekLLMProvider(
        api_key="test-only-placeholder", transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(
        provider.analyze(AnalysisRequest(claim_id="synthetic", facts={}, evidence=[]))
    )
    assert result.provider == "deepseek" and result.suggested_risk == "MEDIUM"


def test_empty_is_not_low_risk():
    provider = DeepSeekLLMProvider(
        api_key="test-only-placeholder",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "deepseek-flash",
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": ""}},
                    ],
                },
            ),
        ),
    )
    with pytest.raises(DeepSeekOutputError):
        asyncio.run(provider.analyze(AnalysisRequest(claim_id="synthetic", facts={}, evidence=[])))


def output(**changes):
    return dict(
        summary="深圳住宿600元，符合2026制度700元标准。",
        anomalies=[],
        suggested_risk="LOW",
        confidence=0.9,
        provider="deepseek",
        model="deepseek-flash",
        **changes,
    )


def envelope(content=None, **changes):
    return {
        "model": "deepseek-flash",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content if content is not None else json.dumps(output()),
                },
            }
        ],
        **changes,
    }


def analyze(payload):
    request = AnalysisRequest(claim_id="synthetic", facts={}, evidence=[])
    adapter = DeepSeekLLMProvider(
        api_key="test-only-placeholder",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    return asyncio.run(adapter.analyze(request)), request


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", ""),
        ("summary", " "),
        ("summary", 42),
        ("summary", "x" * 4001),
        ("anomalies", None),
        ("anomalies", "risk"),
        ("anomalies", [1]),
        ("anomalies", [""]),
        ("anomalies", [" "]),
        ("anomalies", ["x" * 257]),
        ("anomalies", ["risk"] * 25),
        ("suggested_risk", "APPROVE"),
        ("confidence", "0.9"),
        ("confidence", True),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("confidence", float("nan")),
        ("provider", "mock"),
        ("model", "silently-substituted"),
    ],
)
def test_invalid_schema_rejected(field, value):
    data = output()
    data[field] = value
    with pytest.raises(DeepSeekOutputError):
        analyze(envelope(json.dumps(data)))


@pytest.mark.parametrize("field", list(output()))
def test_all_fields_required(field):
    data = output()
    del data[field]
    with pytest.raises(DeepSeekOutputError):
        analyze(envelope(json.dumps(data)))


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "{}",
        "[]",
        "null",
        "```json\n{}\n```",
        "x" * 16385,
        json.dumps({**output(), "decision": "APPROVE"}),
        json.dumps(output())[:-1] + ',"suggested_risk":"HIGH"}',
        json.dumps({**output(), "summary": "test-only-placeholder"}),
    ],
)
def test_malformed_extra_duplicate_and_leaked_key_rejected(content):
    with pytest.raises(DeepSeekOutputError):
        analyze(envelope(content))


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "aborted", None])
def test_unfinished_json_rejected_even_when_json_is_valid(reason):
    payload = envelope()
    payload["choices"][0]["finish_reason"] = reason
    with pytest.raises(DeepSeekOutputError):
        analyze(payload)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"model": "wrong"},
        envelope(model="wrong"),
        envelope(choices=[]),
        envelope(choices=[None]),
        envelope(choices=[{"finish_reason": "stop", "message": None}]),
        envelope(
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(output()),
                        "tool_calls": [{"name": "erp"}],
                    },
                }
            ]
        ),
    ],
)
def test_invalid_envelope_and_tools(payload):
    with pytest.raises(DeepSeekOutputError):
        analyze(payload)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": None},
        {"api_key": ""},
        {"api_key": "line\nbreak"},
        {"base_url": "http://example.com"},
        {"base_url": "https://user:pass@example.com"},
        {"base_url": "https://example.com?q=key"},
        {"base_url": "https://example.com#key"},
        {"model": ""},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"max_tokens": True},
        {"max_tokens": 100000},
    ],
)
def test_config_fail_closed(kwargs):
    from integrations.llm.deepseek import DeepSeekError

    with pytest.raises(DeepSeekError):
        DeepSeekLLMProvider(**{"api_key": "test-only-placeholder", **kwargs})


def test_usage_is_allowlisted_no_reasoning_retained():
    payload = envelope(
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "unexpected": "sensitive",
            "reasoning_tokens": 7,
        }
    )
    payload["choices"][0]["message"]["reasoning_content"] = "private-thoughts"
    result, request = analyze(payload)
    assert request._provider_evidence["token_usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    assert request._provider_evidence["schema_validated"]
    assert "private-thoughts" not in repr(result) + repr(request._provider_evidence)


def test_total_deadline_and_response_byte_limit():
    async def delayed(_):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=envelope())

    from integrations.llm.deepseek import DeepSeekTimeout

    adapter = DeepSeekLLMProvider(
        api_key="test-only-placeholder",
        timeout_seconds=0.01,
        transport=httpx.MockTransport(delayed),
    )
    request = AnalysisRequest(claim_id="test", facts={}, evidence=[])
    with pytest.raises(DeepSeekTimeout):
        asyncio.run(adapter.analyze(request))
    assert request._provider_evidence["error_code"] == "TIMEOUT"
    adapter = DeepSeekLLMProvider(
        api_key="test-only-placeholder",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 131073)),
    )
    with pytest.raises(DeepSeekOutputError):
        asyncio.run(adapter.analyze(request))


def test_input_limit_fails_before_network():
    def unexpected(_):
        pytest.fail("Oversize input must not leave machine")

    adapter = DeepSeekLLMProvider(
        api_key="test-only-placeholder", transport=httpx.MockTransport(unexpected)
    )
    with pytest.raises(DeepSeekOutputError):
        asyncio.run(
            adapter.analyze(
                AnalysisRequest(claim_id="test", facts={"notes": "x" * 32769}, evidence=[])
            )
        )


def test_explicit_config_injects_real_provider_without_request(monkeypatch):
    from apps.api.config import Settings
    from apps.api.main import create_app
    from integrations.llm.deepseek import DeepSeekError

    for name in ("DEEPSEEK_API_KEY", "LLM_PROVIDER", "POLICY_RETRIEVER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(DeepSeekError, match="API_KEY_REQUIRED"):
        create_app(Settings(app_env="test", llm_provider="deepseek", _env_file=None))
    app = create_app(
        Settings(
            app_env="test",
            llm_provider="deepseek",
            deepseek_api_key="test-only-placeholder",
            _env_file=None,
        )
    )
    assert isinstance(
        app.state.audit_service.graph_factory.keywords["provider"], DeepSeekLLMProvider
    )
    app.state.repository.close()


def test_live_gate_stops_before_settings_or_network(monkeypatch):
    from integrations.llm.deepseek import DeepSeekError
    from scripts import m4b_real_llm

    monkeypatch.delenv("RUN_LIVE_LLM_TESTS", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Gate must stop before loading live dependencies")

    monkeypatch.setattr(m4b_real_llm, "Settings", forbidden)
    with pytest.raises(DeepSeekError, match="LIVE_GATE_REQUIRED"):
        m4b_real_llm.live_settings()


def test_live_missing_key_stops_before_rag(monkeypatch):
    from apps.api.config import Settings
    from integrations.llm.deepseek import DeepSeekError
    from scripts import m4b_real_llm

    monkeypatch.setenv("RUN_LIVE_LLM_TESTS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(m4b_real_llm, "Settings", lambda **kwargs: Settings(_env_file=None))
    with pytest.raises(DeepSeekError, match="API_KEY_REQUIRED"):
        m4b_real_llm.live_settings()
