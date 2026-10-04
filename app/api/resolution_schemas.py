"""Request and response models for the verified resolution loop.

Two shapes are deliberately absent from the request models.

There is no field for a deeplink. Every URI the user is shown is read from the session's
stored plan snapshot at the session's own cursor, so a client can ask to move forward but
cannot name where it moves to.

There is no field for evidence source or trust. The server assigns provenance according
to which code path produced the reading, because a client that could label its own
evidence trusted could mint `system_verified` at will.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.contract.schema import ContextDeeplinkResponse


class StartSessionRequest(BaseModel):
    """Open a session on a plan the caller already received from /v1/troubleshoot.

    The plan is re-validated before a session exists. A plan that was valid when it was
    generated may reference a deeplink the catalog no longer authorises, and a plan that
    never came from this service at all is rejected by the same check.
    """

    query: str = Field(min_length=1, description="The complaint the plan answers.")
    plan: ContextDeeplinkResponse
    request_id: str | None = Field(
        default=None, description="The request_id of the troubleshoot call, when known."
    )

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query must not be blank")
        return cleaned


class ObservationRequest(BaseModel):
    """A value the user read off their own device and typed in.

    Recorded as client-reported evidence whatever else the body contains.
    """

    observed_value: str = Field(min_length=1)
    observation_token: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Client-generated idempotency key. Re-submitting the same token returns the "
            "attempt the first call recorded instead of adding another."
        ),
    )


class ConfirmRequest(BaseModel):
    resolved: bool = Field(description="True if the user reports the problem is fixed.")


class AdvanceRequest(BaseModel):
    acknowledge_critical: bool = Field(
        default=False,
        description=(
            "Required before a critical action is presented. Critical actions are "
            "disruptive, so the user agrees to one before it is shown."
        ),
    )


class CompleteRequest(BaseModel):
    status: str = Field(
        default="abandoned",
        description="One of resolved, unresolved, inconclusive, abandoned.",
    )


# ------------------------------------------------------------------ responses


class ExpectedContractView(BaseModel):
    deeplink: str
    key: str
    resultType: str
    condition: str
    value: str
    summary: str


class CurrentStepView(BaseModel):
    goal_title: str
    goal: str
    action_name: str
    description: str
    category: str
    is_critical: bool
    steps: list[str] = Field(default_factory=list)
    actionable_deeplink: str | None = None
    actionable_label: str = ""
    verification_available: bool = False
    verification_unavailable_reason: str | None = None
    expected: ExpectedContractView | None = None
    step_number: int
    step_total: int


class AttemptView(BaseModel):
    attempt_no: int
    action_name: str
    action_category: str
    verification_status: str
    reason_code: str
    evidence_source: str
    expected: dict[str, Any] | None = None
    observed_value: str | None = None
    detail: str = ""
    created_at: datetime | None = None


class SessionView(BaseModel):
    session_id: str
    status: str
    verification_status: str
    reason_code: str | None = None
    query: str
    request_id: str | None = None
    current: CurrentStepView | None = None
    next_is_critical: bool = False
    has_next: bool = False
    attempts: list[AttemptView] = Field(default_factory=list)
    adapter: str
    automatic_verification_possible: bool = Field(
        description=(
            "False whenever the configured adapter cannot read device state, which is "
            "the case for this deployment."
        )
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    idempotent_replay: bool = False


class ReceiptView(BaseModel):
    """The record a finished session leaves behind.

    verification_method is the field a reader checks first: a receipt backed by the
    user's own account must never be mistakable for one backed by a reading the server
    took, so the two are different values and are rendered differently.
    """

    session_id: str
    query: str
    final_status: str
    verification_status: str
    verification_method: str
    system_verified: bool
    actions_attempted: int
    successful_action: str | None = None
    expected_state: str | None = None
    observed_state: str | None = None
    attempts: list[AttemptView] = Field(default_factory=list)
    completed_at: datetime | None = None
    caveat: str | None = None
