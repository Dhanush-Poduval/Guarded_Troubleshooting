"""Checkpoint 3 verification: embeddings, HNSW indexing and hybrid deeplink retrieval.

Covers items 4-6 of the test plan. Assertions are written against measured behaviour
rather than assumed behaviour, and anything that depends on the specific content of the
synthetic fixtures is marked so it can be revisited when the real catalog arrives.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.catalog.loader import CatalogEntry, load_catalog
from app.db.indexes import CACHE_INDEX, CATALOG_INDEX, index_definition, index_exists
from app.retrieval.resolver import (
    load_bm25_index,
    resolve_deeplink,
    search_catalog,
    tokenize,
)

OFFICIAL_CATALOG = pathlib.Path("data/official/deeplinks.json")
OFFICIAL_QUERIES = pathlib.Path("data/official/siis_responses.json")


# --------------------------------------------------------------------------
# Catalog loading
# --------------------------------------------------------------------------

def test_match_text_never_contains_the_uri():
    """Matching must run on descriptive metadata, never the masked URI, which is an
    obfuscated token carrying no meaning."""
    entry = CatalogEntry(
        deeplink="bixby://masked/act/aa73a35e8d",
        description="Open battery settings",
        message="View usage",
        qna_description="Shows battery level",
        original_type=None,
    )
    assert "bixby" not in entry.match_text
    assert "aa73a35e8d" not in entry.match_text
    assert entry.match_text == "Open battery settings View usage Shows battery level"


def test_original_type_is_expanded_into_words_not_kept_as_a_token():
    """offURL and onURL distinguish turning a setting off from on, which a step like
    "turn off adaptive brightness" depends on. The raw token is not English, so it is
    expanded rather than embedded verbatim."""
    off = CatalogEntry("d", "Adaptive brightness", "Toggle", "", "offURL")
    on = CatalogEntry("d", "Adaptive brightness", "Toggle", "", "onURL")
    assert "offURL" not in off.match_text
    assert "off" in off.match_text.lower()
    assert "on" in on.match_text.lower()
    assert off.match_text != on.match_text


def test_official_catalog_loads_and_is_flagged_official():
    if not OFFICIAL_CATALOG.exists():
        pytest.skip("official catalog not present")
    catalog = load_catalog(OFFICIAL_CATALOG)
    assert catalog.source == "official"
    assert catalog.is_synthetic is False
    assert len(catalog.entries) == 578


def test_official_catalog_validation_blocks_are_parsed():
    """Most entries carry a validation deeplink; a subset carries the full comparison."""
    if not OFFICIAL_CATALOG.exists():
        pytest.skip("official catalog not present")
    catalog = load_catalog(OFFICIAL_CATALOG)
    with_validation = [e for e in catalog.entries if e.validation]
    complete = [e for e in with_validation if e.validation.is_complete]
    assert len(with_validation) == 570
    assert len(complete) == 138
    sample = complete[0].validation
    assert sample.deeplink.startswith("bixby://masked/val/")
    assert sample.result_type in {"boolean", "integer", "str", "float"}
    assert sample.condition in {"greater", "equal", "less"}


def test_dummy_positive_is_an_actual_catalog_entry():
    """The reserved placeholder ships in the catalog rather than being special-cased."""
    if not OFFICIAL_CATALOG.exists():
        pytest.skip("official catalog not present")
    catalog = load_catalog(OFFICIAL_CATALOG)
    assert "bixby://dummy_positive" in catalog.deeplink_set()


def test_loader_rejects_duplicate_uris(tmp_path):
    path = tmp_path / "dupes.json"
    entry = {"deeplink": "bixby://masked/act/1", "description": "x"}
    path.write_text(json.dumps({"deeplinks": [entry, dict(entry)]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(path)


def test_loader_rejects_empty_catalog(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"deeplinks": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no deeplinks"):
        load_catalog(path)


def test_bare_list_catalog_is_treated_as_official(tmp_path):
    """The official file may be a plain array with no marker object."""
    path = tmp_path / "official.json"
    path.write_text(
        json.dumps([{"deeplink": "bixby://masked/act/1", "description": "x"}]),
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    assert catalog.source == "official"
    assert catalog.is_synthetic is False


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

def test_tokenizer_drops_function_words():
    """A stopword appearing in few documents earns a high IDF on a small catalog and can
    outrank a genuine topical match through fusion."""
    assert "when" not in tokenize("Videos come out shaky when I record")
    assert "the" not in tokenize("Restart the device")


def test_tokenizer_stems_inflections():
    assert tokenize("videos")[0] == tokenize("video")[0]
    assert tokenize("recording")[0] == tokenize("record")[0]


def test_tokenizer_keeps_device_words_that_look_like_stopwords():
    """on/off/out carry meaning in a settings catalog and must survive."""
    for word in ("on", "off", "out", "down"):
        assert tokenize(f"turn {word} display") != tokenize("turn display")


def test_tokenizer_handles_all_stopword_input():
    assert tokenize("the and of") == []


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def encoder():
    pytest.importorskip("fastembed")
    from app.embeddings.encoder import get_encoder

    return get_encoder()


def test_embedding_dimension_matches_settings(encoder, settings):
    vector = encoder.encode_one_for_cache("battery drains fast")
    assert vector.shape == (settings.embedding_dim,)


def test_embeddings_are_l2_normalised(encoder):
    """Normalisation is what lets cosine similarity be read as 1 - cosine distance."""
    import numpy as np

    vector = encoder.encode_one_for_cache("battery drains fast")
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-4)


def test_embedding_is_deterministic(encoder):
    """Identical input must give identical output, or cached plans drift."""
    import numpy as np

    a = encoder.encode_one_for_cache("battery drains fast")
    b = encoder.encode_one_for_cache("battery drains fast")
    assert np.array_equal(a, b)


def test_paraphrase_scores_above_unrelated(encoder):
    """Direction-only check, deliberately not asserting a specific threshold."""
    a = encoder.encode_one_for_cache("My battery is draining very quickly")
    b = encoder.encode_one_for_cache("My phone battery dies really fast")
    c = encoder.encode_one_for_cache("How do I change the camera resolution")
    assert float(a @ b) > float(a @ c)


# --------------------------------------------------------------------------
# HNSW
# --------------------------------------------------------------------------

def test_hnsw_indexes_exist(db_connection):
    for name in (CATALOG_INDEX, CACHE_INDEX):
        assert index_exists(db_connection, name), (
            f"{name} missing. Run: python -m scripts.build_catalog"
        )


def test_hnsw_uses_cosine_and_configured_parameters(db_connection, settings):
    definition = index_definition(db_connection, CATALOG_INDEX)
    assert definition is not None
    assert "hnsw" in definition
    assert "vector_cosine_ops" in definition
    assert f"m='{settings.hnsw_m}'" in definition
    assert f"ef_construction='{settings.hnsw_ef_construction}'" in definition


def test_catalog_is_populated(db_connection):
    count = db_connection.execute("SELECT count(*) FROM deeplink_catalog").fetchone()[0]
    assert count > 0, "catalog empty. Run: python -m scripts.build_catalog"


def test_every_catalog_row_has_an_embedding(db_connection):
    missing = db_connection.execute(
        "SELECT count(*) FROM deeplink_catalog WHERE embedding IS NULL"
    ).fetchone()[0]
    assert missing == 0


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bm25(db_connection):
    return load_bm25_index(db_connection)


def test_resolve_returns_a_real_catalog_entry(db_connection, bm25, encoder):
    """The single most important guarantee: a resolved URI always exists in the catalog.
    Nothing may invent one."""
    permitted = {
        row[0]
        for row in db_connection.execute("SELECT deeplink FROM deeplink_catalog").fetchall()
    }
    from app.catalog.queries import load_queries

    for record in load_queries(OFFICIAL_QUERIES):
        match = resolve_deeplink(
            db_connection, record.query, bm25_index=bm25, encoder=encoder
        )
        assert match is not None
        assert match.deeplink in permitted


def test_resolution_is_deterministic(db_connection, bm25, encoder):
    """HNSW is approximate, so ordering carries an explicit tie-break on id. Identical
    input must produce an identical plan."""
    results = {
        resolve_deeplink(
            db_connection,
            "My battery is draining very quickly",
            bm25_index=bm25,
            encoder=encoder,
        ).deeplink
        for _ in range(20)
    }
    assert len(results) == 1


def test_search_respects_top_k(db_connection, bm25, encoder):
    matches = search_catalog(
        db_connection, "battery", top_k=3, bm25_index=bm25, encoder=encoder
    )
    assert len(matches) <= 3


def test_search_results_are_ordered_by_fused_score(db_connection, bm25, encoder):
    matches = search_catalog(
        db_connection, "screen brightness", top_k=5, bm25_index=bm25, encoder=encoder
    )
    scores = [m.rrf_score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_gibberish_still_returns_only_catalog_members(db_connection, bm25, encoder):
    """A nonsense query may still return something, but never something invented."""
    permitted = {
        row[0]
        for row in db_connection.execute("SELECT deeplink FROM deeplink_catalog").fetchall()
    }
    match = resolve_deeplink(
        db_connection, "zzzz qqqq xyzzy", bm25_index=bm25, encoder=encoder
    )
    assert match is None or match.deeplink in permitted


@pytest.mark.parametrize(
    "query, expected_fragment",
    [
        ("adjust screen brightness", "brightness"),
        ("change the screen timeout duration", "timeout"),
        ("enable blue light filter at night", "eye comfort"),
        ("back up my data to Samsung Cloud", "back up data"),
    ],
)
def test_known_queries_reach_the_expected_screen(
    db_connection, bm25, encoder, query, expected_fragment
):
    """CATALOG-DEPENDENT. Pins fusion behaviour against the official 578-entry catalog.

    "enable blue light filter" is the interesting one: Samsung's screen for it is called
    Eye Comfort Shield, so a keyword-only retriever would miss it entirely and only the
    dense arm can bridge the vocabulary gap.
    """
    match = resolve_deeplink(db_connection, query, bm25_index=bm25, encoder=encoder)
    assert match is not None
    haystack = f"{match.description} {match.message}".lower()
    assert expected_fragment.lower() in haystack


def test_resolution_matches_the_official_sample_deeplink(db_connection, bm25, encoder):
    """The official sample_output.json pairs a backup action with a specific URI.
    Retrieving that same URI from a natural-language intent is an independent check that
    matching on descriptive metadata actually lands on the right screen."""
    match = resolve_deeplink(
        db_connection, "back up my data to Samsung Cloud", bm25_index=bm25, encoder=encoder
    )
    assert match is not None
    assert match.deeplink == "bixby://masked/act/b3ed3ed663"


def test_resolved_matches_carry_validation_data_when_the_catalog_has_it(
    db_connection, bm25, encoder
):
    """Plans can only emit validationDeeplink if retrieval carries it through."""
    match = resolve_deeplink(
        db_connection, "back up my data to Samsung Cloud", bm25_index=bm25, encoder=encoder
    )
    assert match is not None
    assert match.validation_deeplink is not None
    assert match.validation_key is not None
