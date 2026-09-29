"""Live Phase 2 deeplink candidate inspection test."""

from __future__ import annotations

from dotenv import load_dotenv

from app.config import get_settings
from app.db.session import connect
from app.embeddings.encoder import get_encoder
from app.retrieval.resolver import (
    load_bm25_index,
    search_catalog,
)

load_dotenv(".env")


def test_live_deeplink_candidates():
    """
    Inspect the top Phase 2 catalog candidates.

    This test intentionally does NOT assume that rank 1 is correct.

    We want to inspect retrieval quality before adding candidate
    verification/reranking to the production pipeline.
    """

    settings = get_settings()
    encoder = get_encoder()

    test_intents = [
        "Wi-Fi Settings Connections Wi-Fi",
        "Email App Storage Settings Apps email app Storage",
        "Safe Mode Power menu Safe mode",
    ]

    with connect(settings) as conn:
        bm25 = load_bm25_index(conn)

        print()
        print("=" * 100)
        print("PHASE 2 — TOP 10 DEEPLINK CANDIDATES")
        print("=" * 100)

        for intent in test_intents:
            print()
            print("=" * 100)
            print(f"INTENT: {intent}")
            print("=" * 100)

            matches = search_catalog(
                conn,
                intent,
                top_k=10,
                bm25_index=bm25,
                encoder=encoder,
                settings=settings,
            )

            if not matches:
                print("NO CANDIDATES FOUND")
                continue

            for rank, match in enumerate(
                matches,
                start=1,
            ):
                print()
                print(f"RANK {rank}")
                print("-" * 100)

                print(
                    f"Catalog ID:     "
                    f"{match.catalog_id}"
                )

                print(
                    f"Deeplink:       "
                    f"{match.deeplink}"
                )

                print(
                    f"Description:    "
                    f"{match.description}"
                )

                print(
                    f"Message:        "
                    f"{match.message}"
                )

                print(
                    f"QnA:            "
                    f"{match.qna_description}"
                )

                print(
                    f"Original type:  "
                    f"{match.original_type}"
                )

                print(
                    f"RRF score:      "
                    f"{match.rrf_score}"
                )

                print(
                    f"Cosine:         "
                    f"{match.cosine_similarity}"
                )

                print(
                    f"Vector rank:    "
                    f"{match.vector_rank}"
                )

                print(
                    f"BM25 rank:      "
                    f"{match.bm25_rank}"
                )

                if match.validation_deeplink:
                    print(
                        f"Validation:     "
                        f"{match.validation_deeplink}"
                    )

            # Retrieval itself must at least produce candidates.
            assert len(matches) > 0

            # Every returned candidate must come from the
            # catalog and therefore have a deeplink.
            for match in matches:
                assert match.deeplink
                assert match.deeplink.strip()

        print()
        print("=" * 100)
        print("PHASE 2 CANDIDATE INSPECTION COMPLETED")
        print("=" * 100)
