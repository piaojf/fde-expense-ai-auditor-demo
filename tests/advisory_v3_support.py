"""Authored synthetic outputs over actual v3 MockTransport; never real model evidence."""

import json

import httpx
from advisory_v2_support import output_v2

from integrations.llm.advisory_v3 import AdvisoryDeepSeekProviderV3
from integrations.llm.evidence_selector import selector_manifest


def output_v3(request, codes=("GENERAL_FINANCE_CHECK",)):
    data = output_v2(request, codes)
    manifest = selector_manifest(request)
    by_id = {e["original_chunk_id"]: e["short_id"] for e in manifest["entries"]}
    data.update(schema_version="p4b.advisory.3",
                evidence_snapshot_id=manifest["evidence_snapshot_id"])
    for item in [data, *data["issues"]]:
        item["policy_ref_keys"] = [by_id[r] for r in item.pop("policy_ref_ids")]
    return data


class AdvisoryWireV3:
    protocol_version = "p4b.advisory.3"
    model = "offline-advisory-v3-wire"

    def __init__(self, configure=None):
        self.configure, self.mode = configure, None
        self.requests, self.bodies = [], []

    async def analyze(self, request):
        self.requests.append(request)

        def handler(wire):
            self.bodies.append(json.loads(wire.content))
            assert len(wire.content) <= 32768
            data = output_v3(request)
            if self.configure:
                self.configure(data, request)
            content = "{" if self.mode == "invalid_json" else json.dumps(data, ensure_ascii=False)
            return httpx.Response(200, json=dict(model=self.model, choices=[dict(
                finish_reason="stop", message=dict(role="assistant", content=content))]))

        class RecordingProvider(AdvisoryDeepSeekProviderV3):
            def _body(self, request):
                try:
                    return super()._body(request)
                except Exception as exc:
                    request._provider_evidence["synthetic_body_failure"] = str(exc)
                    raise

        return await RecordingProvider(
            api_key="offline-only-key", model=self.model, max_tokens=2048,
            transport=httpx.MockTransport(handler),
        ).analyze(request)
