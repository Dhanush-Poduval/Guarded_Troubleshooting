"""Shared fixtures.

Database tests skip rather than fail when PostgreSQL is unreachable, so that a teammate
working on another phase is never blocked by a container they do not have running.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.runtime import use_compatible_event_loop

# psycopg's async mode cannot run on the Windows default event loop, so the policy is set
# before pytest-asyncio creates any loop. No-op off Windows.
use_compatible_event_loop()


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
    # Register pgvector adapters here rather than leaving it to whichever test happens to
    # do it first. Without them a numpy array binds as double precision[] and every vector
    # query fails with "cannot adapt type 'ndarray'". Relying on collection order meant
    # tests/test_retrieval.py passed only when tests/test_database_setup.py ran before it.
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception as exc:  # noqa: BLE001
        conn.close()
        pytest.skip(f"pgvector extension not available ({exc}); run scripts.init_db")

    with conn:
        yield conn


# Cache versions at or above this floor belong to the test suite. Production runs at
# cache_version 1, so this range is safe to wipe.
TEST_CACHE_VERSION_FLOOR = 1000


def _purge_test_versions(conn) -> None:
    with conn.transaction():
        conn.execute(
            "DELETE FROM request_metrics WHERE cache_version >= %s",
            (TEST_CACHE_VERSION_FLOOR,),
        )
        # plan_cache_vector cascades from plan_cache.
        conn.execute(
            "DELETE FROM plan_cache WHERE cache_version >= %s",
            (TEST_CACHE_VERSION_FLOOR,),
        )


@pytest.fixture(scope="session", autouse=True)
def clean_test_cache_versions(db_connection):
    """Wipe the test cache-version range before and after the session.

    Tests isolate themselves by taking a fresh cache_version from a counter, but that
    counter restarts at the floor on every run. Without this, a second run reuses versions
    that still hold rows from the first, and every test expecting a cache miss silently
    gets a hit instead. That failure mode passes on a clean database and fails on a rerun,
    which is the worst way for it to behave.
    """
    _purge_test_versions(db_connection)
    yield
    _purge_test_versions(db_connection)

