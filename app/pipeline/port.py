"""The Phase 0-2 boundary.

This is the single seam between the cache and API service (Phase 3 and 4) and the query
enrichment, structure extraction and deeplink sequencing pipeline (Phase 0 to 2).

The contract is deliberately narrow: one call in, one structured result out. Everything on
this side of the seam treats the pipeline as an expensive, fallible black box that may
take seconds and may cost money. Nothing here depends on how the pipeline is implemented,
so replacing the mock with the real implementation is a single binding change in
app.service, with no edits anywhere else.

The method is async because the real implementation will be network-bound on an LLM call.
Making it async now avoids threading an 8 second blocking call through the request path
later.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.contract.schema import ContextDeeplinkResponse


class PipelineRequest(BaseModel):
    """A raw customer complaint, optionally with reference text."""

    query: str
    # When omitted, the engine is expected to answer from pre-warmed cache entries rather
    # than run extraction, because there is no reference text to extract from.
    siis_response: str | None = None


class PipelineMeta(BaseModel):
    """Cost and provenance for one pipeline invocation."""

    model: str | None = None
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class PipelineResult(BaseModel):
    """Everything Phase 0-2 produces for one query.

    canonical_query is the normalised form used as the semantic cache key. query_variations
    are the 8 to 10 paraphrases; they are not decoration, they become the searchable
    phrasings of the cached plan.
    """

    canonical_query: str
    query_variations: list[str] = Field(default_factory=list)
    response: ContextDeeplinkResponse
    meta: PipelineMeta = Field(default_factory=PipelineMeta)
    # Set when the pipeline could not produce a plan, using one of the specified reasons.
    fallback: str | None = None


@runtime_checkable
class PipelinePort(Protocol):
    """Implemented by the real Phase 0-2 pipeline, and by the temporary mock."""

    @property
    def name(self) -> str:
        """Short identifier recorded in metrics, e.g. "mock" or a model id."""
        ...

    async def run(self, request: PipelineRequest) -> PipelineResult:
        ...
