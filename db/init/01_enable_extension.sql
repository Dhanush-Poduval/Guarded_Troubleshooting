-- Runs once, on first container start against an empty data volume.
-- The migration runner repeats this statement so that databases created before this
-- file existed are also handled.
CREATE EXTENSION IF NOT EXISTS vector;
