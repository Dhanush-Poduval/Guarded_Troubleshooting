"""Process level runtime setup.

psycopg's async mode cannot run on the ProactorEventLoop, which is the default asyncio
event loop on Windows. Without this, every pooled connection attempt fails with
"Psycopg cannot use the 'ProactorEventLoop' to run in async mode" and the pool eventually
raises PoolTimeout, which looks like a database outage rather than a loop mismatch.

Any process that opens the async pool must call use_compatible_event_loop() before the
loop starts: scripts, the test suite, and the API entrypoint.

On Linux and macOS this is a no-op, so it costs nothing in the environment the service is
likely to be evaluated in. On Windows the selector loop caps out around 512 sockets and
cannot spawn subprocesses; neither limit affects this service, which holds a handful of
pooled connections.
"""

from __future__ import annotations

import asyncio
import sys


def use_compatible_event_loop() -> None:
    """Select an event loop policy that psycopg's async mode supports."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def uvicorn_loop_name() -> str:
    """The --loop value uvicorn should use.

    uvicorn would otherwise install its own Proactor-based loop on Windows, re-breaking
    psycopg even though the policy above was set.
    """
    return "asyncio"
