"""Deeplink resolution: dense vector search fused with BM25 keyword search.

This is the seam Phase 2 calls. Given the descriptive text of a target screen, it returns
the catalog entry that screen corresponds to, or None.

Two retrievers are combined because they fail differently. Dense search handles
paraphrase and synonym ("dies fast" against "battery drain") but can drift to a
topically-near but wrong screen. BM25 handles exact terminology ("safe mode", "QHD")
that a small embedding model may blur. Their scores live on incompatible scales, so they
are merged by Reciprocal Rank Fusion, which consumes ranks and ignores magnitudes.

Determinism: the evaluation requires identical plans for identical inputs, while HNSW is
approximate. Every ordering here therefore carries an explicit final tie-break on the
catalog id, so equal scores can never reorder between two runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import psycopg

from app.config import Settings, get_settings
from app.db.indexes import set_ef_search
from app.embeddings.encoder import Encoder, get_encoder

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")

# Function words are stripped before BM25. On a catalog of a few hundred entries this is
# not cosmetic: a stopword that happens to appear in only two documents receives a very
# high IDF, so a query sharing nothing but "when" can score above a genuine topical match
# and then outrank it through fusion. Measured on the fixture catalog, this promoted
# "always on display" over "video stabilisation" for a query about shaky video.
#
# Deliberately excluded from this list: on, off, up, down, out, back, all. They look like
# stopwords but carry meaning in a device-settings catalog ("always on display",
# "turn off", "back gestures").
_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does doing for from
    get got had has have having how i if in into is it its just keep keeps me my
    of or should so some than that the their then there these they this those to
    very was were what when where which while who why will with would you your
    """.split()
)

_STEMMER = None


def _stemmer():
    global _STEMMER
    if _STEMMER is None:
        from py_rust_stemmers import SnowballStemmer

        _STEMMER = SnowballStemmer("english")
    return _STEMMER


def tokenize(text: str) -> list[str]:
    """Lowercase, drop function words, then stem.

    Stemming matters here because user phrasing and catalog phrasing differ in inflection:
    "videos"/"video" and "record"/"recording" are the same concept to a user but distinct
    tokens to BM25.
    """
    tokens = [tok for tok in _TOKEN.findall(text.lower()) if tok not in _STOPWORDS]
    if not tokens:
        return []
    return _stemmer().stem_words(tokens)


@dataclass(frozen=True)
class CatalogMatch:
    catalog_id: int
    deeplink: str
    description: str
    message: str
    qna_description: str
    original_type: str | None
    cosine_similarity: float | None
    vector_rank: int | None
    bm25_rank: int | None
    rrf_score: float


@dataclass
class BM25Index:
    """In-process BM25 over the catalog.

    The catalog is a few hundred rows, so an in-memory index is exact and sub-millisecond.
    A Postgres BM25 extension would mean leaving the stock pgvector image for no gain at
    this size; Postgres' built-in ts_rank is not BM25.
    """

    ids: list[int]
    corpus_tokens: list[list[str]]
    _bm25: object = None

    @classmethod
    def build(cls, rows: list[tuple[int, str]]) -> "BM25Index":
        from rank_bm25 import BM25Okapi

        ids = [row[0] for row in rows]
        corpus = [tokenize(row[1]) for row in rows]
        index = cls(ids=ids, corpus_tokens=corpus)
        index._bm25 = BM25Okapi(corpus) if corpus else None
        return index

    def ranked(self, query: str, top_k: int) -> list[tuple[int, float]]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        scored = [
            (cid, float(score))
            for cid, score in zip(self.ids, scores)
            if score > 0.0
        ]
        # Descending score, ascending id as the deterministic tie-break.
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]


def load_bm25_index(conn: psycopg.Connection) -> BM25Index:
    rows = conn.execute(
        "SELECT id, match_text FROM deeplink_catalog ORDER BY id"
    ).fetchall()
    return BM25Index.build([(row[0], row[1]) for row in rows])


def _vector_ranked(
    conn: psycopg.Connection,
    query_vector,
    top_k: int,
    settings: Settings,
) -> list[tuple[int, float]]:
    """Returns (catalog_id, cosine_similarity), best first."""
    with conn.transaction():
        set_ef_search(conn, settings.hnsw_ef_search_catalog)
        rows = conn.execute(
            """
            SELECT id, 1 - (embedding <=> %s) AS similarity
            FROM deeplink_catalog
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s, id
            LIMIT %s
            """,
            (query_vector, query_vector, top_k),
        ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def search_catalog(
    conn: psycopg.Connection,
    intent_text: str,
    *,
    top_k: int | None = None,
    bm25_index: BM25Index | None = None,
    encoder: Encoder | None = None,
    settings: Settings | None = None,
) -> list[CatalogMatch]:
    """Hybrid search over the catalog. Returns matches best-first."""
    settings = settings or get_settings()
    encoder = encoder or get_encoder()
    top_k = top_k or settings.catalog_top_k

    # Over-fetch from each retriever so fusion has something to fuse.
    fetch = max(top_k * 4, 20)

    query_vector = encoder.encode_catalog_query(intent_text)
    dense = _vector_ranked(conn, query_vector, fetch, settings)

    bm25_index = bm25_index or load_bm25_index(conn)
    sparse = bm25_index.ranked(intent_text, fetch)

    dense_rank = {cid: i + 1 for i, (cid, _) in enumerate(dense)}
    sparse_rank = {cid: i + 1 for i, (cid, _) in enumerate(sparse)}
    similarity = {cid: score for cid, score in dense}

    k = settings.rrf_k
    fused: dict[int, float] = {}
    for cid, rank in dense_rank.items():
        fused[cid] = fused.get(cid, 0.0) + settings.rrf_dense_weight / (k + rank)
    for cid, rank in sparse_rank.items():
        fused[cid] = fused.get(cid, 0.0) + settings.rrf_sparse_weight / (k + rank)

    if not fused:
        return []

    # Descending fused score, ascending id: deterministic under equal scores.
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))[:top_k]
    ids = [cid for cid, _ in ordered]

    rows = conn.execute(
        """
        SELECT id, deeplink, description, message, qna_description, original_type
        FROM deeplink_catalog WHERE id = ANY(%s)
        """,
        (ids,),
    ).fetchall()
    by_id = {row[0]: row for row in rows}

    matches = []
    for cid, score in ordered:
        row = by_id.get(cid)
        if row is None:
            continue
        matches.append(
            CatalogMatch(
                catalog_id=row[0],
                deeplink=row[1],
                description=row[2],
                message=row[3],
                qna_description=row[4],
                original_type=row[5],
                cosine_similarity=similarity.get(cid),
                vector_rank=dense_rank.get(cid),
                bm25_rank=sparse_rank.get(cid),
                rrf_score=score,
            )
        )
    return matches


def resolve_deeplink(
    conn: psycopg.Connection,
    intent_text: str,
    *,
    bm25_index: BM25Index | None = None,
    encoder: Encoder | None = None,
    settings: Settings | None = None,
) -> CatalogMatch | None:
    """The stable entry point for Phase 2. Returns the best catalog match, or None.

    Returning None is a valid and expected outcome: a step with no matching screen must
    fall back rather than receive an invented URI.
    """
    matches = search_catalog(
        conn,
        intent_text,
        top_k=1,
        bm25_index=bm25_index,
        encoder=encoder,
        settings=settings,
    )
    return matches[0] if matches else None
