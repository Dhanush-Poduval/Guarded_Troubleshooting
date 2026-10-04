# Guarded Troubleshooting

**Smart Guided Troubleshooting with Semantic Caching, Deeplink Resolution, and Verified Resolution**

Guarded Troubleshooting is an AI-assisted troubleshooting platform that transforms noisy natural-language device complaints into structured, validated, and actionable troubleshooting plans.

The system combines query enrichment, SIIS-grounded troubleshooting extraction, catalog-backed deeplink retrieval, deterministic validation, semantic caching, and a guided resolution workflow.

Beyond generating troubleshooting steps, the platform can guide a user through a validated plan one action at a time, track progress, verify expected device state when trusted evidence is available, distinguish system verification from user confirmation, protect critical actions, and generate a final resolution receipt.

---

## Core Features

- **Query Enrichment** — Normalizes noisy user complaints and generates semantic query variations.
- **SIIS-Grounded Extraction** — Converts troubleshooting source information into structured goals and actions.
- **Catalog-Grounded Deeplinks** — Maps troubleshooting actions to official device settings destinations without inventing URIs.
- **Deterministic Validation** — Enforces schema, formatting, deeplink integrity, action ordering, and safety requirements.
- **Semantic Cache** — Reuses validated plans for semantically similar queries and bypasses expensive pipeline execution on valid hits.
- **Guided Resolution** — Walks users through a validated troubleshooting plan one action at a time.
- **Task Verification** — Compares expected and observed device state using deterministic verification rules.
- **Evidence Provenance** — Distinguishes trusted system evidence, client-reported observations, and user confirmation.
- **Critical Action Protection** — Requires explicit acknowledgement before disruptive troubleshooting actions.
- **Resolution Receipts** — Records the final outcome, attempted actions, verification method, and available evidence.
- **REST API and Web UI** — FastAPI serves both the application API and browser interface.
- **PostgreSQL and pgvector** — Provides persistence, vector search, semantic caching, and guided-session storage.

---

## System Architecture

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
                      Cache Hit       Cache Miss
                         |               |
                         |               v
                         |        Query Enrichment
                         |               |
                         |               v
                         |         SIIS Extraction
                         |               |
                         |               v
                         |        Deeplink Retrieval
                         |               |
                         |               v
                         |      Response Construction
                         |               |
                         |               v
                         |          Validation
                         |               |
                         +-------+-------+
                                 |
                                 v
                     Validated Troubleshooting Plan
                                 |
                                 v
                       Guided Resolution Session
                                 |
                  +--------------+--------------+
                  |                             |
           Expected State                Perform Action
                  |                             |
                  |                             v
                  |                   Verify / Observe /
                  |                        Confirm
                  |                             |
                  +-------------+---------------+
                                |
                                v
                     Deterministic Comparator
                                |
               +----------------+----------------+
               |                |                |
        System Verified   User Confirmed    Failed /
                                           Inconclusive
               |                |                |
               +----------------+----------------+
                                |
                                v
                        Resolution Receipt
```

The AI pipeline helps understand and structure troubleshooting information, but it does not control the system's trust boundary.

Deeplinks originate from the official catalog, generated plans pass deterministic validation, and only trusted device-adapter evidence can result in a `system_verified` outcome.

---

## Troubleshooting Pipeline

A cache miss enters the complete troubleshooting pipeline:

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

### Query Enrichment

The enrichment stage converts noisy user input into a normalized representation while preserving the original troubleshooting intent.

It also generates multiple semantic variations so later components are not dependent on a single phrasing of the problem.

The enrichment layer is implemented under:

```text
app/pipeline/enrichment/
```

It contains:

- `enricher.py` — performs query enrichment and model interaction.
- `prompts.py` — contains enrichment prompt definitions.
- `schema.py` — defines the structured enrichment output.

---

### SIIS Extraction

Troubleshooting source information is converted into structured goals and actions.

The extraction stage identifies information such as:

- troubleshooting goal;
- action names;
- descriptions;
- individual interaction steps;
- action categories.

The extraction layer is located under:

```text
app/pipeline/extraction/
```

It contains:

- `extractor.py` — performs structured troubleshooting extraction.
- `prompts.py` — contains extraction prompts.
- `schema.py` — defines the intermediate extraction structure.

---

### Deeplink Resolution

Troubleshooting actions are mapped against the official deeplink catalog using semantic and lexical retrieval.

The retrieval process is:

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

Candidates are verified before being attached to actions.

If no sufficiently appropriate catalog entry exists, the system does not invent a deeplink.

---

### Response Validation

Before a response can be served or cached, deterministic rules validate the generated troubleshooting plan.

Validation includes:

- response structure;
- troubleshooting goal format;
- action names;
- descriptions;
- individual interaction steps;
- action categories;
- deeplink integrity;
- URL leakage;
- critical-action ordering.

Only validated responses can be stored and reused as trusted troubleshooting plans.

---

## Semantic Cache

Instead of caching only exact strings, Guarded Troubleshooting stores semantic vector representations of queries and their variations.

For example:

```text
"My screen flashes and becomes blank when I open Gmail."
```

and:

```text
"The display starts flashing and turns black whenever I read an email."
```

may resolve to the same validated troubleshooting plan.

The cache workflow is:

```text
Incoming Query
      |
      v
Embedding Generation
      |
      v
Vector Similarity Search
      |
      +----------------------+
      |                      |
   Valid Hit              Cache Miss
      |                      |
      v                      v
Revalidation            Real Pipeline
      |                      |
      v                      v
Return Plan          Validate + Cache
```

Cache entries must satisfy both similarity and ambiguity requirements.

Cached responses are revalidated before being returned.

This allows repeated or semantically equivalent troubleshooting requests to avoid unnecessary AI pipeline execution.

---

## Guided Resolution and Verification

The guided resolution system extends the original troubleshooting pipeline.

A user first receives a validated troubleshooting plan through:

```text
POST /v1/troubleshoot
```

A guided resolution session can then be created from that plan.

```text
Validated Plan
     |
     v
Start Resolution Session
     |
     v
Present Current Action
     |
     v
User Performs Action
     |
     v
Check Expected State
     |
     +--------------------------+
     |                          |
Trusted Adapter          User Observation /
     |                    User Confirmation
     v                          |
System Verification            v
                         Explicitly Labelled
                           User Evidence
```

Each resolution session maintains its own plan snapshot and progress cursor.

This separates individual user progress from troubleshooting plans stored in the shared semantic cache.

---

## Verification Model

The verification system deliberately distinguishes between different levels and sources of evidence.

### System Verification

A result can become:

```text
system_verified
```

only when the observed value was obtained through a trusted adapter and satisfies the expected validation contract.

The client cannot declare its own evidence trusted.

This prevents the application from claiming that it independently verified a device state when it did not.

---

### User Observation

A user may manually provide an observed device value.

For example:

```text
Expected: Wi-Fi = True
Observed: True
```

This evidence is recorded as:

```text
client_reported
```

Even if the reported value matches the expected state, client-reported evidence does not automatically become `system_verified`.

---

### User Confirmation

The user can directly confirm whether the troubleshooting process solved the problem.

A successful user confirmation produces:

```text
user_confirmed
```

rather than claiming that the server independently verified the device state.

This distinction is retained in the final resolution receipt.

---

## Verification Outcomes

The resolution system distinguishes between outcomes such as:

```text
pending
system_verified
user_confirmed
verification_failed
verification_unavailable
inconclusive
```

This prevents the application from overstating what it knows about the device.

---

## Trusted Adapter Boundary

The production application does not assume that it has access to device state.

If no trusted device-state channel is available, automatic verification reports that verification is unavailable instead of fabricating a successful result.

Tests use a deterministic fake trusted adapter to exercise the system-verification path.

Only catalog entries containing a sufficiently complete validation contract can support automatic verification.

A validation contract can contain:

```text
key
resultType
condition
value
```

The deterministic comparator evaluates observed state against this expected contract.

---

## Critical Action Protection

Some troubleshooting actions may be disruptive.

Before a resolution session advances into a critical action, the user must explicitly acknowledge it.

```text
Normal Action
     |
     v
Next Action Critical?
     |
    Yes
     |
     v
Require User Acknowledgement
     |
     v
Present Critical Action
```

This prevents disruptive troubleshooting actions from being entered silently.

---

## Resolution Receipts

Completed sessions can generate a resolution receipt.

A receipt can contain information such as:

- final session status;
- verification status;
- verification method;
- actions attempted;
- successful action;
- expected state;
- observed state;
- verification attempts;
- completion time.

Most importantly, the receipt distinguishes:

```text
system_verified
```

from:

```text
user_confirmed
```

A user-confirmed result therefore cannot be mistaken for a device state independently verified by the system.

---

## Resolution API

The guided-resolution workflow is exposed through the following endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/v1/resolution/sessions` | Start a guided resolution session |
| `GET` | `/v1/resolution/sessions/{id}` | Get the current session state |
| `POST` | `/v1/resolution/sessions/{id}/presented` | Record that the current action was presented |
| `POST` | `/v1/resolution/sessions/{id}/verify` | Attempt trusted automatic verification |
| `POST` | `/v1/resolution/sessions/{id}/observations` | Submit a user-observed value |
| `POST` | `/v1/resolution/sessions/{id}/confirm` | Record whether the user says the issue is resolved |
| `POST` | `/v1/resolution/sessions/{id}/advance` | Advance to the next validated action |
| `POST` | `/v1/resolution/sessions/{id}/complete` | Complete the resolution session |
| `GET` | `/v1/resolution/sessions/{id}/receipt` | Retrieve the final resolution receipt |

Observation requests can include an idempotency token so retried requests do not accidentally create duplicate verification attempts.

---

## Project Structure

```text
Guarded_Troubleshooting/
│
├── app/
│   ├── api/
│   │   ├── main.py
│   │   ├── readiness.py
│   │   ├── schemas.py
│   │   ├── resolution_routes.py
│   │   └── resolution_schemas.py
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
│   ├── resolution/
│   │   ├── models.py
│   │   ├── service.py
│   │   └── store.py
│   │
│   ├── retrieval/
│   │   └── resolver.py
│   │
│   ├── validation/
│   │   └── rules.py
│   │
│   ├── verification/
│   │   ├── adapter.py
│   │   ├── comparator.py
│   │   └── fake.py
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
│       ├── 003_cache_ambiguity.sql
│       └── 004_resolution_sessions.sql
│
├── scripts/
│   ├── audit_auto_deeplinks.py
│   ├── benchmark.py
│   ├── build_catalog.py
│   ├── init_db.py
│   ├── prewarm_cache.py
│   ├── reset_catalog.py
│   └── run_api.py
│
├── tests/
│   ├── cache_live.py
│   ├── enrichment.py
│   ├── extraction.py
│   ├── full_pipeline.py
│   ├── live_pipeline.py
│   ├── test_api.py
│   ├── test_auto_deeplink_invariant.py
│   ├── test_cache.py
│   ├── test_database_setup.py
│   ├── test_resolution.py
│   ├── test_resolution_api.py
│   ├── test_retrieval.py
│   ├── test_validation.py
│   └── test_verification.py
│
├── web/
│   ├── app.js
│   ├── resolution.js
│   ├── console.html
│   ├── console.css
│   ├── index.html
│   ├── landing.js
│   ├── landing.css
│   └── theme.css
│
├── presentation/
│   └── Smart_Guided_Troubleshooting_Final_Submission_filled.pptx
│
├── ai_disclosure/
│   └── LangAI3.0_AI_Disclosure.docx
│
├── .env.example
├── docker-compose.yaml
├── metrics.md
├── metrics_prewarmed.md
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

---

## Major Components

### Troubleshooting Pipeline

`app/pipeline/`

Contains the core troubleshooting pipeline.

The real pipeline coordinates:

```text
Query Enrichment
      |
      v
SIIS Extraction
      |
      v
Action Construction
      |
      v
Deeplink Resolution
      |
      v
Validation
```

The `mock/` implementation provides a deterministic alternative for controlled testing and development.

---

### Semantic Cache

`app/cache/store.py`

Implements the PostgreSQL/pgvector semantic plan cache.

It handles:

- semantic lookup;
- query-vector storage;
- similarity thresholds;
- ambiguity detection;
- cache-plan storage;
- cache metrics;
- plan reuse.

The cache is positioned before the full AI pipeline so valid semantic hits can bypass expensive pipeline execution.

---

### Deeplink Retrieval

`app/retrieval/resolver.py`

Maps troubleshooting actions to compatible entries from the official deeplink catalog.

Retrieval combines semantic and lexical evidence rather than depending entirely on one retrieval method.

---

### Validation

`app/validation/rules.py`

Contains deterministic rules protecting the troubleshooting response contract.

AI-generated or intermediate content cannot bypass this validation layer before becoming a trusted response.

---

### Resolution

`app/resolution/`

Implements the guided-resolution state machine.

It manages:

- resolution sessions;
- immutable plan snapshots;
- current action position;
- session state transitions;
- verification attempts;
- critical actions;
- completion states;
- resolution receipts.

---

### Verification

`app/verification/`

Contains the deterministic verification core.

It provides:

- expected-state contracts;
- evidence-source tracking;
- value comparison;
- verification statuses;
- trusted adapter interfaces;
- unavailable production adapter;
- fake trusted adapter for testing.

Verification decisions are deliberately kept outside the language model.

---

### API

`app/api/`

Exposes the application's REST API.

The original troubleshooting endpoints remain available while the resolution routes add the guided troubleshooting and verification workflow.

---

### Web Interface

`web/`

Contains the browser-based user interface.

`app.js` manages the primary troubleshooting experience and communication with the API.

`resolution.js` powers the guided resolution and verification experience.

The frontend communicates with the same REST API exposed to external clients.

---

### Database

`app/db/` contains application-level PostgreSQL access and connection management.

PostgreSQL is used for:

- application persistence;
- semantic plan caching;
- vector search through pgvector;
- deeplink catalog indexing;
- resolution sessions;
- verification attempts.

---

### Resolution Database Migration

`db/migrations/004_resolution_sessions.sql`

Adds persistence required by the guided-resolution feature.

Resolution sessions and verification attempts are stored separately from shared semantic-cache plans.

Verification attempts are append-only so previous evidence is not silently overwritten.

The database also enforces the trust rule that `system_verified` attempts must originate from trusted-adapter evidence.

---

## Getting Started

### Prerequisites

Make sure the following are installed:

- Python 3.12+
- Docker
- Docker Compose
- Git
- A Gemini API key

---

### 1. Clone the Repository

```bash
git clone https://github.com/Dhanush-Poduval/Guarded_Troubleshooting.git
cd Guarded_Troubleshooting
```

---

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```bash
.venv\Scripts\activate
```

---

### 3. Install Dependencies

Install the runtime dependencies:

```bash
pip install -r requirements.txt
```

For development and testing:

```bash
pip install -r requirements-dev.txt
```

---

### 4. Configure Environment Variables

Create the local environment file from the provided template:

```bash
cp .env.example .env
```

Add your Gemini API key to `.env`:

```env
GEMINI_API_KEY=your_api_key_here
```

The remaining local configuration is documented in `.env.example`.

The real `.env` file is intentionally excluded from version control and should not be committed.

---

### 5. Start PostgreSQL and pgvector

```bash
docker compose up -d
```

Check that the database is running:

```bash
docker compose ps
```

The default local PostgreSQL instance is exposed on:

```text
localhost:5433
```

---

### 6. Initialize the Database

```bash
python -m scripts.init_db
```

This applies the database migrations, including the guided-resolution session schema.

---

### 7. Build the Deeplink Catalog

```bash
python -m scripts.build_catalog
```

This loads and indexes the official deeplink catalog used by the retrieval system.

---

### 8. Start the Application

```bash
python -m scripts.run_api
```

Open:

```text
http://127.0.0.1:8000/
```

The FastAPI/Uvicorn process serves both the backend API and the frontend.

A separate frontend server is not required.

---

### 9. Verify System Readiness

Run:

```bash
curl http://127.0.0.1:8000/health
```

A ready system reports:

```json
{
  "status": "ok"
}
```

along with individual dependency-readiness information.

---

## Useful Endpoints

```text
GET  /health
GET  /cache/stats
GET  /v1/examples
POST /v1/troubleshoot
```

The guided-resolution endpoints are available under:

```text
/v1/resolution/
```

FastAPI interactive API documentation is available at:

```text
http://127.0.0.1:8000/docs
```

---

## Testing

Install the development dependencies first:

```bash
pip install -r requirements-dev.txt
```

Run the deterministic test suite:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
python -m pytest \
-p pytest_asyncio.plugin \
--ignore=tests/cache_live.py \
-v
```

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` prevents unrelated system-wide pytest plugins from interfering with the project's test environment.

Live tests may depend on external services or available model quota and are therefore separable from the deterministic test suite.

The guided resolution and verification functionality is covered by:

```text
tests/test_resolution.py
tests/test_resolution_api.py
tests/test_verification.py
```

These tests cover behavior including:

- resolution state transitions;
- deterministic value comparison;
- trusted evidence;
- client-reported evidence;
- user confirmation;
- critical-action acknowledgement;
- idempotent observations;
- resolution receipts;
- backwards compatibility with the original API.

---

## Data and Persistence

Official challenge data remains under:

```text
data/official/
```

Runtime-generated data is stored separately in PostgreSQL.

The semantic cache stores reusable validated troubleshooting plans and their query vectors.

Guided resolution sessions maintain their own plan snapshots so one user's troubleshooting progress does not modify a plan shared through the semantic cache.

Verification attempts are append-only, preserving what was attempted and how the evidence was obtained.

---

## Security and Trust Boundaries

The resolution system is designed to avoid overstating what the application knows.

Key safeguards include:

- Clients cannot inject the deeplink used by the resolution-session cursor.
- Clients cannot label their own evidence as trusted.
- Client-reported matching values cannot produce `system_verified`.
- User confirmation is distinguished from system verification.
- Critical actions require explicit acknowledgement.
- Troubleshooting plans are revalidated before guided resolution begins.
- The database enforces trusted evidence for `system_verified` attempts.
- Missing device-state access results in unavailable or inconclusive verification rather than fabricated success.
- Resolution sessions operate on validated plan snapshots instead of mutable client-provided navigation.
- Deeplinks are obtained from the permitted catalog rather than generated freely by the model.

---

## Evaluation

Evaluation and benchmarking resources include:

```text
metrics.md
metrics_prewarmed.md
scripts/benchmark.py
scripts/audit_auto_deeplinks.py
```

The evaluation layer covers areas including:

- response and schema compliance;
- semantic cache behavior;
- cache-hit latency;
- cache-hit rate;
- pipeline latency;
- deeplink validity;
- retrieval behavior;
- URL leakage;
- semantic paraphrase behavior;
- automatic deeplink invariants.

---

## Environment and Dependencies

### `.env.example`

Documents the environment variables required to configure the application without exposing credentials.

### `requirements.txt`

Contains the Python dependencies required to run the application.

### `requirements-dev.txt`

Contains additional development and testing dependencies such as pytest.

### `docker-compose.yaml`

Defines the PostgreSQL and pgvector database environment.

### `pytest.ini`

Contains pytest configuration used by the automated test suite.

---

## Submission Resources

- Presentation: [View Presentation](./presentation/Smart_Guided_Troubleshooting_Final_Submission_filled.pptx)
- Demo Video: [Watch Demo Video](https://drive.google.com/file/d/1tVMslTQtBKIP-qObZXfjxNKbqdIfXCeE/view?usp=sharing)
- AI Disclosure: [View AI Disclosure](./ai_disclosure/LangAI3.0_AI_Disclosure.docx)
