"""Start the REST API service.

    python -m scripts.run_api

Exists so the event loop policy is set before uvicorn starts. Running uvicorn directly
also works, but needs the loop selected explicitly:

    uvicorn app.api.main:app --loop asyncio
"""

from __future__ import annotations

import argparse

from app.runtime import use_compatible_event_loop, uvicorn_loop_name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Each worker holds its own copy of the embedding model, so memory scales "
             "with this. Incompatible with --reload.",
    )
    args = parser.parse_args()

    use_compatible_event_loop()

    import uvicorn

    uvicorn.run(
        "app.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        loop=uvicorn_loop_name(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
