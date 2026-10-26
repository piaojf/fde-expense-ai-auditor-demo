"""Unimplemented Qwen/OpenAI adapters; DeepSeek lives in deepseek.py."""

from integrations.llm.base import AnalysisRequest


class ReservedProvider:
    async def analyze(self, request: AnalysisRequest):
        raise NotImplementedError("Real LLM adapter is not configured in M1")


class QwenProvider(ReservedProvider):
    pass


class OpenAIProvider(ReservedProvider):
    pass
