"""Create the schema in a running PostgreSQL instance.

    docker compose up -d
    python -m scripts.init_db

Safe to run repeatedly: already-applied migrations are skipped.
"""

from __future__ import annotations

import sys

import psycopg

from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.session import connect, extension_version


def main() -> int:
    settings = get_settings()
    print(f"Connecting to {settings.safe_database_url}")

    try:
        # Vector adapters are registered only after the extension is guaranteed present.
        with connect(settings, register_vector_types=False) as conn:
            applied = run_migrations(conn, settings)
            version = extension_version(conn)
    except psycopg.OperationalError as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        print("Is the database running? Try: docker compose up -d", file=sys.stderr)
        return 1

    print(f"pgvector extension version: {version}")
    if applied:
        for name in applied:
            print(f"  applied {name}")
    else:
        print("  no pending migrations")
    print(f"Vector columns sized at {settings.embedding_dim} dimensions "
          f"for {settings.embedding_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
