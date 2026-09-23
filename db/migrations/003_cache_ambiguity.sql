-- Observability for the semantic cache's ambiguity check.
--
-- A single similarity threshold cannot separate "same problem, different words" from
-- "different problem, same words" when every query sits in one domain. Measured on the
-- official query set, 34 of 190 pairs of genuinely distinct problems score above 0.78,
-- and the closest pair of distinct problems reaches 0.8742.
--
-- The cache therefore also requires the best-matching plan to beat the runner-up plan by
-- a margin. These columns record what the check saw, so the margin can be tuned from data
-- rather than guessed, and so the rate of ambiguity rejections is visible.

ALTER TABLE request_metrics
    ADD COLUMN IF NOT EXISTS runner_up_similarity DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cache_reject_reason  TEXT;

ALTER TABLE request_metrics
    DROP CONSTRAINT IF EXISTS request_metrics_reject_reason_chk;
ALTER TABLE request_metrics
    ADD CONSTRAINT request_metrics_reject_reason_chk
    CHECK (cache_reject_reason IS NULL
           OR cache_reject_reason IN ('below_threshold', 'ambiguous', 'failed_revalidation'));
