-- Core storage for Phase 3 (semantic retrieval + semantic cache) and Phase 4 (REST API).
--
-- Vector columns are sized by __EMBEDDING_DIM__, substituted by app/db/migrate.py from
-- Settings.embedding_dim. Embedding dimension is fixed per column, so switching model
-- means re-running this migration against an empty database.
--
-- HNSW indexes are deliberately NOT created here. pgvector builds a graph faster once the
-- rows already exist, so index creation belongs with the Phase 3 indexing step.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- deeplink_catalog
--   System of record for approved Settings deeplinks.
--   match_text is the only text that is embedded or keyword-indexed, and it is built
--   from description / message / qna_description. The masked URI is excluded on purpose:
--   the deeplink token is obfuscated, so matching against it is meaningless.
--   source distinguishes synthetic fixture rows from the official catalog.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deeplink_catalog (
    id               BIGSERIAL PRIMARY KEY,
    deeplink         TEXT NOT NULL UNIQUE,
    description      TEXT NOT NULL,
    message          TEXT NOT NULL DEFAULT '',
    qna_description  TEXT NOT NULL DEFAULT '',
    classes          JSONB,
    original_type    TEXT,
    match_text       TEXT NOT NULL,
    embedding        vector(__EMBEDDING_DIM__),
    source           TEXT NOT NULL DEFAULT 'synthetic',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT deeplink_catalog_source_chk CHECK (source IN ('synthetic', 'official'))
);

CREATE INDEX IF NOT EXISTS deeplink_catalog_source_idx ON deeplink_catalog (source);

-- ---------------------------------------------------------------------------
-- plan_cache
--   One row per validated troubleshooting plan. Only validated plans are ever written.
--   catalog_source records which catalog the plan's deeplinks came from: a plan built
--   against synthetic deeplinks is unusable once the official catalog lands and must be
--   purged rather than migrated.
--   cache_version allows mass invalidation when validation rules change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plan_cache (
    id                 BIGSERIAL PRIMARY KEY,
    canonical_query    TEXT NOT NULL,
    original_query     TEXT NOT NULL,
    plan               JSONB NOT NULL,
    query_variations   JSONB NOT NULL DEFAULT '[]'::jsonb,
    score              DOUBLE PRECISION,
    cache_version      INTEGER NOT NULL DEFAULT 1,
    catalog_source     TEXT NOT NULL DEFAULT 'synthetic',
    pipeline_model     TEXT,
    pipeline_cost_usd  NUMERIC(12, 6) NOT NULL DEFAULT 0,
    origin             TEXT NOT NULL DEFAULT 'runtime',
    hit_count          BIGINT NOT NULL DEFAULT 0,
    last_hit_at        TIMESTAMPTZ,
    validated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plan_cache_origin_chk CHECK (origin IN ('prewarm', 'runtime')),
    CONSTRAINT plan_cache_catalog_source_chk
        CHECK (catalog_source IN ('synthetic', 'official')),
    -- score is specified as a confidence between 0.0 and 1.0.
    CONSTRAINT plan_cache_score_chk
        CHECK (score IS NULL OR (score >= 0.0 AND score <= 1.0))
);

CREATE UNIQUE INDEX IF NOT EXISTS plan_cache_canonical_version_uidx
    ON plan_cache (canonical_query, cache_version);
CREATE INDEX IF NOT EXISTS plan_cache_version_idx ON plan_cache (cache_version);
CREATE INDEX IF NOT EXISTS plan_cache_catalog_source_idx ON plan_cache (catalog_source);

-- ---------------------------------------------------------------------------
-- plan_cache_vector
--   One row per searchable phrasing of a cached plan: the canonical query, the original
--   query, and each generated paraphrase. Indexing the paraphrase set rather than a
--   single key is what makes an unseen rewording hit the cache.
--   ON DELETE CASCADE keeps vectors from outliving the plan they describe.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plan_cache_vector (
    id              BIGSERIAL PRIMARY KEY,
    plan_id         BIGINT NOT NULL REFERENCES plan_cache(id) ON DELETE CASCADE,
    variation_text  TEXT NOT NULL,
    variation_kind  TEXT NOT NULL DEFAULT 'paraphrase',
    embedding       vector(__EMBEDDING_DIM__) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plan_cache_vector_kind_chk
        CHECK (variation_kind IN ('canonical', 'original', 'paraphrase'))
);

CREATE INDEX IF NOT EXISTS plan_cache_vector_plan_idx ON plan_cache_vector (plan_id);

-- ---------------------------------------------------------------------------
-- request_metrics
--   Per-request operational record backing the latency percentiles, cache hit rate and
--   cost figures the evaluation asks for. Measured, never estimated.
--   fallback is constrained to the two specified values.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request_metrics (
    id                 BIGSERIAL PRIMARY KEY,
    request_id         UUID NOT NULL,
    query              TEXT NOT NULL,
    cache_hit          BOOLEAN NOT NULL,
    similarity         DOUBLE PRECISION,
    matched_plan_id    BIGINT REFERENCES plan_cache(id) ON DELETE SET NULL,
    pipeline_invoked   BOOLEAN NOT NULL,
    validation_passed  BOOLEAN,
    fallback           TEXT,
    latency_ms         DOUBLE PRECISION NOT NULL,
    embed_ms           DOUBLE PRECISION,
    lookup_ms          DOUBLE PRECISION,
    pipeline_ms        DOUBLE PRECISION,
    cost_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0,
    cache_version      INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT request_metrics_fallback_chk
        CHECK (fallback IS NULL OR fallback IN ('no_match', 'no_siis_context'))
);

CREATE INDEX IF NOT EXISTS request_metrics_created_at_idx
    ON request_metrics (created_at DESC);
CREATE INDEX IF NOT EXISTS request_metrics_cache_hit_idx ON request_metrics (cache_hit);
