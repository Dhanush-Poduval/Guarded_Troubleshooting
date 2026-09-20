"""Applies the SQL files in db/migrations in filename order.

Every migration is written to be idempotent, and applied filenames are recorded in
schema_migrations so a re-run is a no-op and reports what it skipped.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from app.config import Settings, get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

# Placeholder token rather than str.format, so that SQL containing braces stays safe.
_DIM_TOKEN = "__EMBEDDING_DIM__"


def _render(sql: str, embedding_dim: int) -> str:
    # Coerced to int at the call site; embedding_dim is never interpolated as free text.
    return sql.replace(_DIM_TOKEN, str(embedding_dim))


def applied_migrations(conn: psycopg.Connection) -> set[str]:
    exists = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    if not exists or exists[0] is None:
        return set()
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def run_migrations(
    conn: psycopg.Connection,
    settings: Settings | None = None,
) -> list[str]:
    """Apply any pending migrations. Returns the filenames that were applied."""
    settings = settings or get_settings()
    dim = int(settings.embedding_dim)

    already = applied_migrations(conn)
    newly_applied: list[str] = []

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        sql = _render(path.read_text(encoding="utf-8"), dim)
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT (filename) DO NOTHING",
                (path.name,),
            )
        newly_applied.append(path.name)

    return newly_applied
