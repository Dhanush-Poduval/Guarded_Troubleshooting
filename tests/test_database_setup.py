"""Checkpoint 2 verification: connectivity, pgvector, schema, and a vector round-trip.

Covers items 1-3 of the test plan. HNSW index checks arrive with the Phase 3 indexing step,
because the index is built after the catalog rows are loaded.
"""

from __future__ import annotations

import pytest

from app.db.session import extension_version

EXPECTED_TABLES = {
    "schema_migrations",
    "deeplink_catalog",
    "plan_cache",
    "plan_cache_vector",
    "request_metrics",
}


def test_connection_is_live(db_connection):
    assert db_connection.execute("SELECT 1").fetchone()[0] == 1


def test_pgvector_extension_installed(db_connection):
    version = extension_version(db_connection)
    assert version is not None, (
        "vector extension missing. Run: python -m scripts.init_db"
    )
    print(f"\npgvector version: {version}")


def test_core_tables_exist(db_connection):
    rows = db_connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    present = {row[0] for row in rows}
    missing = EXPECTED_TABLES - present
    assert not missing, f"missing tables: {sorted(missing)}. Run: python -m scripts.init_db"


def test_vector_columns_match_configured_dimension(db_connection, settings):
    """A dimension mismatch between config and schema would fail only at insert time."""
    rows = db_connection.execute(
        """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE n.nspname = 'public' AND t.typname = 'vector' AND a.attnum > 0
        """
    ).fetchall()
    assert rows, "no vector columns found"
    expected = f"vector({settings.embedding_dim})"
    for table, column, declared in rows:
        assert declared == expected, (
            f"{table}.{column} is {declared}, expected {expected}. "
            "EMBEDDING_DIM was changed after the migration ran."
        )


def test_vector_insert_and_cosine_distance(db_connection):
    """Round-trips vectors through a temp table so no test data reaches real tables.

    Covers both ways a query vector can be bound. Inside an operator expression there is
    no column to infer the type from, so a bare Python list is sent as double precision[]
    and `<=>` does not resolve. Either a numpy array (adapted by register_vector) or an
    explicit ::vector cast is required. Phase 3 passes numpy arrays, since that is what
    the embedding model returns.
    """
    pytest.importorskip("pgvector")
    import numpy as np
    from pgvector.psycopg import register_vector

    register_vector(db_connection)

    with db_connection.transaction():
        db_connection.execute("CREATE TEMP TABLE t (id int, embedding vector(3))")
        db_connection.execute(
            "INSERT INTO t (id, embedding) VALUES (%s, %s), (%s, %s)",
            (1, [1.0, 0.0, 0.0], 2, [0.0, 1.0, 0.0]),
        )

        probe = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        rows = db_connection.execute(
            "SELECT id, embedding <=> %s AS distance FROM t ORDER BY distance, id",
            (probe,),
        ).fetchall()

        assert [row[0] for row in rows] == [1, 2]
        assert rows[0][1] == pytest.approx(0.0, abs=1e-6)
        assert rows[1][1] == pytest.approx(1.0, abs=1e-6)

        # An explicit cast makes a plain list work too.
        cast_rows = db_connection.execute(
            "SELECT id, embedding <=> %s::vector AS distance FROM t ORDER BY distance, id",
            ([1.0, 0.0, 0.0],),
        ).fetchall()
        assert [row[0] for row in cast_rows] == [1, 2]


def test_bare_list_in_operator_expression_is_not_a_vector(db_connection):
    """Guards the footgun above: without a cast or numpy, `<=>` fails rather than
    silently returning wrong distances."""
    pytest.importorskip("pgvector")
    import psycopg
    from pgvector.psycopg import register_vector

    register_vector(db_connection)

    with pytest.raises(psycopg.errors.UndefinedFunction):
        with db_connection.transaction():
            db_connection.execute("CREATE TEMP TABLE t2 (id int, embedding vector(3))")
            db_connection.execute(
                "SELECT embedding <=> %s FROM t2", ([1.0, 0.0, 0.0],)
            ).fetchall()


def test_score_constraint_rejects_out_of_range(db_connection):
    """score must stay within 0.0-1.0; the check constraint is the last line of defence."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with db_connection.transaction():
            db_connection.execute(
                """
                INSERT INTO plan_cache
                    (canonical_query, original_query, plan, score)
                VALUES (%s, %s, %s::jsonb, %s)
                """,
                ("__constraint_probe__", "__constraint_probe__", '{"contexts": []}', 1.5),
            )


def test_fallback_constraint_rejects_unknown_value(db_connection):
    """Only the two specified fallback values are storable."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with db_connection.transaction():
            db_connection.execute(
                """
                INSERT INTO request_metrics
                    (request_id, query, cache_hit, pipeline_invoked, latency_ms, fallback)
                VALUES (gen_random_uuid(), %s, false, true, 1.0, %s)
                """,
                ("__constraint_probe__", "not_a_real_fallback"),
            )
