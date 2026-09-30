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

## Getting Started

### Prerequisites

Make sure the following are installed:

- Python 3.12+
- Docker and Docker Compose
- Git

### 1. Clone the Repository

```bash
git clone https://github.com/Dhanush-Poduval/Guarded_Troubleshooting.git
cd Guarded_Troubleshooting
```

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt

pip install -r requirements-dev.txt
```

### 4. Configure Environment Variables

Create the environment file from the provided example:

```bash
cp .env.example .env
```

Open `.env` and provide the required configuration values, including the Gemini API key.

### 5. Start PostgreSQL + pgvector

```bash
docker compose up -d
```

Verify that the database container is running:

```bash
docker compose ps
```

### 6. Initialize the Database

```bash
python -m scripts.init_db
```

### 7. Build the Deeplink Catalog

```bash
python -m scripts.build_catalog
```

### 8. Start the Application

```bash
python -m scripts.run_api
```

The application will start at:

```text
http://127.0.0.1:8000
```

The same server provides both the REST API and the web interface, so a separate frontend server is not required.

### 9. Verify System Readiness

In another terminal:

```bash
curl http://127.0.0.1:8000/health
```

A ready system should report:

```json
{
  "status": "ok",
  "database": true,
  "embedding_model": true,
  "vector_indexes": true,
  "catalog_indexed": true,
  "cache_ready": true
}
```

Then open:

```text
http://127.0.0.1:8000/
```

in your browser to use the troubleshooting interface.

### Optional: Run Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m pytest \
-p pytest_asyncio.plugin \
--ignore=tests/cache_live.py \
-v
```

## Submission Resources

- Presentation: [View Presentation](./presentation/Smart_Guided_Troubleshooting_Final_Submission_filled.pptx)
- Demo Video: [Watch Demo Video](https://drive.google.com/file/d/1tVMslTQtBKIP-qObZXfjxNKbqdIfXCeE/view?usp=sharing)
