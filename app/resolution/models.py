"""Session state, and the cursor that walks a validated plan.

The cursor matters more than it looks. Every deeplink the user is ever shown is read out
of the session's own immutable plan snapshot at the position the cursor names. Nothing is
taken from the request body, so a client cannot steer the session at a URI of its
choosing: the worst it can do is ask to move to the next position, which the service
grants or refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterator

from app.contract.schema import (
    Action,
    ContextDeeplinkResponse,
    Goal,
    StepGroup,
    actionCategory,
)
from app.verification.comparator import (
    EvidenceSource,
    ExpectedContract,
    ReasonCode,
    VerificationStatus,
    contract_from,
)


class SessionStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    INCONCLUSIVE = "inconclusive"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        SessionStatus.RESOLVED,
        SessionStatus.UNRESOLVED,
        SessionStatus.INCONCLUSIVE,
        SessionStatus.ABANDONED,
    }
)


class InvalidTransition(Exception):
    """Raised when a caller asks for a move the state machine does not allow."""


@dataclass(frozen=True)
class Position:
    """Where in the plan a session currently stands."""

    goal: int = 0
    action: int = 0
    step_group: int = 0

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.goal, self.action, self.step_group)


@dataclass(frozen=True)
class PlanStep:
    """One addressable step group, with everything the console needs to present it."""

    position: Position
    goal: Goal
    action: Action
    step_group: StepGroup
    index: int
    total: int

    @property
    def goal_title(self) -> str:
        return self.goal.title

    @property
    def action_name(self) -> str:
        return self.action.actionName

    @property
    def category(self) -> actionCategory:
        return self.action.category or actionCategory.manual

    @property
    def is_critical(self) -> bool:
        return self.category is actionCategory.critical

    @property
    def steps(self) -> list[str]:
        return list(self.step_group.steps)

    @property
    def actionable_deeplink(self) -> str | None:
        link = self.step_group.actionableDeeplink
        return link.deeplink if link and link.deeplink else None

    @property
    def actionable_label(self) -> str:
        link = self.step_group.actionableDeeplink
        if not link:
            return ""
        return link.message or link.description or "Open on device"

    @property
    def expected(self) -> ExpectedContract | None:
        """The verification contract, or None when the catalog entry is only partial."""
        return contract_from(self.step_group.validationDeeplink)

    @property
    def verification_available(self) -> bool:
        return self.expected is not None

    def unavailable_reason(self) -> ReasonCode:
        if self.step_group.validationDeeplink is None:
            return ReasonCode.NO_VALIDATION_DEEPLINK
        return ReasonCode.INCOMPLETE_CONTRACT


def walk(plan: ContextDeeplinkResponse) -> list[PlanStep]:
    """Flatten a plan into the ordered list of step groups a session walks.

    Ordering follows the plan exactly. The pipeline already forces disruptive actions
    last, so preserving order is what keeps a session from opening with a factory reset.
    """
    steps: list[PlanStep] = []
    for g, goal in enumerate(plan.contexts):
        for a, action in enumerate(goal.actions):
            for s, group in enumerate(action.stepGroups):
                steps.append(
                    PlanStep(
                        position=Position(g, a, s),
                        goal=goal,
                        action=action,
                        step_group=group,
                        index=len(steps),
                        total=0,
                    )
                )
    # total is only knowable once the walk is complete.
    return [
        PlanStep(
            position=step.position,
            goal=step.goal,
            action=step.action,
            step_group=step.step_group,
            index=step.index,
            total=len(steps),
        )
        for step in steps
    ]


def step_at(plan: ContextDeeplinkResponse, position: Position) -> PlanStep | None:
    for step in walk(plan):
        if step.position == position:
            return step
    return None


def next_position(plan: ContextDeeplinkResponse, position: Position) -> Position | None:
    """The next step group in plan order, or None when the plan is exhausted."""
    found = False
    for step in walk(plan):
        if found:
            return step.position
        if step.position == position:
            found = True
    return None


@dataclass
class Attempt:
    """One recorded verification attempt. Append-only; a retry adds another."""

    attempt_no: int
    position: Position
    action_name: str
    action_category: str
    verification_status: VerificationStatus
    reason_code: ReasonCode
    evidence_source: EvidenceSource
    expected_contract: dict | None = None
    observed_value: str | None = None
    detail: str = ""
    created_at: datetime | None = None


@dataclass
class ResolutionSession:
    """A guided walk through one validated plan."""

    id: str
    query: str
    plan: ContextDeeplinkResponse
    status: SessionStatus = SessionStatus.PENDING
    position: Position = field(default_factory=Position)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    reason_code: ReasonCode | None = None
    request_id: str | None = None
    plan_id: int | None = None
    cache_version: int | None = None
    catalog_source: str | None = None
    action_presented_at: datetime | None = None
    critical_ack_at: datetime | None = None
    resolved_action: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def current(self) -> PlanStep | None:
        return step_at(self.plan, self.position)

    @property
    def steps(self) -> list[PlanStep]:
        return walk(self.plan)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def require_active(self) -> None:
        if self.is_terminal:
            raise InvalidTransition(
                f"session is {self.status.value} and accepts no further changes"
            )

    def iter_remaining(self) -> Iterator[PlanStep]:
        seen = False
        for step in self.steps:
            if seen:
                yield step
            if step.position == self.position:
                seen = True
