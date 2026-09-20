"""Shared fixtures.

Database tests skip rather than fail when PostgreSQL is unreachable, so that a teammate
working on another phase is never blocked by a container they do not have running.
"""

from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def db_connection(settings):
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"PostgreSQL not reachable at {settings.safe_database_url} ({exc}). "
            "Start it with: docker compose up -d"
        )
    with conn:
        yield conn
