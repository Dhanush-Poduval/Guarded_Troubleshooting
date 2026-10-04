-- The verified resolution loop: a complaint is answered with a plan, the user performs
-- one action at a time, and the resulting device state is checked before the next action
-- is offered.
--
-- Sessions are stored apart from the semantic plan cache on purpose. A cached plan is
-- shared by every request whose wording matches it, so writing one user's progress into
-- it would leak that progress to the next user and invalidate the plan's reusability.
-- The session therefore carries its own immutable snapshot of the plan it is walking:
-- the cache may be re-warmed, re-validated or purged underneath it without the session
-- changing what it already asked the user to do.
--
-- Every statement is idempotent, so re-running migrations is a no-op.

-- ---------------------------------------------------------------------------
-- resolution_session
--   One guided walk through one validated plan.
--   plan_snapshot is the plan as it was validated at session start. plan_id is a soft
--   reference for analysis only and is nulled rather than cascading, because losing the
--   cached plan must not destroy the record of what a user was guided through.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolution_session (
    id                   UUID PRIMARY KEY,
    request_id           UUID,
    plan_id              BIGINT REFERENCES plan_cache(id) ON DELETE SET NULL,
    query                TEXT NOT NULL,
    plan_snapshot        JSONB NOT NULL,
    cache_version        INTEGER,
    catalog_source       TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    goal_index           INTEGER NOT NULL DEFAULT 0,
    action_index         INTEGER NOT NULL DEFAULT 0,
    step_group_index     INTEGER NOT NULL DEFAULT 0,
    verification_status  TEXT NOT NULL DEFAULT 'pending',
    reason_code          TEXT,
    action_presented_at  TIMESTAMPTZ,
    critical_ack_at      TIMESTAMPTZ,
    resolved_action      TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);

ALTER TABLE resolution_session
    DROP CONSTRAINT IF EXISTS resolution_session_status_chk;
ALTER TABLE resolution_session
    ADD CONSTRAINT resolution_session_status_chk
    CHECK (status IN ('pending', 'in_progress', 'resolved', 'unresolved',
                      'inconclusive', 'abandoned'));

ALTER TABLE resolution_session
    DROP CONSTRAINT IF EXISTS resolution_session_verification_chk;
ALTER TABLE resolution_session
    ADD CONSTRAINT resolution_session_verification_chk
    CHECK (verification_status IN ('pending', 'system_verified', 'user_confirmed',
                                   'verification_failed', 'verification_unavailable',
                                   'inconclusive'));

-- A finished session must record when it finished, and an unfinished one must not.
ALTER TABLE resolution_session
    DROP CONSTRAINT IF EXISTS resolution_session_completed_chk;
ALTER TABLE resolution_session
    ADD CONSTRAINT resolution_session_completed_chk
    CHECK (
        (status IN ('resolved', 'unresolved', 'inconclusive', 'abandoned')
         AND completed_at IS NOT NULL)
        OR
        (status IN ('pending', 'in_progress') AND completed_at IS NULL)
    );

CREATE INDEX IF NOT EXISTS resolution_session_status_idx
    ON resolution_session (status);
CREATE INDEX IF NOT EXISTS resolution_session_created_idx
    ON resolution_session (created_at DESC);
CREATE INDEX IF NOT EXISTS resolution_session_request_idx
    ON resolution_session (request_id);

-- ---------------------------------------------------------------------------
-- resolution_attempt
--   One verification attempt against one step group. Append-only: a retry adds a row
--   rather than overwriting the previous reading, so the receipt can show what was tried.
--
--   observation_token makes submission idempotent. A client that retries a request after
--   a timeout must not record a second attempt, so the token is unique per session and
--   the repeat returns the row the first call wrote.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolution_attempt (
    id                   BIGSERIAL PRIMARY KEY,
    session_id           UUID NOT NULL REFERENCES resolution_session(id) ON DELETE CASCADE,
    attempt_no           INTEGER NOT NULL,
    goal_index           INTEGER NOT NULL,
    action_index         INTEGER NOT NULL,
    step_group_index     INTEGER NOT NULL,
    action_name          TEXT NOT NULL,
    action_category      TEXT,
    expected_contract    JSONB,
    observed_value       TEXT,
    evidence_source      TEXT NOT NULL,
    verification_status  TEXT NOT NULL,
    reason_code          TEXT NOT NULL,
    detail               TEXT,
    observation_token    TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE resolution_attempt
    DROP CONSTRAINT IF EXISTS resolution_attempt_evidence_chk;
ALTER TABLE resolution_attempt
    ADD CONSTRAINT resolution_attempt_evidence_chk
    CHECK (evidence_source IN ('trusted_adapter', 'client_reported', 'user_confirmation'));

ALTER TABLE resolution_attempt
    DROP CONSTRAINT IF EXISTS resolution_attempt_verification_chk;
ALTER TABLE resolution_attempt
    ADD CONSTRAINT resolution_attempt_verification_chk
    CHECK (verification_status IN ('pending', 'system_verified', 'user_confirmed',
                                   'verification_failed', 'verification_unavailable',
                                   'inconclusive'));

-- The database refuses the central safety rule as well as the service: evidence the
-- server did not obtain itself can never be recorded as system_verified.
ALTER TABLE resolution_attempt
    DROP CONSTRAINT IF EXISTS resolution_attempt_trusted_only_chk;
ALTER TABLE resolution_attempt
    ADD CONSTRAINT resolution_attempt_trusted_only_chk
    CHECK (verification_status <> 'system_verified'
           OR evidence_source = 'trusted_adapter');

CREATE INDEX IF NOT EXISTS resolution_attempt_session_idx
    ON resolution_attempt (session_id, attempt_no);

CREATE UNIQUE INDEX IF NOT EXISTS resolution_attempt_token_uidx
    ON resolution_attempt (session_id, observation_token)
    WHERE observation_token IS NOT NULL;
