"""Application settings, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL ---
    # Defaults mirror the compose fallbacks so the app works before a .env is written.
    postgres_user: str = "troubleshoot_user"
    postgres_password: str = "troubleshoot_password"
    postgres_db: str = "troubleshooting"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    # --- Embedding model ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # pgvector can store up to 16000 dimensions but only indexes `vector` columns with
    # HNSW up to 2000, so anything larger would silently lose the index.
    embedding_dim: int = Field(default=384, ge=1, le=2000)
    # BGE v1.5 ships a retrieval instruction, but fastembed does not apply it for v1.5
    # (its model card calls prefixes "not so necessary"). Left empty by default; set it to
    # "Represent this sentence for searching relevant passages: " to A/B the effect on
    # catalog retrieval. It is only ever applied to the query side of catalog search,
    # never to cache lookup, which compares query against query.
    catalog_query_prefix: str = ""

    # --- HNSW ---
    # pgvector defaults: m=16, ef_construction=64, ef_search=40.
    hnsw_m: int = Field(default=16, ge=2, le=100)
    hnsw_ef_construction: int = Field(default=64, ge=4, le=1000)
    # Catalog search is quality-critical: a missed neighbour means a worse deeplink, so it
    # runs with a larger candidate list than the pgvector default.
    hnsw_ef_search_catalog: int = Field(default=100, ge=1, le=1000)
    # Cache lookup is failure-safe: a missed neighbour is just a cache miss, which falls
    # through to the pipeline, so the default candidate list is sufficient.
    hnsw_ef_search_cache: int = Field(default=40, ge=1, le=1000)

    # --- Retrieval ---
    catalog_top_k: int = Field(default=5, ge=1, le=100)
    # Reciprocal Rank Fusion constant. 60 is the value from the original RRF paper.
    rrf_k: int = Field(default=60, ge=1)
    # Unweighted RRF treats both retrievers as equally authoritative, which produces
    # exact ties whenever they rank the same two candidates in opposite order. Dense
    # search is weighted higher because colloquial paraphrase is the primary problem
    # here and BM25 acts as a terminology correction rather than a co-equal vote.
    # These defaults are provisional and must be re-validated on the real catalog.
    rrf_dense_weight: float = Field(default=1.0, ge=0.0)
    rrf_sparse_weight: float = Field(default=0.8, ge=0.0)

    # --- Semantic cache ---
    # Measured on bge-small-en-v1.5 against the synthetic fixtures, using held-out
    # paraphrases as positives and cross-domain query pairs as negatives:
    #
    #   held-out paraphrases   min 0.6909  mean 0.8056  max 0.8631
    #   cross-domain pairs     max 0.7119  mean 0.5781   (n = 96)
    #
    #   threshold   paraphrase recall   cross-domain false positives
    #      0.75            83%                     0
    #      0.78            83%                     0
    #      0.80            67%                     0
    #      0.85            33%                     0
    #
    # 0.78 is chosen as the highest value that still clears the required 80% hit rate on
    # unseen paraphrases while admitting no cross-domain false positive. 0.85, the usual
    # off-the-shelf default, would have retained only a third of genuine paraphrases.
    #
    # Same-domain pairs score up to 0.8044 and are NOT counted as false positives: two
    # phrasings of the same underlying battery problem sharing a plan is the intended
    # behaviour, not an error.
    #
    # These figures come from synthetic fixtures and must be re-measured on the real
    # query set before any of them is quoted as a result.
    cache_similarity_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    cache_version: int = Field(default=1, ge=1)

    # --- Data sources ---
    catalog_path: str = "data/fixtures_synthetic/deeplinks.json"
    queries_path: str = "data/fixtures_synthetic/queries.json"

    @property
    def database_url(self) -> str:
        """libpq connection URL. Credentials are percent-encoded."""
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def safe_database_url(self) -> str:
        """Same URL with the password masked, for logs and error messages."""
        user = quote_plus(self.postgres_user)
        return (
            f"postgresql://{user}:***"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
