"""Opt-in fault injection shared only by the local mock adapters."""

from enum import StrEnum


class FailureMode(StrEnum):
    NONE = "NONE"
    TIMEOUT_ONCE = "TIMEOUT_ONCE"
    TIMEOUT_ALWAYS = "TIMEOUT_ALWAYS"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    SUCCESS_THEN_RESPONSE_LOST = "SUCCESS_THEN_RESPONSE_LOST"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"


class FaultInjector:
    def __init__(self, mode: FailureMode = FailureMode.NONE):
        self.mode = FailureMode(mode)
        self.calls = 0

    def before_call(self):
        self.calls += 1
        if self.mode == FailureMode.TIMEOUT_ALWAYS or (
            self.mode == FailureMode.TIMEOUT_ONCE and self.calls == 1
        ):
            raise TimeoutError("Mock dependency timeout")
        if self.mode == FailureMode.TEMPORARY_ERROR and self.calls == 1:
            raise ConnectionError("Mock temporary connection failure")

    def after_commit(self):
        if self.mode == FailureMode.SUCCESS_THEN_RESPONSE_LOST and self.calls == 1:
            raise TimeoutError("Mock response lost after durable commit")
