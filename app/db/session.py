"""Synchronous database access for migrations, indexing scripts and tests.

The async pool used by the API service is introduced alongside FastAPI, so that it can be
opened and closed from the application lifespan rather than at import time.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import Settings, get_settings


@contextmanager
def connect(
    settings: Settings | None = None,
    *,
    register_vector_types: bool = True,
) -> Iterator[psycopg.Connection]:
    """Open a single connection.

    register_vector_types adapts pgvector values to and from Python. It needs the vector
    extension to already exist, so migrations open the connection with it disabled.
    """
    settings = settings or get_settings()
    with psycopg.connect(settings.database_url) as conn:
        if register_vector_types:
            from pgvector.psycopg import register_vector

            register_vector(conn)
        yield conn


def extension_version(conn: psycopg.Connection) -> str | None:
    """Installed pgvector version, or None when the extension is absent."""
    row = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    return row[0] if row else None
