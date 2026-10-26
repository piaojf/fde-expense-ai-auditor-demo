"""Explicit OpenAI-style remote dense embeddings; no model download or fallback."""

import math
import time

import httpx
from pydantic import SecretStr

from rag.embeddings.base import BGE_M3_DENSE_DIMENSIONS


class RemoteEmbeddingError(Exception):
    """Safe structured error: never retain a response body, request, URL or key."""

    def __init__(self, code, *, status_code=None, retryable=False, attempts=0):
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.attempts = attempts
        super().__init__(f"Remote embedding failed: {code}")

    def as_dict(self):
        return dict(code=self.code, status_code=self.status_code,
                    retryable=self.retryable, attempts=self.attempts)


class RemoteEmbeddingUnavailable(RemoteEmbeddingError, ConnectionError):
    """Exhausted transient failures retain the existing M3 retry classification."""


class RemoteEmbeddingProvider:
    """One configured dialect. Response model identity is mandatory and exact.

    base_url ends at the service API root (e.g. https://api.siliconflow.cn/v1).
    Batch size 1 sends string input for services that do not accept arrays.
    Each call owns its client so concurrent retriever threads share no client lifecycle.
    httpx inherits existing network settings; redirects are refused to protect the key.
    """

    RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

    def __init__(self, *, base_url, api_key, model_name, dimension=1024, timeout=15.0,
                 max_attempts=3, batch_size=16, transport=None, sleeper=time.sleep,
                 event_hooks=None):
        try:
            url = httpx.URL(base_url)
        except (TypeError, httpx.InvalidURL):
            raise RemoteEmbeddingError("INVALID_BASE_URL") from None
        if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment):
            raise RemoteEmbeddingError("INVALID_BASE_URL")
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not isinstance(key, str) or not key.strip():
            raise RemoteEmbeddingError("API_KEY_REQUIRED")
        if any(c in key for c in "\r\n"):
            raise RemoteEmbeddingError("INVALID_API_KEY")
        if not isinstance(model_name, str) or not model_name.strip():
            raise RemoteEmbeddingError("MODEL_REQUIRED")
        if dimension != BGE_M3_DENSE_DIMENSIONS:
            raise RemoteEmbeddingError("SCHEMA_DIMENSION_MISMATCH")
        if (not math.isfinite(timeout) or not 0 < timeout <= 120
                or not 1 <= max_attempts <= 5 or not 1 <= batch_size <= 128):
            raise RemoteEmbeddingError("INVALID_REQUEST_BUDGET")
        self.model_name, self.dimension = model_name, dimension
        self._url = str(url).rstrip("/") + "/embeddings"
        self._api_key = SecretStr(key)
        self.timeout, self.max_attempts, self.batch_size = timeout, max_attempts, batch_size
        self._transport, self._sleeper = transport, sleeper
        self._event_hooks = event_hooks
        self.actual_model = None
        self.actual_dimension = None

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise RemoteEmbeddingError("EMPTY_INPUT")
        result = []
        with httpx.Client(timeout=self.timeout, transport=self._transport,
                          follow_redirects=False, event_hooks=self._event_hooks) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start:start + self.batch_size]
                result.extend(self._request(client, batch))
        return result

    def _request(self, client, batch):
        for attempt in range(1, self.max_attempts + 1):
            status = None
            try:
                response = client.post(
                    self._url,
                    headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
                    json={"model": self.model_name,
                          "input": batch[0] if self.batch_size == 1 else batch,
                          "encoding_format": "float"},
                )
            except httpx.TimeoutException:
                code = "TIMEOUT"
            except httpx.TransportError:
                code = "TRANSPORT_UNAVAILABLE"
            else:
                status = response.status_code
                if status in self.RETRY_STATUSES:
                    code = "SERVICE_UNAVAILABLE"
                elif status != 200:
                    raise RemoteEmbeddingError(
                        "HTTP_REJECTED", status_code=status, attempts=attempt,
                    ) from None
                else:
                    return self._decode(response, len(batch), attempt)
            if attempt == self.max_attempts:
                raise RemoteEmbeddingUnavailable(
                    code, status_code=status, retryable=True, attempts=attempt,
                ) from None
            self._sleeper(min(0.25 * 2 ** (attempt - 1), 2.0))
        raise AssertionError("Request budget must execute at least one attempt")

    def _decode(self, response, count, attempts):
        try:
            payload = response.json()
        except ValueError:
            raise RemoteEmbeddingError("INVALID_RESPONSE", attempts=attempts) from None
        if not isinstance(payload, dict):
            raise RemoteEmbeddingError("INVALID_RESPONSE", attempts=attempts)
        if payload.get("model") != self.model_name:
            raise RemoteEmbeddingError("RESPONSE_MODEL_MISMATCH", attempts=attempts)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != count:
            raise RemoteEmbeddingError("RESPONSE_COUNT_MISMATCH", attempts=attempts)
        ordered = {}
        for item in data:
            if not isinstance(item, dict):
                raise RemoteEmbeddingError("INVALID_RESPONSE", attempts=attempts)
            index, vector = item.get("index"), item.get("embedding")
            if type(index) is not int or not 0 <= index < count or index in ordered:
                raise RemoteEmbeddingError("INVALID_RESPONSE_INDEX", attempts=attempts)
            if not isinstance(vector, list) or len(vector) != self.dimension:
                raise RemoteEmbeddingError("RESPONSE_DIMENSION_MISMATCH", attempts=attempts)
            if any(type(v) not in (int, float) for v in vector):
                raise RemoteEmbeddingError("INVALID_VECTOR", attempts=attempts)
            try:
                values = [float(v) for v in vector]
            except (OverflowError, ValueError):
                raise RemoteEmbeddingError("INVALID_VECTOR", attempts=attempts) from None
            if not all(math.isfinite(v) for v in values) or not any(values):
                raise RemoteEmbeddingError("INVALID_VECTOR", attempts=attempts)
            ordered[index] = values
        self.actual_model, self.actual_dimension = payload["model"], self.dimension
        return [ordered[index] for index in range(count)]
