"""Bounded dependency retries; neither business rejections nor schema errors retry."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from uuid import uuid4

import httpx

from domain.schemas import AuditEvent


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    retryable_errors: tuple[type[Exception], ...] = (
        TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError,
    )
    backoff: float = 0.05
    max_backoff: float = 1.0
    retryable_statuses: tuple[int, ...] = (429, 502, 503)

    def __post_init__(self):
        if (
            self.max_attempts < 1 or self.max_attempts > 100
            or not isfinite(self.backoff) or self.backoff < 0
            or not isfinite(self.max_backoff) or self.max_backoff < 0
        ):
            raise ValueError("Invalid retry budget/backoff")

    def is_retryable(self, error):
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in self.retryable_statuses
        return isinstance(error, self.retryable_errors)

    def delay(self, attempt):
        return min(self.max_backoff, self.backoff * 2 ** (attempt - 1))


class DependencyFailure(Exception):
    def __init__(self, error, retryable, attempts, events):
        super().__init__(type(error).__name__)
        self.error_type = type(error).__name__
        self.retryable, self.attempts, self.events = retryable, attempts, events


def dependency_event(state, kind, dependency, operation, attempt, **details):
    timestamp = datetime.now(UTC)
    return AuditEvent(
        event_id=uuid4(), claim_id=state.claim_id, run_id=state.run_id,
        event_type=kind, actor=f"agent:{operation}", occurred_at=timestamp,
        payload={
            "node": operation, "dependency": dependency, "operation": operation,
            "attempt": attempt, "error_type": None, "retryable": False,
            "timestamp": timestamp.isoformat(), "summary": kind,
            "status": "INFO", "duration_ms": 0, **details,
        },
    ).model_dump(mode="json")


async def call_with_retry(state, dependency, operation, call, policy, *, sleeper=asyncio.sleep,
                          journal=None):
    events = []

    def record(kind, attempt, **details):
        item = dependency_event(state, kind, dependency, operation, attempt, **details)
        events.append(item)
        if journal:
            journal([AuditEvent.model_validate(item)])

    for attempt in range(1, policy.max_attempts + 1):
        if attempt > 1:
            record("RETRY_ATTEMPTED", attempt)
        try:
            result = await call()
        except Exception as error:
            retryable = policy.is_retryable(error)
            record("DEPENDENCY_CALL_ATTEMPTED", attempt)
            record("DEPENDENCY_CALL_FAILED", attempt, error_type=type(error).__name__,
                   retryable=retryable, status="FAILED")
            if not retryable or attempt == policy.max_attempts:
                record("RETRY_EXHAUSTED" if retryable else "DEPENDENCY_NON_RETRYABLE",
                       attempt, error_type=type(error).__name__, retryable=retryable)
                raise DependencyFailure(error, retryable, attempt, events) from error
            delay = policy.delay(attempt)
            record("RETRY_SCHEDULED", attempt, retryable=True, delay_seconds=delay)
            await sleeper(delay)
        else:
            if attempt > 1:
                record("DEPENDENCY_RECOVERED", attempt, status="SUCCEEDED")
            if isinstance(result, dict) and result.get("idempotent_replay"):
                record("IDEMPOTENT_REPLAY", attempt, status="SUCCEEDED",
                       idempotency_key=result.get("idempotency_key"), actual_side_effect=False)
            return result, events, attempt
    raise AssertionError("Retry policy must execute at least one attempt")
