"""REST surface for the verified resolution loop.

Handlers stay thin: they translate HTTP to a service call and back. The state machine,
the trust boundary and the comparison all live below this layer, so no safety rule can be
bypassed by reaching a different endpoint.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.resolution_schemas import (
    AdvanceRequest,
    AttemptView,
    CompleteRequest,
    ConfirmRequest,
    CurrentStepView,
    ExpectedContractView,
    ObservationRequest,
    ReceiptView,
    SessionView,
    StartSessionRequest,
)
from app.resolution.models import (
    InvalidTransition,
    PlanStep,
    ResolutionSession,
    SessionStatus,
    next_position,
    step_at,
)
from app.resolution.service import (
    PlanRejected,
    ResolutionService,
    SessionNotFound,
    StepOutcome,
)
from app.verification.comparator import VerificationStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/resolution", tags=["resolution"])


def get_resolution_service(request: Request) -> ResolutionService:
    service = getattr(request.app.state, "resolution", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="resolution service is still initializing; check GET /health",
        )
    return service


def _step_view(step: PlanStep) -> CurrentStepView:
    contract = step.expected
    return CurrentStepView(
        goal_title=step.goal_title,
        goal=step.goal.goal,
        action_name=step.action_name,
        description=step.action.description,
        category=step.category.value,
        is_critical=step.is_critical,
        steps=step.steps,
        actionable_deeplink=step.actionable_deeplink,
        actionable_label=step.actionable_label,
        verification_available=contract is not None,
        verification_unavailable_reason=(
            None if contract else step.unavailable_reason().value
        ),
        expected=(
            ExpectedContractView(**contract.as_dict(), summary=contract.describe())
            if contract
            else None
        ),
        step_number=step.index + 1,
        step_total=step.total,
    )


def _session_view(
    session: ResolutionSession,
    service: ResolutionService,
    *,
    idempotent_replay: bool = False,
) -> SessionView:
    step = session.current
    following = next_position(session.plan, session.position)
    upcoming = step_at(session.plan, following) if following else None

    return SessionView(
        session_id=session.id,
        status=session.status.value,
        verification_status=session.verification_status.value,
        reason_code=session.reason_code.value if session.reason_code else None,
        query=session.query,
        request_id=session.request_id,
        current=_step_view(step) if step else None,
        has_next=following is not None,
        next_is_critical=bool(upcoming and upcoming.is_critical),
        attempts=[
            AttemptView(
                attempt_no=a.attempt_no,
                action_name=a.action_name,
                action_category=a.action_category,
                verification_status=a.verification_status.value,
                reason_code=a.reason_code.value,
                evidence_source=a.evidence_source.value,
                expected=a.expected_contract,
                observed_value=a.observed_value,
                detail=a.detail,
                created_at=a.created_at,
            )
            for a in session.attempts
        ],
        adapter=service.adapter_name,
        automatic_verification_possible=service.adapter_name != "unavailable",
        created_at=session.created_at,
        updated_at=session.updated_at,
        completed_at=session.completed_at,
        idempotent_replay=idempotent_replay,
    )


def _view(outcome: StepOutcome, service: ResolutionService) -> SessionView:
    return _session_view(
        outcome.session, service, idempotent_replay=outcome.idempotent_replay
    )


# ------------------------------------------------------------------- handlers


@router.post("/sessions", response_model=SessionView, status_code=201)
async def start_session(
    payload: StartSessionRequest,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Open a guided session on a validated plan."""
    try:
        session = await service.start(
            query=payload.query, plan=payload.plan, request_id=payload.request_id
        )
    except PlanRejected as exc:
        # 422 rather than 400: the body parsed, but the plan is not one this service is
        # willing to guide a user through.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "plan_rejected",
                "message": "the submitted plan did not pass validation",
                "violations": exc.violations[:20],
            },
        ) from exc
    return _session_view(session, service)


@router.get("/sessions/{session_id}", response_model=SessionView)
async def read_session(
    session_id: str,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    return _session_view(await _load(service, session_id), service)


@router.post("/sessions/{session_id}/presented", response_model=SessionView)
async def mark_presented(
    session_id: str,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Record that the action was shown or its deeplink opened.

    This changes no verification status. Opening a settings screen is not evidence about
    the resulting device state.
    """
    return _view(await _call(service.mark_presented(session_id)), service)


@router.post("/sessions/{session_id}/verify", response_model=SessionView)
async def verify(
    session_id: str,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Attempt automatic verification through the trusted adapter.

    With no device channel configured this reports verification_unavailable, which is the
    truthful result rather than a failure.
    """
    return _view(await _call(service.verify(session_id)), service)


@router.post("/sessions/{session_id}/observations", response_model=SessionView)
async def submit_observation(
    session_id: str,
    payload: ObservationRequest,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Submit a value the user read from their device. Always client-reported."""
    return _view(
        await _call(
            service.submit_observation(
                session_id,
                observed_value=payload.observed_value,
                observation_token=payload.observation_token,
            )
        ),
        service,
    )


@router.post("/sessions/{session_id}/confirm", response_model=SessionView)
async def confirm(
    session_id: str,
    payload: ConfirmRequest,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Record the user's own account of whether the problem is fixed."""
    return _view(await _call(service.confirm(session_id, resolved=payload.resolved)),
                 service)


@router.post("/sessions/{session_id}/advance", response_model=SessionView)
async def advance(
    session_id: str,
    payload: AdvanceRequest,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    """Move to the next action already present in the validated plan."""
    return _view(
        await _call(
            service.advance(
                session_id, acknowledge_critical=payload.acknowledge_critical
            )
        ),
        service,
    )


@router.post("/sessions/{session_id}/complete", response_model=SessionView)
async def complete(
    session_id: str,
    payload: CompleteRequest,
    service: ResolutionService = Depends(get_resolution_service),
) -> SessionView:
    try:
        status = SessionStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown session status {payload.status!r}"
        ) from exc

    session = await _call_session(service.complete(session_id, status=status))
    return _session_view(session, service)


@router.get("/sessions/{session_id}/receipt", response_model=ReceiptView)
async def receipt(
    session_id: str,
    service: ResolutionService = Depends(get_resolution_service),
) -> ReceiptView:
    """The record a finished session leaves behind."""
    session = await _load(service, session_id)
    if not session.is_terminal:
        raise HTTPException(
            status_code=409,
            detail="the session is still in progress and has no receipt yet",
        )
    return _receipt(session)


# ------------------------------------------------------------------ internals


def _receipt(session: ResolutionSession) -> ReceiptView:
    decisive = None
    for attempt in reversed(session.attempts):
        if attempt.verification_status in {
            VerificationStatus.SYSTEM_VERIFIED,
            VerificationStatus.USER_CONFIRMED,
        }:
            decisive = attempt
            break

    system_verified = (
        decisive is not None
        and decisive.verification_status is VerificationStatus.SYSTEM_VERIFIED
    )
    if decisive is None:
        method = "none"
        caveat = (
            "No action in this plan was verified or confirmed. The session ended "
            f"{session.status.value}."
        )
    elif system_verified:
        method = "system_verified"
        caveat = None
    else:
        method = "user_confirmed"
        caveat = (
            "This outcome rests on the user's own report. The server did not read the "
            "device state back, so it is not a system verification."
        )

    expected = None
    if decisive and decisive.expected_contract:
        contract = decisive.expected_contract
        expected = f"{contract.get('key')} {contract.get('condition')} {contract.get('value')}"

    return ReceiptView(
        session_id=session.id,
        query=session.query,
        final_status=session.status.value,
        verification_status=session.verification_status.value,
        verification_method=method,
        system_verified=system_verified,
        actions_attempted=len({a.position.as_tuple() for a in session.attempts}),
        successful_action=decisive.action_name if decisive else None,
        expected_state=expected,
        observed_state=decisive.observed_value if decisive else None,
        attempts=[
            AttemptView(
                attempt_no=a.attempt_no,
                action_name=a.action_name,
                action_category=a.action_category,
                verification_status=a.verification_status.value,
                reason_code=a.reason_code.value,
                evidence_source=a.evidence_source.value,
                expected=a.expected_contract,
                observed_value=a.observed_value,
                detail=a.detail,
                created_at=a.created_at,
            )
            for a in session.attempts
        ],
        completed_at=session.completed_at,
        caveat=caveat,
    )


async def _load(service: ResolutionService, session_id: str) -> ResolutionSession:
    try:
        return await service.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc


async def _call(awaitable) -> StepOutcome:
    try:
        return await awaitable
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except InvalidTransition as exc:
        # 409: the request is well formed, but the session is not in a state that allows
        # it. Distinguishing this from a malformed body matters to a client deciding
        # whether to retry.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _call_session(awaitable) -> ResolutionSession:
    try:
        return await awaitable
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
