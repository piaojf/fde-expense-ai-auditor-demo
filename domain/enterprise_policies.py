"""v1.5 document scope; wildcard drafts do not grant executable rule scope."""

from typing import Literal

from pydantic import Field, field_validator

from domain.policies import ConfirmPolicy, PolicyMetadata, PreviewPolicy
from domain.policy_publication import RuleConfirmation

DocumentExpense = Literal["ALL", "HOTEL", "MEAL", "TRANSPORT", "OTHER"]


class EnterprisePolicyMetadata(PolicyMetadata):
    expense_type: DocumentExpense


class EnterpriseConfirmPolicy(ConfirmPolicy):
    metadata: EnterprisePolicyMetadata


class EnterprisePreviewPolicy(PreviewPolicy):
    expense_type: DocumentExpense


class EnterpriseRuleConfirmation(RuleConfirmation):
    source_mode: Literal["AUTOMATIC", "MANUAL"] = "AUTOMATIC"
    conditions: str = Field(default="", max_length=1000)

    @field_validator("conditions")
    @classmethod
    def visible_conditions(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("Conditions must contain visible text")
        return value.strip()
