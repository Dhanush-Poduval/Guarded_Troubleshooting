import asyncio
from app.config import get_settings
from app.bootstrap import build_pool, build_service

QUERY = """My Samsung A115G tablet screen flashes and then goes completely blank whenever I tap to open an email in Gmail, and after it works for a short time it goes blank again."""

async def main():
    settings = get_settings()
    pool = build_pool(settings)
    await pool.open()

    try:
        service = build_service(pool, settings)

        result = await service.troubleshoot(QUERY)

        print("\n=== CACHE RESULT ===")
        print("Cache hit:       ", result.cache_hit)
        print("Pipeline invoked:", result.pipeline_invoked)
        print("Similarity:      ", result.similarity)
        print("Runner-up:       ", result.runner_up_similarity)
        print("Reject reason:   ", result.cache_reject_reason)
        print("Latency:         ", result.latency_ms, "ms")
        print("Embedding:       ", result.embed_ms, "ms")
        print("Lookup:          ", result.lookup_ms, "ms")
        print("Pipeline time:   ", result.pipeline_ms)

    finally:
        await pool.close()

asyncio.run(main())
