## Project Structure

Guarded Troubleshooting follows a modular architecture that separates query understanding, troubleshooting-step extraction, deeplink retrieval, response validation, semantic caching, persistence, API delivery, evaluation, and the web interface.

The repository is organized as follows:

```text
Guarded_Troubleshooting/
├── app/
│   ├── api/
│   │   ├── main.py
│   │   ├── readiness.py
│   │   └── schemas.py
│   │
│   ├── cache/
│   │   └── store.py
│   │
│   ├── catalog/
│   │   ├── indexer.py
│   │   ├── loader.py
│   │   └── queries.py
│   │
│   ├── contract/
│   │   └── schema.py
│   │
│   ├── db/
│   │   ├── indexes.py
│   │   ├── migrate.py
│   │   ├── pool.py
│   │   └── session.py
│   │
│   ├── embeddings/
│   │   └── encoder.py
│   │
│   ├── pipeline/
│   │   ├── enrichment/
│   │   │   ├── enricher.py
│   │   │   ├── prompts.py
│   │   │   └── schema.py
│   │   │
│   │   ├── extraction/
│   │   │   ├── extractor.py
│   │   │   ├── prompts.py
│   │   │   └── schema.py
│   │   │
│   │   ├── mock/
│   │   │   └── mock_pipeline.py
│   │   │
│   │   ├── real/
│   │   │   └── pipeline.py
│   │   │
│   │   └── port.py
│   │
│   ├── retrieval/
│   │   └── resolver.py
│   │
│   ├── validation/
│   │   └── rules.py
│   │
│   ├── bootstrap.py
│   ├── config.py
│   ├── runtime.py
│   └── service.py
│
├── data/
│   ├── official/
│   │   ├── deeplinks.json
│   │   ├── input.txt
│   │   ├── sample_output.json
│   │   ├── schema.py
│   │   └── siis_responses.json
│   │
│   └── handwritten_paraphrases.json
│
├── db/
│   ├── init/
│   │   └── 01_enable_extension.sql
│   │
│   └── migrations/
│       ├── 001_core_schema.sql
│       ├── 002_official_catalog_fields.sql
│       └── 003_cache_ambiguity.sql
│
├── scripts/
│   ├── benchmark.py
│   ├── build_catalog.py
│   ├── init_db.py
│   ├── prewarm_cache.py
│   ├── reset_catalog.py
│   └── run_api.py
│
├── tests/
│   ├── cache_live.py
│   ├── conftest.py
│   ├── deep_link.py
│   ├── enrichment.py
│   ├── extraction.py
│   ├── full_pipeline.py
│   ├── live_pipeline.py
│   ├── test_api.py
│   ├── test_benchmark_helpers.py
│   ├── test_cache.py
│   ├── test_database_setup.py
│   ├── test_retrieval.py
│   └── test_validation.py
│
├── web/
│   ├── app.js
│   ├── console.css
│   ├── console.html
│   ├── index.html
│   ├── landing.css
│   ├── landing.js
│   └── theme.css
│
├── .env.example
├── docker-compose.yaml
├── metrics.md
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

### Application Layer

The `app/` directory contains the main application and is divided into independent modules for each stage of the troubleshooting system.

#### `app/pipeline/`

This directory contains the core troubleshooting pipeline.

The pipeline converts a noisy natural-language device problem into a structured and validated troubleshooting response. It is divided into enrichment, extraction, retrieval integration, and final response construction.

##### `pipeline/enrichment/`

Implements the query-understanding stage of the system.

- `enricher.py` — Processes the raw user query, generates a normalized representation, and produces multiple semantically related query variations for downstream retrieval and caching.
- `prompts.py` — Contains the prompt definitions used during query enrichment.
- `schema.py` — Defines the structured output expected from the enrichment stage.

The enrichment stage is responsible for transforming potentially noisy user input into a more consistent representation while preserving the original troubleshooting intent.

It also generates multiple query variations so that later components are not dependent on a single phrasing of the problem.

##### `pipeline/extraction/`

Handles structured troubleshooting information extraction.

- `extractor.py` — Converts SIIS troubleshooting content into structured troubleshooting goals and actions.
- `prompts.py` — Defines extraction prompts and formatting requirements.
- `schema.py` — Defines the intermediate structured representation produced by the extraction stage.

The extractor converts source troubleshooting information into individual actions that can subsequently be mapped to device settings and deeplinks.

##### `pipeline/real/`

Contains the production troubleshooting pipeline.

- `pipeline.py` — Coordinates query enrichment, SIIS extraction, deeplink resolution, action construction, validation, and final response generation.

This is the primary pipeline used when a request cannot be satisfied from the semantic cache.

Conceptually, a cold request follows:

```text
User Query
    |
    v
Query Enrichment
    |
    v
SIIS Troubleshooting Extraction
    |
    v
Action Identification
    |
    v
Deeplink Retrieval
    |
    v
Deterministic Verification
    |
    v
Response Construction
    |
    v
Validation
    |
    v
Validated Troubleshooting Response
```

##### `pipeline/mock/`

Contains a deterministic mock implementation of the pipeline.

- `mock_pipeline.py` — Provides a pipeline implementation that can be used for controlled testing and development without depending on the complete external AI pipeline.

##### `pipeline/port.py`

Defines the common interface used by pipeline implementations.

Keeping the pipeline behind a common interface allows the surrounding service layer to operate independently of whether the real or mock implementation is being used.

---

### Semantic Cache

#### `app/cache/`

Contains the semantic troubleshooting-plan cache.

- `store.py` — Implements semantic lookup, plan storage, cache-hit tracking, ambiguity handling, and cache metrics.

Instead of caching only exact strings, the cache stores vector representations of troubleshooting queries and their variations.

This allows semantically equivalent requests such as:

```text
"My screen flashes and becomes blank when I open Gmail."
```

and:

```text
"The display starts flashing and turns black whenever I read an email."
```

to potentially reuse the same validated troubleshooting plan.

The cache uses similarity thresholds together with an ambiguity margin. A result is returned only when the closest cached plan is sufficiently similar and sufficiently distinguishable from competing plans.

The cache path is therefore:

```text
Incoming Query
      |
      v
Embedding Generation
      |
      v
Vector Similarity Search
      |
      +--------------------+
      |                    |
   Valid Hit             Cache Miss
      |                    |
      v                    v
Revalidation          Real Pipeline
      |                    |
      v                    v
Return Plan         Validate + Cache
```

This prevents expensive pipeline execution for troubleshooting requests that have already been solved or are semantic variations of previously solved problems.

---

### Deeplink Retrieval

#### `app/retrieval/`

Contains the deeplink resolution system.

- `resolver.py` — Searches the indexed deeplink catalog and resolves troubleshooting actions to appropriate device settings destinations.

The resolver combines semantic retrieval with lexical/keyword evidence rather than depending entirely on either approach.

Candidate deeplinks are retrieved from the official catalog and then checked before being attached to an action.

A deeplink is not generated or invented when the catalog does not contain a sufficiently appropriate destination.

The retrieval process can be represented as:

```text
Troubleshooting Action
        |
        v
Semantic Retrieval
        +
Keyword Retrieval
        |
        v
Candidate Ranking
        |
        v
Compatibility Verification
        |
        +--------------------+
        |                    |
    Valid Match          No Valid Match
        |                    |
        v                    v
Attach Deeplink       Keep Manual Steps
```

---

### Response Validation

#### `app/validation/`

Contains deterministic validation rules for generated troubleshooting responses.

- `rules.py` — Validates response structure, action ordering, descriptions, deeplinks, steps, categories, and other contract requirements.

The validation layer acts as a guard between AI-generated/intermediate content and the final API response.

Among other checks, the validator ensures that:

- generated responses follow the required schema;
- goals follow the expected troubleshooting/configuration format;
- action titles and descriptions satisfy formatting constraints;
- troubleshooting descriptions satisfy the required word limits;
- individual steps represent executable user interactions;
- deeplinks originate from the permitted catalog;
- invalid URLs are not exposed;
- disruptive or critical actions appear after safer actions.

A generated plan must pass validation before it can be stored and served as a trusted cached response.

---

### Embeddings

#### `app/embeddings/`

Contains the embedding layer used throughout the system.

- `encoder.py` — Generates vector representations for queries and searchable text.

Embeddings are shared by multiple components, including:

- semantic cache lookup;
- paraphrase matching;
- deeplink retrieval;
- catalog indexing.

Using a shared embedding layer keeps semantic comparisons consistent across the application.

---

### Deeplink Catalog

#### `app/catalog/`

Handles loading, indexing, and querying of the official deeplink dataset.

- `loader.py` — Reads and normalizes catalog records.
- `indexer.py` — Generates and stores searchable catalog representations.
- `queries.py` — Provides catalog-related database queries.

The catalog is indexed into PostgreSQL so the retrieval layer can efficiently perform semantic and lexical searches.

The official catalog itself remains separate from runtime-generated troubleshooting plans.

---

### API Layer

#### `app/api/`

Contains the REST interface exposed by the application.

- `main.py` — Defines the FastAPI application and troubleshooting endpoints.
- `schemas.py` — Defines API request and response models.
- `readiness.py` — Performs health and dependency-readiness checks.

The main troubleshooting endpoint passes requests through the service layer rather than directly invoking the AI pipeline.

This is important because it allows every API request to benefit from semantic caching.

The request flow is:

```text
REST Request
     |
     v
FastAPI
     |
     v
Troubleshooting Service
     |
     v
Semantic Cache
   /     \
 Hit     Miss
  |        |
  |        v
  |    Real Pipeline
  |        |
  +--------+
     |
     v
Validation
     |
     v
API Response
```

The API also exposes health and cache statistics endpoints for operational visibility.

---

### Service Layer

#### `app/service.py`

The service layer is the main orchestration boundary between the API, cache, pipeline, validator, and metrics system.

For each troubleshooting request, the service:

1. generates the query embedding;
2. performs semantic cache lookup;
3. checks similarity and ambiguity conditions;
4. revalidates cached responses before serving them;
5. invokes the real pipeline when no acceptable cache entry exists;
6. validates newly generated plans;
7. stores successful plans and query variations in the semantic cache;
8. records latency and cache metrics;
9. returns operational metadata with the troubleshooting response.

This means the API itself does not need to understand whether a response originated from the cache or from the full pipeline.

---

### Bootstrap and Runtime

#### `app/bootstrap.py`

Constructs the major application dependencies.

It connects components such as:

- database pool;
- embedding encoder;
- catalog;
- deeplink resolver;
- validator;
- pipeline;
- semantic cache;
- troubleshooting service.

Centralizing dependency construction keeps application startup consistent between the API, tests, and scripts.

#### `app/config.py`

Defines runtime configuration and environment-backed settings.

Configuration includes values related to:

- database connectivity;
- model configuration;
- cache thresholds;
- ambiguity margins;
- cache versions;
- vector search parameters;
- pipeline behavior.

#### `app/runtime.py`

Contains runtime utilities shared by application entry points.

---

### Data Contract

#### `app/contract/`

Contains the canonical troubleshooting response schema.

- `schema.py` — Defines the structured response objects used throughout the system.

Keeping the response contract independent from the API allows the pipeline, validator, cache, and API to operate on the same structured representation.

---

### Database Layer

#### `app/db/`

Contains application-level PostgreSQL access.

- `pool.py` — Manages asynchronous database connection pooling.
- `session.py` — Provides database session/connection helpers.
- `migrate.py` — Handles database migration execution.
- `indexes.py` — Handles required database and vector indexes.

PostgreSQL is used as both the persistent application database and the vector-search backend through `pgvector`.

---

### Database Schema and Migrations

#### `db/`

Contains SQL required to initialize and evolve the database.

```text
db/
├── init/
│   └── 01_enable_extension.sql
└── migrations/
    ├── 001_core_schema.sql
    ├── 002_official_catalog_fields.sql
    └── 003_cache_ambiguity.sql
```

`01_enable_extension.sql` enables the required PostgreSQL extension.

The migrations then establish the core schema, catalog-related fields, semantic cache structures, and ambiguity-related metrics.

This keeps database changes reproducible instead of requiring manual database modification.

---

### Official Data

#### `data/official/`

Contains the challenge-provided source data used by the application.

- `input.txt` — Official troubleshooting input data.
- `siis_responses.json` — SIIS troubleshooting responses used by the pipeline.
- `deeplinks.json` — Official deeplink catalog.
- `sample_output.json` — Example of the expected response format.
- `schema.py` — Schema associated with the supplied dataset.

These files are treated as source data. Runtime-generated cache entries are stored separately in PostgreSQL.

#### `data/handwritten_paraphrases.json`

Contains manually prepared semantic variations used for evaluating cache behavior across differently worded requests.

This allows evaluation to distinguish between exact-string caching and genuine semantic reuse.

---

### Scripts

#### `scripts/`

Contains operational and evaluation utilities.

- `init_db.py` — Initializes the database.
- `build_catalog.py` — Loads and indexes the deeplink catalog.
- `reset_catalog.py` — Resets/rebuilds catalog state when required.
- `prewarm_cache.py` — Pre-populates semantic cache entries.
- `run_api.py` — Starts the REST API.
- `benchmark.py` — Runs system-level evaluation and produces performance measurements.

These scripts keep common setup and evaluation operations reproducible.

---

### Tests

#### `tests/`

Contains unit, integration, pipeline, API, retrieval, validation, database, and cache tests.

Important test groups include:

- `enrichment.py` — Query-enrichment behavior.
- `extraction.py` — Structured troubleshooting extraction.
- `deep_link.py` — Deeplink-related behavior.
- `full_pipeline.py` — End-to-end pipeline behavior.
- `live_pipeline.py` — Pipeline execution against live model dependencies.
- `cache_live.py` — Real semantic-cache latency and integration behavior.
- `test_cache.py` — Cache correctness and edge cases.
- `test_retrieval.py` — Deeplink retrieval behavior.
- `test_validation.py` — Deterministic response-validation rules.
- `test_database_setup.py` — Database and schema readiness.
- `test_api.py` — REST API behavior and response contract.
- `test_benchmark_helpers.py` — Benchmark utility behavior.
- `conftest.py` — Shared pytest fixtures and test configuration.

The test suite separates deterministic tests from live tests where possible so individual components can be verified independently.

---

### Web Interface

#### `web/`

Contains the browser-based interface for interacting with the troubleshooting system.

- `index.html` — Main landing interface.
- `landing.css` — Landing-page styling.
- `landing.js` — Landing-page interaction logic.
- `console.html` — Troubleshooting console interface.
- `console.css` — Console-specific styling.
- `app.js` — Frontend application and API interaction logic.
- `theme.css` — Shared visual theme definitions.

The frontend communicates with the same REST API used by external clients, keeping the user interface separated from the backend troubleshooting logic.

---

### Evaluation

#### `metrics.md`

Contains benchmark and evaluation results for the system.

The evaluation layer measures areas such as:

- response/schema compliance;
- semantic cache performance;
- cache-hit latency;
- cache-hit rate;
- pipeline latency;
- deeplink validity;
- retrieval behavior;
- URL leakage;
- semantic paraphrase behavior.

#### `scripts/benchmark.py`

Provides the corresponding benchmark runner used to evaluate these properties across the supplied dataset and paraphrase cases.

---

### Environment and Dependencies

#### `.env.example`

Documents the environment variables required to configure the application without committing credentials to the repository.

#### `requirements.txt`

Contains the Python dependencies required to run the application.

#### `requirements-dev.txt`

Contains additional dependencies used during development and testing.

#### `docker-compose.yaml`

Defines the containerized PostgreSQL/pgvector database environment used by the project.

#### `pytest.ini`

Contains pytest configuration used by the automated test suite.

---

## System Architecture

At a high level, Guarded Troubleshooting combines an AI-based troubleshooting pipeline with deterministic validation, catalog-grounded deeplink retrieval, and semantic caching.

```text
                         User Query
                             |
                             v
                     REST API / Web UI
                             |
                             v
                  Troubleshooting Service
                             |
                             v
                    Query Embedding
                             |
                             v
                    Semantic Cache
                      /           \
                 Cache Hit      Cache Miss
                    |               |
                    |               v
                    |        Query Enrichment
                    |               |
                    |               v
                    |        SIIS Extraction
                    |               |
                    |               v
                    |       Deeplink Retrieval
                    |               |
                    |               v
                    |      Response Construction
                    |               |
                    |               v
                    |          Validation
                    |               |
                    |               v
                    |         Cache Storage
                    |               |
                    +-------+-------+
                            |
                            v
                     Final Validation
                            |
                            v
                       API Response
```

The architecture is designed so that expensive processing occurs only when necessary. Once a validated troubleshooting plan has been generated, semantically similar future requests can reuse it through the vector cache while still passing through deterministic validation before being returned.

This separation also ensures that AI-generated content does not directly control deeplinks or bypass the response contract. Deeplink candidates originate from the indexed catalog, generated plans are checked against deterministic rules, and only validated responses are returned to the client.

# System Performance Metrics & Evaluation Report
**Model(s):** gemini-3.5-flash via the Phase 0-2 pipeline
**Embeddings:** BAAI/bge-small-en-v1.5 (384d, ONNX Runtime)
**Environment:** 16 logical CPUs / 17 GB RAM / Windows-11-10.0.26200-SP0
**Generated:** 2026-09-30 11:13:41 by `python -m scripts.benchmark`

> Every figure below was measured during the run that produced this file.
> Rows that cannot be measured honestly yet say so and give the reason.

---

## 1. Schema & Rule Compliance
Evaluated on all 20 official queries from `siis_responses.json`.

| Metric | Target | Measured Value |
| :--- | :--- | :--- |
| Schema-valid output lines | >= 99% | 100.0% |
| Rule compliance (Goal / Title / Description syntax) | >= 95% | 100.0% |
| Absolute URL leaks | 0 | 0 |
| Deeplink catalog validity (exact URI match) | 100% | 100.0% |
| Auto actions carrying valid actionable deeplink | >= 90% | 62.5% |
| Query variations within 8-10 distinct | informational | 100.0% |

---

## 2. Accuracy Benchmarks

| Evaluation Metric | Scale / Anchor | Score |
| :--- | :--- | :--- |
| Step accuracy (completeness, correctness, ordering) | 0.0 - 3.0 | not measured |
| Deeplink relevance (exact target screen vs. parent menu) | 0.0 - 2.0 | not measured |

**Why these are blank.** Both are human-judged scores against reference plans. The kit ships one worked example, which is too small a sample to score against, and no per-query ground-truth plans. Producing a number here would mean grading our own output against itself, so it is left unmeasured rather than filled with a figure that looks like evidence.

One narrow correctness check is available and does pass: retrieving from the natural-language intent "back up my data to Samsung Cloud" returns `bixby://masked/act/b3ed3ed663`, the exact URI the official `sample_output.json` pairs with its backup action.

---

## 3. Latency Benchmarks (N = 30 requests per path)

| Execution Path | Target (P95) | P50 (ms) | P95 (ms) |
| :--- | :--- | :--- | :--- |
| Cache hit - exact query match | <= 300 ms | 436.5 | 1234.8 |
| Cache hit - unseen semantic paraphrase | <= 300 ms | 318.2 | 537.0 |
| Cold query - full pipeline extraction & mapping | <= 8000 ms | 10185.8 | 10721.2 |

**The cold path misses its target.** It is two sequential Gemini calls, query enrichment then structure extraction, and each costs seconds. This is a real limitation rather than a measurement artifact: it was consistent across the twenty pre-warm runs. The architecture is built to keep traffic off this path, and at the measured hit rate most requests never reach it, but a genuinely novel query does pay it. Running the two stages concurrently, or moving enrichment to the lite model, are the obvious levers and are not yet applied.

Fast-path figures were taken on a developer laptop that was also running Docker, PostgreSQL and the benchmark process itself. The median is stable, but the 95th percentile carries a tail from CPU contention during embedding: measured in isolation on the same machine the encoder returns p95 23.7 ms with a worst case of 34.3 ms over 60 calls, so the tail reflects the measurement environment rather than the encoder.

Fast-path breakdown on a cache hit: embedding 819.0 ms median, cache lookup 32.5 ms median.

The paraphrase row times only the rewordings that actually hit the cache. A rewording that misses runs the pipeline, so folding it into a row labelled "cache hit" would report the cold path under a fast-path heading.

---

## 4. Operational Cost & Cache Efficacy

| Metric Item | Target | Measured Value |
| :--- | :--- | :--- |
| Cold query average inference cost | Tracked | $0.00 |
| Cache hit inference cost | $0.00 | $0.00 |
| Semantic cache hit rate (on unseen paraphrases) | >= 80% | 90% (18/20) |
| Cost derivation method | - | (prompt tokens + completion tokens) x rate |

Both cost rows are genuinely $0.00 because the pipeline behind the port is the mock and makes no paid calls. The accounting path is wired: `PipelineMeta` carries model, cost and token counts, the cache stores them per plan, and `request_metrics` records cost per request, so real figures appear as soon as a paid pipeline is bound.

**The hit-rate figure is measured against 20 rewordings this team wrote** (`data/handwritten_paraphrases.json`), one per official query. The kit ships no paraphrase ground truth. This number is therefore evidence that the cache generalises across phrasing, not an official evaluation result.

Rewordings that did not hit (ambiguous x2):
- `ambiguous` - The display on my Z Flip 7 is totally dark so I cannot see or tap anything, and I have no way to
- `ambiguous` - The folding inner display on my Flip 7 has gone blank and ignores touch, although the outside co

Every miss was an ambiguity rejection rather than a similarity failure, and all of them name a Flip device. The cache holds four separate Flip plans covering a dark inner screen, a blank fold, a flickering fold and a half-dark display; a rewording of any one of them sits nearly as close to the other three, so the margin check declines to choose and runs the pipeline instead. That is the intended trade: these four are exactly the cases where answering from cache would have been a coin flip between similar plans. Removing the margin would raise this figure while allowing the wrong Flip plan to be served.

---

## 5. Architectural Ablation Analysis

| Architecture Variant | Step Accuracy | Latency P50 / P95 (ms) | Resolved | Agreement with A | Key Observations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Variant A: Hybrid BM25 + Dense | not measured | 60.3 / 413.3 | 20/20 | 100% | Shipped configuration. Weighted RRF, dense 1.0 and sparse 0.8. |
| Baseline: Dense embedding only | not measured | 368.9 / 467.5 | 20/20 | 30% | Bridges vocabulary gaps such as blue light filter to Eye Comfort Shield, but drifts to topically near screens. |
| Variant B: BM25 keyword only | not measured | 380.0 / 470.7 | 20/20 | 25% | Exact terminology only. Misses any screen whose catalog wording differs from the user's. |

Step accuracy is blank for the same reason as section 2: scoring a variant needs reference plans. Agreement is reported instead, meaning how often each variant picks the same catalog screen as the shipped hybrid over the official queries. It shows how much each retriever contributes without claiming which is correct.

The full-LLM baseline the template names is not implementable here: it requires the Phase 0-2 pipeline, which is still the mock.

---

## 6. Known Edge Cases & System Limitations

- **Every official query concerns the screen.** Pairwise similarity between the 20 distinct problems reaches 0.8742 with a mean of 0.7361, so a similarity threshold alone cannot separate a reworded query from a different problem phrased similarly. The cache therefore also requires the best plan to beat the runner-up plan by a margin, and treats a near-tie as a miss.
- **Short queries are riskier than long ones.** The official queries run 17 to 36 words. A very short query carries less signal and lands near many plans; probes of two or three words were observed either missing or, when a short plan had been cached, matching the wrong one. Real traffic that is mostly short would need the threshold re-tuned.
- **The ambiguity margin catches ties, not confident mistakes.** It rejects when two plans are near-equally close. A single clearly-closest but wrong plan passes it, because nothing in the similarity signal marks it as wrong.
- **Multi-intent queries are not split.** One official query bundles three complaints (a cracked fold, dead touch areas and a dim display) and is handled as a single request.
- **Approximate index, deterministic ordering.** HNSW is approximate, so every ordering carries an explicit tie-break on row id to keep identical inputs producing identical plans. At the current corpus size PostgreSQL chooses a sequential scan over the index anyway; the index is in place for growth and concurrency, and is not what delivers the fast-path latency.
- **Only 138 of 578 catalog entries carry a complete validation block.** The other 432 with a validation deeplink have only a deeplink and a key, so `validationDeeplink` is emitted with comparison fields only where the catalog supports it rather than being invented.
- **Description length follows the sample, not the prose rule.** The written specification says 5 to 7 words, but the official `sample_output.json` uses 9 and 12, so enforcing the written rule would reject the reference artifact. The bound is 5 to 15 and is configurable via `DESCRIPTION_MAX_WORDS`.
- **Windows event loop.** psycopg's async mode cannot run on the default Proactor loop; the failure surfaces as a pool timeout that resembles a database outage. `app/runtime.py` selects a compatible loop and `scripts/run_api.py` passes `--loop asyncio` to uvicorn.

