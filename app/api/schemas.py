"""Request and response models for the REST API.

The response envelope is a superset of the official sample_output.json. `query` and
`response` appear exactly as that file shows them, so a consumer reading those fields sees
no difference, and `query_variations` and `meta` are added because the evaluation asks for
cache-hit and latency evidence which the sample has nowhere to put.

Responses are pure JSON. No markdown fences and no conversational preamble ever appear,
which is a contract requirement rather than a style preference.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.contract.schema import ContextDeeplinkResponse


class TroubleshootRequest(BaseModel):
    query: str = Field(min_length=1, description="The raw customer complaint.")
    # The written contract shows a raw string, but siis_responses.json carries an object
    # of {title, content}. Both are accepted and normalised, so a client following either
    # the document or the dataset works without translation.
    siis_response: str | dict[str, Any] | None = None

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query must not be blank")
        return cleaned

    def siis_text(self) -> str | None:
        """Flatten siis_response to the single string the pipeline consumes."""
        if self.siis_response is None:
            return None
        if isinstance(self.siis_response, str):
            return self.siis_response or None
        title = str(self.siis_response.get("title", "") or "")
        content = str(self.siis_response.get("content", "") or "")
        if title and content:
            return f"{title}\n\n{content}"
        return content or title or None


class ResponseMeta(BaseModel):
    """Operational metadata for one request. Every value is measured, never estimated."""

    latency_ms: float
    cache_hit: bool
    model: str | None = None
    cost_usd: float = 0.0

    # Beyond the four fields the worked example shows.
    pipeline_used: bool
    request_id: str
    cache_version: int
    similarity: float | None = None
    embed_ms: float | None = None
    lookup_ms: float | None = None
    pipeline_ms: float | None = None
    # One of the specified reasons when no plan could be produced.
    fallback: str | None = None
    # Why the cache declined to answer, when it declined.
    cache_reject_reason: str | None = None


class TroubleshootResponse(BaseModel):
    query: str
    query_variations: list[str] = Field(default_factory=list)
    response: ContextDeeplinkResponse
    meta: ResponseMeta


class HealthResponse(BaseModel):
    """200 only when the caching layer, the model and the vector indexes are all ready.

    A liveness stub that always returns ok would satisfy the letter of the contract and
    none of its purpose, since a warm process with an unbuilt index serves nothing.
    """

    status: str
    database: bool
    embedding_model: bool
    vector_indexes: bool
    catalog_indexed: bool
    cache_ready: bool
    detail: str | None = None


class CacheStatsResponse(BaseModel):
    """Measured cache aggregates. Mirrors the columns behind them rather than
    recomputing anything in the handler."""

    cache_version: int
    similarity_threshold: float
    ambiguity_margin: float
    cached_plans: int
    cached_vectors: int
    prewarmed_plans: int
    plans_from_synthetic_catalog: int
    lifetime_plan_hits: int
    requests_total: int
    requests_cache_hit: int
    hit_rate: float | None = None
    hit_latency_p50_ms: float | None = None
    hit_latency_p95_ms: float | None = None
    miss_latency_p50_ms: float | None = None
    miss_latency_p95_ms: float | None = None
    total_cost_usd: float = 0.0
    rejected_below_threshold: int = 0
    rejected_ambiguous: int = 0
    rejected_failed_revalidation: int = 0
    pool: dict[str, Any] | None = None
