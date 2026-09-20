# SYNTHETIC FIXTURES - DELETE ON ARRIVAL OF THE OFFICIAL DATASET

Nothing in this directory is real. These files exist only so that Phase 3 and Phase 4 can
be built and tested before the official starter assets are available.

When the real deeplinks.json, queries.json, siis_responses.json and samples/ arrive:

1. Delete this entire directory, including the generator script.
2. Point CATALOG_PATH and QUERIES_PATH at the real files.
3. Run `python -m scripts.reset_catalog` BEFORE re-indexing. Cached plans built against
   synthetic deeplinks reference URIs that do not exist in the real catalog, so they must
   be purged rather than migrated.

Every file here carries `"_synthetic": true`. The loader refuses to mark such data as
official and logs a warning on every load, so a demo cannot silently run on fixtures.

The `bixby://masked/act/9xxx` numbering is deliberately outside any plausible real range,
so a stray synthetic URI is obvious if one ever leaks into a stored plan.
