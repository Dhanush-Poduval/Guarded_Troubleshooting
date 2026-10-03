"""The verified resolution loop.

    plan -> present an action -> user performs it -> check the resulting state
         -> verified, or guide to the next safe action already in the plan

Four rules shape this module, and each exists because its opposite is a plausible shortcut.

A session only ever walks a plan that passed validation at session start. The plan is
re-validated here rather than trusted from the caller, because a plan that was valid when
it was generated may reference a deeplink the catalog no longer authorises.

Deeplinks are read from the stored snapshot at the session's own cursor. A caller can ask
to move forward; it cannot name the URI it moves to. That is what stops a client turning
this endpoint into an opener for arbitrary URIs.

Trust is assigned by the code path, never by the request. Evidence a client posts is
client_reported whatever the body says, and only a reading this service obtained through
the adapter is trusted_adapter. The comparator then refuses to call anything else
system_verified.

Nothing is ever resolved because a link was clicked. Opening a deeplink records that an
action was presented, which is not evidence about the device, so it moves no status.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg_pool import AsyncConnectionPool

from app.contract.schema import ContextDeeplinkResponse
from app.resolution import store
from app.resolution.models import (
    Attempt,
    InvalidTransition,
    PlanStep,
    Position,
    ResolutionSession,
    SessionStatus,
    next_position,
)
from app.validation.rules import validate_plan
from app.verification.adapter import DeviceVerificationAdapter, UnavailableAdapter
from app.verification.comparator import (
    EvidenceSource,
    Observation,
    ReasonCode,
    VerificationStatus,
    compare,
)

logger = logging.getLogger(__name__)


class PlanRejected(Exception):
    """The submitted plan did not pass validation, so no session was created."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations) or "plan failed validation")
        self.violations = violations


class SessionNotFound(Exception):
    pass


@dataclass
class StepOutcome:
    """The result of one loop operation, as the API and console need to render it."""

    session: ResolutionSession
    attempt: Attempt | None = None
    idempotent_replay: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ResolutionService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        permitted_deeplinks: frozenset[str],
        adapter: DeviceVerificationAdapter | None = None,
    ) -> None:
        self._pool = pool
        self._permitted = permitted_deeplinks
        # Defaults to the adapter that reads nothing, because this deployment has no
        # device channel. A real one is injected here and nowhere else.
        self._adapter: DeviceVerificationAdapter = adapter or UnavailableAdapter()

    @property
    def adapter_name(self) -> str:
        return getattr(self._adapter, "name", "unknown")

    # ------------------------------------------------------------------ start

    async def start(
        self,
        *,
        query: str,
        plan: ContextDeeplinkResponse,
        request_id: str | None = None,
        plan_id: int | None = None,
        cache_version: int | None = None,
        catalog_source: str | None = None,
    ) -> ResolutionSession:
        """Validate the plan, then open a session on an immutable snapshot of it."""
        report = validate_plan(plan, self._permitted)
        if not report.ok:
            # Includes the case that matters most: a plan carrying a URI the catalog does
            # not authorise never becomes a session, so it can never be presented.
            raise PlanRejected([str(v) for v in report.violations])

        if not plan.contexts:
            raise PlanRejected(["plan contains no contexts to walk"])

        session = ResolutionSession(
            id=str(uuid.uuid4()),
            query=query,
            plan=plan,
            status=SessionStatus.PENDING,
            position=Position(0, 0, 0),
            request_id=request_id,
            plan_id=plan_id,
            cache_version=cache_version,
            catalog_source=catalog_source,
        )
        if session.current is None:
            raise PlanRejected(["plan contains no step groups to walk"])

        async with self._pool.connection() as conn:
            await store.create_session(conn, session)

        logger.info(
            "resolution session %s opened on %d step groups",
            session.id,
            len(session.steps),
        )
        return session

    # ------------------------------------------------------------------- read

    async def get(self, session_id: str) -> ResolutionSession:
        async with self._pool.connection() as conn:
            session = await store.get_session(conn, session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    # ---------------------------------------------------------------- present

    async def mark_presented(self, session_id: str) -> StepOutcome:
        """Record that the current action was shown to, or opened by, the user.

        This moves the session into progress and nothing more. Opening a settings screen
        says nothing about the resulting device state, so no verification status changes
        here. Treating a click as success is exactly the false signal this loop exists to
        avoid.
        """
        session = await self.get(session_id)
        session.require_active()

        session.status = SessionStatus.IN_PROGRESS
        session.action_presented_at = _now()
        await self._save(session)
        return StepOutcome(session=session)

    # ------------------------------------------------------------- verify now

    async def verify(self, session_id: str) -> StepOutcome:
        """Attempt automatic verification through the trusted adapter.

        In this deployment the adapter reads nothing, so the honest outcome is
        verification_unavailable and the console falls back to user confirmation.
        """
        session = await self.get(session_id)
        session.require_active()

        step = self._require_step(session)
        contract = step.expected

        if contract is None:
            return await self._record(
                session,
                step,
                status=VerificationStatus.VERIFICATION_UNAVAILABLE,
                reason=step.unavailable_reason(),
                source=EvidenceSource.TRUSTED_ADAPTER,
                observed=None,
                detail="this step has no complete validation contract to check against",
            )

        reading = await self._adapter.read_state(
            deeplink=contract.deeplink, key=contract.key
        )
        if not reading.available or reading.value is None:
            return await self._record(
                session,
                step,
                status=VerificationStatus.VERIFICATION_UNAVAILABLE,
                reason=reading.reason or ReasonCode.ADAPTER_UNAVAILABLE,
                source=EvidenceSource.TRUSTED_ADAPTER,
                observed=None,
                detail=reading.detail,
                contract=contract,
            )

        outcome = compare(
            contract,
            Observation(
                key=contract.key,
                value=reading.value,
                source=EvidenceSource.TRUSTED_ADAPTER,
            ),
        )
        return await self._record(
            session,
            step,
            status=outcome.status,
            reason=outcome.reason,
            source=EvidenceSource.TRUSTED_ADAPTER,
            observed=reading.value,
            detail=outcome.detail,
            contract=contract,
        )

    # --------------------------------------------------------- client evidence

    async def submit_observation(
        self,
        session_id: str,
        *,
        observed_value: str,
        observation_token: str | None = None,
    ) -> StepOutcome:
        """Accept a value the user read off their own device.

        The source is fixed to client_reported here regardless of anything in the
        request, so a client cannot promote its own evidence. A satisfied comparison on
        this path is reported as inconclusive rather than verified.
        """
        session = await self.get(session_id)
        session.require_active()
        step = self._require_step(session)

        if observation_token:
            async with self._pool.connection() as conn:
                existing = await store.find_attempt_by_token(
                    conn, session_id, observation_token
                )
            if existing is not None:
                # A retry after a timeout must not record a second attempt.
                return StepOutcome(
                    session=session, attempt=existing, idempotent_replay=True
                )

        contract = step.expected
        if contract is None:
            return await self._record(
                session,
                step,
                status=VerificationStatus.VERIFICATION_UNAVAILABLE,
                reason=step.unavailable_reason(),
                source=EvidenceSource.CLIENT_REPORTED,
                observed=observed_value,
                detail="this step has no complete validation contract to check against",
                token=observation_token,
            )

        outcome = compare(
            contract,
            Observation(
                key=contract.key,
                value=observed_value,
                source=EvidenceSource.CLIENT_REPORTED,
            ),
        )
        return await self._record(
            session,
            step,
            status=outcome.status,
            reason=outcome.reason,
            source=EvidenceSource.CLIENT_REPORTED,
            observed=observed_value,
            detail=outcome.detail,
            contract=contract,
            token=observation_token,
        )

    # ------------------------------------------------------- user confirmation

    async def confirm(self, session_id: str, *, resolved: bool) -> StepOutcome:
        """Record the user's own account of whether the problem is fixed.

        This is a first-class outcome, not a weaker form of verification, and it is
        labelled user_confirmed everywhere it appears so a reader never mistakes it for a
        reading the server took.
        """
        session = await self.get(session_id)
        session.require_active()
        step = self._require_step(session)

        if resolved:
            outcome = await self._record(
                session,
                step,
                status=VerificationStatus.USER_CONFIRMED,
                reason=ReasonCode.USER_CONFIRMED_RESOLVED,
                source=EvidenceSource.USER_CONFIRMATION,
                observed=None,
                detail="the user reported the problem resolved after this action",
            )
            await self._finish(
                outcome.session,
                SessionStatus.RESOLVED,
                resolved_action=step.action_name,
            )
            return StepOutcome(session=outcome.session, attempt=outcome.attempt)

        return await self._record(
            session,
            step,
            status=VerificationStatus.VERIFICATION_FAILED,
            reason=ReasonCode.USER_REPORTED_UNRESOLVED,
            source=EvidenceSource.USER_CONFIRMATION,
            observed=None,
            detail="the user reported the problem persists after this action",
        )

    # -------------------------------------------------------------- advancing

    async def advance(
        self, session_id: str, *, acknowledge_critical: bool = False
    ) -> StepOutcome:
        """Move to the next step group already present in the validated plan.

        No replacement action is ever generated. When the plan runs out the session ends
        as unresolved, which is a truthful outcome and leaves the user better informed
        than an invented step would.
        """
        session = await self.get(session_id)
        session.require_active()

        following = next_position(session.plan, session.position)
        if following is None:
            await self._finish(session, SessionStatus.UNRESOLVED)
            return StepOutcome(session=session)

        from app.resolution.models import step_at

        upcoming = step_at(session.plan, following)
        if upcoming is not None and upcoming.is_critical and not acknowledge_critical:
            # A critical action is disruptive — a forced restart, a factory reset, a
            # service visit. The user agrees to it before it is presented.
            raise InvalidTransition(
                "the next action is critical and requires explicit acknowledgement"
            )

        session.position = following
        session.status = SessionStatus.IN_PROGRESS
        session.verification_status = VerificationStatus.PENDING
        session.reason_code = None
        session.action_presented_at = None
        if upcoming is not None and upcoming.is_critical:
            session.critical_ack_at = _now()
        await self._save(session)
        return StepOutcome(session=session)

    # -------------------------------------------------------------- finishing

    async def complete(
        self, session_id: str, *, status: SessionStatus
    ) -> ResolutionSession:
        if status not in {
            SessionStatus.RESOLVED,
            SessionStatus.UNRESOLVED,
            SessionStatus.INCONCLUSIVE,
            SessionStatus.ABANDONED,
        }:
            raise InvalidTransition(f"{status.value} is not a terminal status")

        session = await self.get(session_id)
        session.require_active()
        await self._finish(session, status)
        return session

    # --------------------------------------------------------------- internals

    def _require_step(self, session: ResolutionSession) -> PlanStep:
        step = session.current
        if step is None:
            raise InvalidTransition("the session cursor is past the end of its plan")
        return step

    async def _record(
        self,
        session: ResolutionSession,
        step: PlanStep,
        *,
        status: VerificationStatus,
        reason: ReasonCode,
        source: EvidenceSource,
        observed: str | None,
        detail: str = "",
        contract=None,
        token: str | None = None,
    ) -> StepOutcome:
        async with self._pool.connection() as conn:
            attempt_no = await store.next_attempt_no(conn, session.id)
            attempt = Attempt(
                attempt_no=attempt_no,
                position=step.position,
                action_name=step.action_name,
                action_category=step.category.value,
                verification_status=status,
                reason_code=reason,
                evidence_source=source,
                expected_contract=contract.as_dict() if contract else None,
                observed_value=observed,
                detail=detail,
            )
            await store.record_attempt(conn, session.id, attempt, token)

            session.attempts.append(attempt)
            session.verification_status = status
            session.reason_code = reason
            if session.status is SessionStatus.PENDING:
                session.status = SessionStatus.IN_PROGRESS
            await store.save_progress(conn, session)

        return StepOutcome(session=session, attempt=attempt)

    async def _save(self, session: ResolutionSession) -> None:
        async with self._pool.connection() as conn:
            await store.save_progress(conn, session)

    async def _finish(
        self,
        session: ResolutionSession,
        status: SessionStatus,
        resolved_action: str | None = None,
    ) -> None:
        session.status = status
        session.completed_at = _now()
        if resolved_action:
            session.resolved_action = resolved_action
        await self._save(session)
        logger.info("resolution session %s finished as %s", session.id, status.value)
