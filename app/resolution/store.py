"""Persistence for resolution sessions.

Sessions are written here and nowhere else, and the cached plan is never touched: a
session reads its plan from its own snapshot column, so progress through a plan cannot
leak into the shared cache or change what the next request is served.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.contract.schema import ContextDeeplinkResponse
from app.resolution.models import (
    Attempt,
    Position,
    ResolutionSession,
    SessionStatus,
)
from app.verification.comparator import EvidenceSource, ReasonCode, VerificationStatus

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(conn: AsyncConnection, session: ResolutionSession) -> None:
    """Insert a new session. The plan snapshot is written once and never updated."""
    await conn.execute(
        """
        INSERT INTO resolution_session (
            id, request_id, plan_id, query, plan_snapshot, cache_version,
            catalog_source, status, goal_index, action_index, step_group_index,
            verification_status, reason_code
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session.id,
            session.request_id,
            session.plan_id,
            session.query,
            json.dumps(session.plan.model_dump(mode="json")),
            session.cache_version,
            session.catalog_source,
            session.status.value,
            session.position.goal,
            session.position.action,
            session.position.step_group,
            session.verification_status.value,
            session.reason_code.value if session.reason_code else None,
        ),
    )


async def get_session(conn: AsyncConnection, session_id: str) -> ResolutionSession | None:
    """Load a session and its attempt history, or None when the id is unknown."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resolution_session WHERE id = %s", (session_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None

        await cur.execute(
            """
            SELECT * FROM resolution_attempt
            WHERE session_id = %s
            ORDER BY attempt_no
            """,
            (session_id,),
        )
        attempt_rows = await cur.fetchall()

    snapshot = row["plan_snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)

    session = ResolutionSession(
        id=str(row["id"]),
        query=row["query"],
        plan=ContextDeeplinkResponse.model_validate(snapshot),
        status=SessionStatus(row["status"]),
        position=Position(
            row["goal_index"], row["action_index"], row["step_group_index"]
        ),
        verification_status=VerificationStatus(row["verification_status"]),
        reason_code=ReasonCode(row["reason_code"]) if row["reason_code"] else None,
        request_id=str(row["request_id"]) if row["request_id"] else None,
        plan_id=row["plan_id"],
        cache_version=row["cache_version"],
        catalog_source=row["catalog_source"],
        action_presented_at=row["action_presented_at"],
        critical_ack_at=row["critical_ack_at"],
        resolved_action=row["resolved_action"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )

    session.attempts = [
        Attempt(
            attempt_no=a["attempt_no"],
            position=Position(
                a["goal_index"], a["action_index"], a["step_group_index"]
            ),
            action_name=a["action_name"],
            action_category=a["action_category"] or "",
            verification_status=VerificationStatus(a["verification_status"]),
            reason_code=ReasonCode(a["reason_code"]),
            evidence_source=EvidenceSource(a["evidence_source"]),
            expected_contract=a["expected_contract"],
            observed_value=a["observed_value"],
            detail=a["detail"] or "",
            created_at=a["created_at"],
        )
        for a in attempt_rows
    ]
    return session


async def save_progress(conn: AsyncConnection, session: ResolutionSession) -> None:
    """Persist the mutable part of a session. plan_snapshot is deliberately excluded."""
    await conn.execute(
        """
        UPDATE resolution_session SET
            status = %s,
            goal_index = %s,
            action_index = %s,
            step_group_index = %s,
            verification_status = %s,
            reason_code = %s,
            action_presented_at = %s,
            critical_ack_at = %s,
            resolved_action = %s,
            completed_at = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            session.status.value,
            session.position.goal,
            session.position.action,
            session.position.step_group,
            session.verification_status.value,
            session.reason_code.value if session.reason_code else None,
            session.action_presented_at,
            session.critical_ack_at,
            session.resolved_action,
            session.completed_at,
            session.id,
        ),
    )


async def find_attempt_by_token(
    conn: AsyncConnection, session_id: str, token: str
) -> Attempt | None:
    """Return the attempt a given observation token already produced, if any.

    This is what makes observation submission idempotent: a client retrying after a
    timeout gets back the attempt its first call recorded instead of adding a second.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM resolution_attempt
            WHERE session_id = %s AND observation_token = %s
            """,
            (session_id, token),
        )
        row = await cur.fetchone()

    if row is None:
        return None

    return Attempt(
        attempt_no=row["attempt_no"],
        position=Position(
            row["goal_index"], row["action_index"], row["step_group_index"]
        ),
        action_name=row["action_name"],
        action_category=row["action_category"] or "",
        verification_status=VerificationStatus(row["verification_status"]),
        reason_code=ReasonCode(row["reason_code"]),
        evidence_source=EvidenceSource(row["evidence_source"]),
        expected_contract=row["expected_contract"],
        observed_value=row["observed_value"],
        detail=row["detail"] or "",
        created_at=row["created_at"],
    )


async def next_attempt_no(conn: AsyncConnection, session_id: str) -> int:
    row = await (
        await conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM resolution_attempt "
            "WHERE session_id = %s",
            (session_id,),
        )
    ).fetchone()
    return int(row[0])


async def record_attempt(
    conn: AsyncConnection,
    session_id: str,
    attempt: Attempt,
    observation_token: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO resolution_attempt (
            session_id, attempt_no, goal_index, action_index, step_group_index,
            action_name, action_category, expected_contract, observed_value,
            evidence_source, verification_status, reason_code, detail,
            observation_token
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            attempt.attempt_no,
            attempt.position.goal,
            attempt.position.action,
            attempt.position.step_group,
            attempt.action_name,
            attempt.action_category,
            json.dumps(attempt.expected_contract) if attempt.expected_contract else None,
            attempt.observed_value,
            attempt.evidence_source.value,
            attempt.verification_status.value,
            attempt.reason_code.value,
            attempt.detail,
            observation_token,
        ),
    )
