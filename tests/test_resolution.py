"""The verified resolution loop, end to end against the real database.

These tests use the real session store and the real state machine, with a fake trusted
adapter standing in for a Galaxy device. No model call is made: the plans are built here
from real catalog entries, because the loop's behaviour depends on the validation
contracts in those entries and not on how the plan was generated.
"""

from __future__ import annotations

import json
import pathlib
import uuid

import pytest

from app.contract.schema import (
    Action,
    ContextDeeplinkResponse,
    Deeplink,
    Goal,
    StepGroup,
    ValidationDeepLink,
    actionCategory,
)
from app.resolution.models import InvalidTransition, SessionStatus
from app.resolution.service import PlanRejected, ResolutionService, SessionNotFound
from app.verification.comparator import EvidenceSource, ReasonCode, VerificationStatus
from app.verification.fake import FakeTrustedAdapter

pytest.importorskip("psycopg")

CATALOG = pathlib.Path("data/official/deeplinks.json")


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def catalog_entries():
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    entries = raw["deeplinks"]
    complete = [
        e
        for e in entries
        if (e.get("validation") or {}).get("condition")
        and (e.get("validation") or {}).get("resultType")
        and (e.get("validation") or {}).get("value") is not None
    ]
    if len(complete) < 2:
        pytest.skip("official catalog has too few complete validation contracts")
    return complete


@pytest.fixture
def permitted(catalog_entries):
    """Exactly the URIs the real catalog authorises, of both kinds."""
    uris = set()
    for entry in catalog_entries:
        uris.add(entry["deeplink"])
        validation = entry.get("validation") or {}
        if validation.get("deeplink"):
            uris.add(validation["deeplink"])
    return frozenset(uris)


def _step_group(entry, *, with_validation=True):
    validation = entry.get("validation") or {}
    return StepGroup(
        steps=[
            "Navigate to and open Settings.",
            f"Select {entry.get('message') or 'the setting'}.",
        ],
        actionableDeeplink=Deeplink(
            deeplink=entry["deeplink"],
            description=entry.get("description", "Opens the setting."),
            message=entry.get("message", ""),
        ),
        validationDeeplink=(
            ValidationDeepLink(
                deeplink=validation["deeplink"],
                key=validation["key"],
                resultType=validation["resultType"],
                condition=validation["condition"],
                value=validation["value"],
            )
            if with_validation and validation.get("deeplink")
            else None
        ),
    )


def build_plan(entries, *, categories=None, with_validation=True):
    """A plan whose step groups come from real catalog entries, so it validates."""
    categories = categories or [actionCategory.auto] * len(entries)
    # actionName must be Title Case and the title 2-3 words in sentence case; these are
    # the contract's own rules, so the fixtures satisfy them rather than bypassing them.
    names = ["Check Display Settings", "Restart The Device", "Inspect Screen Hardware"]
    actions = [
        Action(
            actionName=names[i % len(names)],
            description="It will open your device settings",
            stepGroups=[_step_group(entry, with_validation=with_validation)],
            category=category,
        )
        for i, (entry, category) in enumerate(zip(entries, categories))
    ]
    return ContextDeeplinkResponse(
        contexts=[
            Goal(
                goal="Follow these steps to perform this Display Troubleshooting",
                title="Screen display damage",
                actions=actions,
                score=0.9,
            )
        ]
    )


@pytest.fixture
async def pool(settings, db_connection):
    from app.bootstrap import build_pool

    created = build_pool(settings)
    await created.open()
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def adapter():
    return FakeTrustedAdapter()


@pytest.fixture
def service(pool, permitted, adapter):
    return ResolutionService(pool=pool, permitted_deeplinks=permitted, adapter=adapter)


@pytest.fixture
def two_step_plan(catalog_entries):
    return build_plan(catalog_entries[:2])


def seed_for(adapter, plan, step_index, value):
    """Seed the fake device with a reading for one step group's validation contract."""
    from app.resolution.models import walk

    step = walk(plan)[step_index]
    contract = step.expected
    adapter.seed(contract.deeplink, contract.key, value)
    return contract


# ------------------------------------------------------------- session opening


async def test_a_validated_plan_opens_a_session(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)

    assert session.status is SessionStatus.PENDING
    assert session.verification_status is VerificationStatus.PENDING
    assert session.position.as_tuple() == (0, 0, 0)
    assert len(session.steps) == 2
    assert session.current.actionable_deeplink is not None


async def test_a_plan_carrying_an_unauthorised_uri_never_becomes_a_session(
    service, catalog_entries
):
    """The security case that matters: a client cannot have the service present a URI
    the catalog does not authorise by submitting it inside a plan."""
    plan = build_plan(catalog_entries[:1])
    plan.contexts[0].actions[0].stepGroups[0].actionableDeeplink.deeplink = (
        "bixby://attacker/act/0000000000"
    )

    with pytest.raises(PlanRejected) as excinfo:
        await service.start(query="My screen is black", plan=plan)
    assert excinfo.value.violations


async def test_an_empty_plan_is_rejected(service):
    with pytest.raises(PlanRejected):
        await service.start(
            query="My screen is black", plan=ContextDeeplinkResponse(contexts=[])
        )


async def test_a_structurally_invalid_plan_is_rejected(service, catalog_entries):
    plan = build_plan(catalog_entries[:1])
    # Title length is bounded by the contract; this one is far outside it.
    plan.contexts[0].title = "A title that is far too long to satisfy the contract rule"
    with pytest.raises(PlanRejected):
        await service.start(query="My screen is black", plan=plan)


async def test_an_unknown_session_is_not_found(service):
    with pytest.raises(SessionNotFound):
        await service.get(str(uuid.uuid4()))


# ------------------------------------------------------------- persistence


async def test_a_session_survives_a_reload(service, two_step_plan):
    opened = await service.start(query="My screen is black", plan=two_step_plan)
    reloaded = await service.get(opened.id)

    assert reloaded.id == opened.id
    assert reloaded.query == opened.query
    assert reloaded.position == opened.position
    # The snapshot round-trips through JSONB unchanged, which is what lets the session
    # keep walking a plan the cache may have since replaced.
    assert reloaded.plan.model_dump() == opened.plan.model_dump()


async def test_progress_is_not_written_back_into_the_cached_plan(
    service, two_step_plan, pool
):
    """A cached plan is shared by every matching request, so session progress must not
    reach it."""
    session = await service.start(query="My screen is black", plan=two_step_plan)
    await service.mark_presented(session.id)
    await service.advance(session.id)

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT plan_snapshot FROM resolution_session WHERE id = %s",
                (session.id,),
            )
        ).fetchone()

    snapshot = row[0]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    assert snapshot == two_step_plan.model_dump(mode="json")


# ------------------------------------------------------- presenting an action


async def test_opening_a_deeplink_verifies_nothing(service, two_step_plan):
    """The central truthfulness rule: a click is not evidence about the device."""
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.mark_presented(session.id)

    assert outcome.session.status is SessionStatus.IN_PROGRESS
    assert outcome.session.verification_status is VerificationStatus.PENDING
    assert outcome.session.attempts == []


# ---------------------------------------------------- automatic verification


async def test_a_trusted_reading_that_matches_is_system_verified(
    service, two_step_plan, adapter
):
    contract = seed_for(adapter, two_step_plan, 0, "True")
    session = await service.start(query="My screen is black", plan=two_step_plan)

    outcome = await service.verify(session.id)

    assert outcome.attempt.verification_status is VerificationStatus.SYSTEM_VERIFIED
    assert outcome.attempt.evidence_source is EvidenceSource.TRUSTED_ADAPTER
    assert outcome.attempt.observed_value == "True"
    assert outcome.attempt.expected_contract["key"] == contract.key


async def test_a_trusted_reading_that_contradicts_the_contract_fails(
    service, two_step_plan, adapter
):
    seed_for(adapter, two_step_plan, 0, "False")
    session = await service.start(query="My screen is black", plan=two_step_plan)

    outcome = await service.verify(session.id)

    assert outcome.attempt.verification_status is VerificationStatus.VERIFICATION_FAILED
    assert outcome.attempt.reason_code is ReasonCode.VALUE_MISMATCH


async def test_an_unreadable_device_reports_unavailable_not_failed(
    service, two_step_plan
):
    """Nothing was seeded, so the adapter read nothing. That is unavailable, not a
    failed verification: the distinction is what keeps the console honest."""
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.verify(session.id)

    assert (
        outcome.attempt.verification_status
        is VerificationStatus.VERIFICATION_UNAVAILABLE
    )
    assert outcome.attempt.reason_code is ReasonCode.ADAPTER_UNAVAILABLE


async def test_the_production_adapter_can_never_system_verify(
    pool, permitted, two_step_plan
):
    """Wired as it ships, automatic verification is simply unavailable."""
    shipped = ResolutionService(pool=pool, permitted_deeplinks=permitted)
    assert shipped.adapter_name == "unavailable"

    session = await shipped.start(query="My screen is black", plan=two_step_plan)
    outcome = await shipped.verify(session.id)

    assert (
        outcome.attempt.verification_status
        is VerificationStatus.VERIFICATION_UNAVAILABLE
    )


async def test_a_step_without_a_validation_contract_is_unavailable(
    service, catalog_entries
):
    plan = build_plan(catalog_entries[:1], with_validation=False)
    session = await service.start(query="My screen is black", plan=plan)

    outcome = await service.verify(session.id)

    assert (
        outcome.attempt.verification_status
        is VerificationStatus.VERIFICATION_UNAVAILABLE
    )
    assert outcome.attempt.reason_code is ReasonCode.NO_VALIDATION_DEEPLINK


# -------------------------------------------------------- client-reported path


async def test_client_reported_evidence_is_never_system_verified(
    service, two_step_plan
):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.submit_observation(session.id, observed_value="True")

    assert outcome.attempt.evidence_source is EvidenceSource.CLIENT_REPORTED
    assert outcome.attempt.verification_status is VerificationStatus.INCONCLUSIVE
    assert outcome.attempt.verification_status is not VerificationStatus.SYSTEM_VERIFIED


async def test_client_reported_evidence_can_still_fail(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.submit_observation(session.id, observed_value="False")

    assert outcome.attempt.verification_status is VerificationStatus.VERIFICATION_FAILED


async def test_an_unparseable_client_value_is_inconclusive(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.submit_observation(session.id, observed_value="maybe")

    assert outcome.attempt.verification_status is VerificationStatus.INCONCLUSIVE
    assert outcome.attempt.reason_code is ReasonCode.OBSERVED_NOT_BOOLEAN


async def test_resubmitting_one_observation_token_records_a_single_attempt(
    service, two_step_plan
):
    """A client retrying after a timeout must not add a second attempt."""
    session = await service.start(query="My screen is black", plan=two_step_plan)
    token = str(uuid.uuid4())

    first = await service.submit_observation(
        session.id, observed_value="True", observation_token=token
    )
    second = await service.submit_observation(
        session.id, observed_value="True", observation_token=token
    )

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.attempt.attempt_no == first.attempt.attempt_no

    reloaded = await service.get(session.id)
    assert len(reloaded.attempts) == 1


# ---------------------------------------------------------- user confirmation


async def test_user_confirmation_resolves_the_session_but_is_labelled_as_such(
    service, two_step_plan
):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.confirm(session.id, resolved=True)

    assert outcome.session.status is SessionStatus.RESOLVED
    assert outcome.session.verification_status is VerificationStatus.USER_CONFIRMED
    assert outcome.attempt.evidence_source is EvidenceSource.USER_CONFIRMATION
    assert outcome.session.completed_at is not None
    assert outcome.session.resolved_action


async def test_a_user_reporting_no_fix_records_a_failure_and_keeps_going(
    service, two_step_plan
):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    outcome = await service.confirm(session.id, resolved=False)

    assert outcome.session.status is SessionStatus.IN_PROGRESS
    assert outcome.session.verification_status is VerificationStatus.VERIFICATION_FAILED
    assert outcome.session.completed_at is None


# ----------------------------------------------------------------- advancing


async def test_advancing_moves_to_the_next_action_in_the_plan(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    first = session.current.action_name

    outcome = await service.advance(session.id)

    assert outcome.session.position.as_tuple() != (0, 0, 0)
    assert outcome.session.current.action_name != first
    # A fresh action starts unverified rather than inheriting the previous verdict.
    assert outcome.session.verification_status is VerificationStatus.PENDING


async def test_running_out_of_actions_ends_the_session_unresolved(
    service, catalog_entries
):
    """No replacement action is invented. Ending honestly leaves the user better
    informed than a fabricated next step would."""
    plan = build_plan(catalog_entries[:1])
    session = await service.start(query="My screen is black", plan=plan)

    outcome = await service.advance(session.id)

    assert outcome.session.status is SessionStatus.UNRESOLVED
    assert outcome.session.completed_at is not None


async def test_a_critical_action_is_not_presented_without_acknowledgement(
    service, catalog_entries
):
    plan = build_plan(
        catalog_entries[:2], categories=[actionCategory.auto, actionCategory.critical]
    )
    session = await service.start(query="My screen is black", plan=plan)

    with pytest.raises(InvalidTransition):
        await service.advance(session.id)

    outcome = await service.advance(session.id, acknowledge_critical=True)
    assert outcome.session.current.is_critical
    assert outcome.session.critical_ack_at is not None


# ------------------------------------------------------- invalid transitions


async def test_a_finished_session_refuses_further_changes(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    await service.confirm(session.id, resolved=True)

    for call in (
        service.mark_presented(session.id),
        service.verify(session.id),
        service.submit_observation(session.id, observed_value="True"),
        service.confirm(session.id, resolved=True),
        service.advance(session.id),
    ):
        with pytest.raises(InvalidTransition):
            await call


async def test_completing_with_a_non_terminal_status_is_rejected(
    service, two_step_plan
):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    with pytest.raises(InvalidTransition):
        await service.complete(session.id, status=SessionStatus.IN_PROGRESS)


async def test_abandoning_a_session_is_terminal(service, two_step_plan):
    session = await service.start(query="My screen is black", plan=two_step_plan)
    finished = await service.complete(session.id, status=SessionStatus.ABANDONED)

    assert finished.status is SessionStatus.ABANDONED
    assert finished.completed_at is not None


# --------------------------------------------------- the full guided sequence


async def test_a_failed_action_leads_to_the_next_validated_action(
    service, catalog_entries, adapter
):
    """The loop the feature exists for: verify, fail, move to the next existing safe
    action, verify again, succeed."""
    plan = build_plan(catalog_entries[:2])
    seed_for(adapter, plan, 0, "False")
    seed_for(adapter, plan, 1, "True")

    session = await service.start(query="My screen is black", plan=plan)

    await service.mark_presented(session.id)
    first = await service.verify(session.id)
    assert first.attempt.verification_status is VerificationStatus.VERIFICATION_FAILED

    moved = await service.advance(session.id)
    assert moved.session.verification_status is VerificationStatus.PENDING

    await service.mark_presented(session.id)
    second = await service.verify(session.id)
    assert second.attempt.verification_status is VerificationStatus.SYSTEM_VERIFIED

    final = await service.complete(session.id, status=SessionStatus.RESOLVED)
    assert final.status is SessionStatus.RESOLVED
    assert len(final.attempts) == 2


# ------------------------------------------------------- migration idempotency


def test_rerunning_migrations_is_a_no_op(db_connection):
    """Migrations are applied on every start, so a second run must change nothing.

    The resolution tables use ALTER ... ADD CONSTRAINT, which is not idempotent on its
    own; each is preceded by DROP CONSTRAINT IF EXISTS for exactly this reason.
    """
    from app.db.migrate import applied_migrations, run_migrations

    before = applied_migrations(db_connection)
    assert "004_resolution_sessions.sql" in before, "run python -m scripts.init_db first"

    assert run_migrations(db_connection) == [], "a second run applied something again"
    assert applied_migrations(db_connection) == before


def test_the_database_refuses_untrusted_system_verification(db_connection):
    """The safety rule is enforced by a CHECK constraint as well as by the service, so
    no future code path can record a trusted verdict for untrusted evidence."""
    import psycopg

    session_id = str(uuid.uuid4())
    with db_connection.transaction():
        db_connection.execute(
            """
            INSERT INTO resolution_session (id, query, plan_snapshot)
            VALUES (%s, %s, %s)
            """,
            (session_id, "constraint probe", json.dumps({"contexts": []})),
        )

    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            with db_connection.transaction():
                db_connection.execute(
                    """
                    INSERT INTO resolution_attempt (
                        session_id, attempt_no, goal_index, action_index,
                        step_group_index, action_name, evidence_source,
                        verification_status, reason_code
                    ) VALUES (%s, 1, 0, 0, 0, 'Probe', 'client_reported',
                              'system_verified', 'matched')
                    """,
                    (session_id,),
                )
    finally:
        with db_connection.transaction():
            db_connection.execute(
                "DELETE FROM resolution_session WHERE id = %s", (session_id,)
            )
