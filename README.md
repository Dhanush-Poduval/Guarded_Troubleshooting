# Smart Guided Troubleshooting Engine

Turns unstructured Galaxy device complaints into validated, deeplinked troubleshooting
plans, served from a semantic cache so that a previously-seen problem is answered without
re-running the extraction pipeline.

## Pipeline

```
Raw complaint (+ optional SIIS reference text)
  |
  ├─ [0] Query enrichment            ─┐
  ├─ [1] Structure extraction (LLM)   ├─ Phase 0-2, behind PipelinePort
  ├─ [2] Deeplink mapping & ordering ─┘
  |
  ├─ [3] Fast-path semantic cache     ── this repository
  └─ [4] REST API service             ── this repository
```

All five phases are implemented. Phases 0 to 2 sit behind a single interface,
`app/pipeline/port.py`, and are satisfied by `app/pipeline/real/pipeline.py`, which calls
Gemini for query enrichment and structure extraction and resolves deeplinks through the
hybrid retrieval layer. The mock that stood in during development is retained in
`app/pipeline/mock/` for offline testing.

## Request flow

```
POST /v1/troubleshoot
  └─ embed query                                     ~6 ms
     └─ semantic cache lookup (pgvector + HNSW)      ~5 ms
        ├─ hit  → re-validate → serve                pipeline never runs
        └─ miss → Phase 0-2 → validate → cache → serve
```

A cached plan is re-validated before it is served, not only when written: validation rules
can tighten, and a plan built against a different catalog can hold URIs that no longer
resolve. Nothing invalid is ever served or stored.

## Setup

Requires Docker, Python 3.12, and a Gemini API key for the Phase 0-2 pipeline.

```bash
pip install -r requirements-dev.txt
cp .env.example .env
# Set GEMINI_API_KEY in .env before continuing: Phase 0-2 calls Gemini and the
# service will not start without it. Key from https://aistudio.google.com/apikey
docker compose up -d
python -m scripts.init_db          # create schema
python -m scripts.build_catalog    # embed the 578-entry deeplink catalog
python -m scripts.prewarm_cache    # cache a plan per canonical query
python -m scripts.run_api          # http://127.0.0.1:8000
```

That one process serves both the API and the web client, so there is nothing else to
start: open <http://127.0.0.1:8000> for the UI and
<http://127.0.0.1:8000/docs> for the API.

`python -m scripts.run_api` is the supported entry point. Running uvicorn directly works
too, but the loop must be selected explicitly:

```bash
uvicorn app.api.main:app --loop asyncio
```

**On Windows this is not optional.** psycopg's async mode cannot run on the default
Proactor event loop, and the failure surfaces as a pool timeout that looks like a database
outage rather than a loop mismatch.

## Endpoints

| Method | Path | Purpose |
| :--- | :--- | :--- |
| POST | `/v1/troubleshoot` | Process a complaint, return an actionable plan |
| POST | `/troubleshoot` | Unversioned alias for the above |
| GET | `/health` | Readiness: 200 only when pool, model, indexes and catalog are live |
| GET | `/cache/stats` | Measured hit rate, latency percentiles, rejection reasons |
| GET | `/v1/examples` | The official complaints and their SIIS text, for the web client |
| POST | `/v1/resolution/sessions` | Open a guided resolution session on a validated plan |
| GET | `/v1/resolution/sessions/{id}` | Current session state and attempt history |
| POST | `/v1/resolution/sessions/{id}/presented` | Record that the action was shown or opened |
| POST | `/v1/resolution/sessions/{id}/verify` | Attempt automatic verification through the adapter |
| POST | `/v1/resolution/sessions/{id}/observations` | Submit a value the user read (client-reported) |
| POST | `/v1/resolution/sessions/{id}/confirm` | Record the user's own account of the outcome |
| POST | `/v1/resolution/sessions/{id}/advance` | Move to the next action already in the plan |
| POST | `/v1/resolution/sessions/{id}/complete` | Finish or abandon the session |
| GET | `/v1/resolution/sessions/{id}/receipt` | The record a finished session leaves behind |
| GET | `/` | The web client (served from `web/`, omitted if the directory is absent) |

```bash
curl -X POST http://127.0.0.1:8000/v1/troubleshoot \
  -H 'Content-Type: application/json' \
  -d '{"query": "My Galaxy S24 Ultra screen is completely black and will not turn on"}'
```

`siis_response` is optional and accepts either a raw string or the `{title, content}`
object the dataset ships.

`/health` is a readiness check rather than a liveness stub: a process that is listening but
whose HNSW index has not been built serves nothing, so it returns 503 naming the component
that is not ready.

## Verified resolution loop

Generating a plan answers "what should I try". It does not answer "did it work". The
resolution loop closes that gap:

```
complaint
  -> validated plan
  -> present one action, with its catalog deeplink
  -> user performs it
  -> check the resulting device state against the action's own validation contract
       verified   -> finish, and issue a receipt
       failed     -> offer the next action already in the validated plan
       unavailable-> fall back to clearly labelled user confirmation
```

The loop never invents a step. When verification fails it advances to the next step group
already present in the plan the user is walking; when the plan runs out the session ends
`unresolved`, which is a truthful outcome rather than a fabricated next move.

### What `system_verified` is allowed to mean

This is the constraint the whole feature is built around.

The catalog ships masked `bixby://` URIs, and the console is a desktop web page with no
channel to a Galaxy handset. **This deployment cannot read device state.** So:

* A `DeviceVerificationAdapter` is the only thing that can produce trusted evidence, and
  the adapter wired in production is `UnavailableAdapter`, which reads nothing and says
  so. Every automatic check therefore reports `verification_unavailable`.
* Clicking a deeplink copies it, exactly as before, and records that the action was
  *presented*. It moves no verification status. A click is not evidence about a device.
* A value the user types is recorded as `client_reported`. Even when it satisfies the
  contract, the result is `inconclusive`, never `system_verified` — the server did not
  obtain the value and cannot vouch for it.
* The user's own account is a first-class outcome, recorded as `user_confirmed` and
  labelled that way everywhere it appears.

The rule is enforced three times over: the comparator demotes a satisfied comparison that
carries untrusted evidence, the service assigns provenance by code path rather than from
the request body, and a `CHECK` constraint in `resolution_attempt` refuses to store
`system_verified` against any source other than `trusted_adapter`.

### Verification is deterministic

No language model takes part in deciding whether a check passed. `app/verification/
comparator.py` is a pure function over (contract, observation).

Automatic verification is attempted only when the step's `validationDeeplink` carries a
**complete** contract: `deeplink`, `key`, `resultType`, `condition` and `value`. Only 138
of the 578 catalog entries do. A partial contract returns `verification_unavailable`
rather than a guess at the missing half.

| Result type | `equal` | `greater` / `less` |
| :--- | :--- | :--- |
| `boolean` | yes | rejected — ordering booleans has no agreed meaning |
| `integer` | yes | yes |
| `float` | yes | yes |
| `str` | yes, case-insensitive on trimmed text | rejected |

Anything that cannot be decided safely is `inconclusive` with a reason code, never
coerced: `"maybe"` does not become `False`, `"1.5"` is not an integer, `nan` and `inf` are
refused, and a reading for a different `key` proves nothing about the expected one.

### States

```
pending ──presented/evidence──> in_progress ──┬── confirm(resolved) ─────> resolved
                                              ├── complete(status) ──────> resolved
                                              │                            unresolved
                                              │                            inconclusive
                                              ├── advance past last step ─> unresolved
                                              └── complete(abandoned) ───> abandoned
```

`resolved`, `unresolved`, `inconclusive` and `abandoned` are terminal; a terminal session
rejects every further call with `409`. Verification status moves independently through
`pending`, `system_verified`, `user_confirmed`, `verification_failed`,
`verification_unavailable` and `inconclusive`, and resets to `pending` on each advance so
one action's verdict is never inherited by the next.

A `critical` action — a forced restart, a reset, a service visit — is not presented until
the client advances with `acknowledge_critical: true`. Without it the advance is refused.

### Sessions are stored apart from the plan cache

A cached plan is shared by every request whose wording matches it, so writing one user's
progress into it would leak that progress to the next user. Each session therefore holds
its own immutable `plan_snapshot`, validated at session start. The cache may be re-warmed
or purged underneath a session without changing what it already asked the user to do.

Two tables, added by `db/migrations/004_resolution_sessions.sql`:

* `resolution_session` — the snapshot, the cursor (goal / action / step-group index),
  session and verification status, reason code, critical acknowledgement and timestamps.
* `resolution_attempt` — append-only history. A retry adds a row rather than overwriting
  the previous reading, and `observation_token` is unique per session so a client retrying
  after a timeout gets back the attempt its first call recorded.

### API example

```bash
# 1. Open a session on a plan /v1/troubleshoot already returned.
#    The plan is re-validated here: one carrying a URI the catalog does not authorise
#    is refused with 422 and never becomes a session.
curl -X POST http://127.0.0.1:8000/v1/resolution/sessions \
  -H 'Content-Type: application/json' \
  -d '{"query": "My Galaxy S24 screen is black", "plan": { "contexts": [ ... ] }}'

# 2. The user opened the deeplink. This records presentation only.
curl -X POST http://127.0.0.1:8000/v1/resolution/sessions/$SID/presented

# 3. Ask the server to read the setting back. Unavailable in this deployment.
curl -X POST http://127.0.0.1:8000/v1/resolution/sessions/$SID/verify

# 4. Or let the user report what they see. Always client-reported.
curl -X POST http://127.0.0.1:8000/v1/resolution/sessions/$SID/observations \
  -H 'Content-Type: application/json' \
  -d '{"observed_value": "True", "observation_token": "f3c1-once"}'

# 5. Not fixed? Move to the next action already in the plan.
curl -X POST http://127.0.0.1:8000/v1/resolution/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"acknowledge_critical": false}'
```

Note the absence of a deeplink field in every request. Each URI the user sees is read from
the session's stored snapshot at the session's own cursor, so a client can ask to move
forward but cannot name where it moves to.

### Receipt

A finished session leaves a record. `verification_method` is the field to read first.

```json
{
  "session_id": "2b5d1b9f-aaf6-4258-84d8-52efc9e7ab9f",
  "query": "My Galaxy S24 Ultra screen is completely black and won't turn on",
  "final_status": "resolved",
  "verification_method": "user_confirmed",
  "system_verified": false,
  "actions_attempted": 1,
  "successful_action": "Charge Device",
  "expected_state": "not applicable",
  "observed_state": null,
  "completed_at": "2026-10-03T19:36:47Z",
  "caveat": "This outcome rests on the user's own report. The server did not read the
             device state back, so it is not a system verification."
}
```

A system-verified receipt carries `"system_verified": true`, the expected and observed
states, and no caveat. The console renders the two as visually different objects — a green
card against a violet one — so one cannot be skim-read as the other.

### Connecting a real Galaxy-side adapter

Implement the `DeviceVerificationAdapter` protocol in `app/verification/adapter.py`:

```python
class GalaxyBridgeAdapter:
    name = "galaxy_bridge"

    async def read_state(self, *, deeplink: str, key: str) -> AdapterReading:
        ...  # return AdapterReading.read(value), or AdapterReading.unavailable(reason)
```

then inject it where the service is constructed in `app/api/main.py`:

```python
app.state.resolution = ResolutionService(
    pool=pool,
    permitted_deeplinks=app.state.service.permitted_deeplinks,
    adapter=GalaxyBridgeAdapter(),
)
```

Nothing else changes. The comparator, the state machine, the API and the console all
depend on the interface rather than on a device, and `automatic_verification_possible`
flips to true in the session view the moment a reading adapter is present.

**The one rule an implementation must honour:** return a reading only when one was
actually obtained. Returning a plausible value when the device could not be reached
converts an honest "unavailable" into a false "verified", which is the single failure this
whole boundary exists to prevent.

## Web client

`web/` holds a two-page client that the API mounts at `/`. There is no build step, no
package manifest and no CDN dependency: plain HTML, CSS and JavaScript served by the same
uvicorn process, so the whole system is one command and works with no network access.

`/` is an overview of what the engine does and how it is built, ending in the measured
results including the two targets that are missed. Its cache figures are read live from
`/cache/stats` on the running instance rather than written into the page. The sample
request is fired only when the visitor asks for it, because a complaint the cache cannot
answer runs the full pipeline and costs a paid model call.

`/console.html` is the working client. A complaint goes in, and the plan comes back
rendered as goals, numbered actions carrying their `auto` / `manual` / `critical` category,
and step groups whose deeplinks appear as buttons. A `bixby://` URI does not resolve in a
desktop browser, so clicking a deeplink copies it and states what it would do on-device
rather than pretending to navigate.

Beside it sits the evidence panel, and every value in it comes from the `meta` block of the
response rather than being computed in the browser: cache hit or miss with the cosine
similarity that decided it, the end-to-end latency split into embed, cache lookup and
pipeline, the model actually called, the cost, and the fallback reason when there is one.

Two details are there for the sake of an honest demo. **Ask again** re-sends the identical
complaint, which turns the previous miss into a cache hit and shows the latency collapse
directly. The reference-text panel can be emptied, which makes the engine answer
`no_siis_context` with no plan -- the specified behaviour, since steps are only ever derived
from supplied text and never invented.

Pick a complaint from the dropdown to load a real record from the participant kit; the
list is served by `/v1/examples` so the client never carries its own copy of the dataset.

Once a plan is on screen, **Start guided resolution** opens the verified resolution loop
described above. It shows one action at a time with its category and deeplink, states
plainly whether an automatic check is possible for that step, and offers the choice
between entering an observed value and confirming the outcome yourself. The status banner
and the receipt use one fixed vocabulary -- System verified, User confirmed, Verification
failed, Verification unavailable, Inconclusive -- and the page never shows "resolved"
because a deeplink was clicked.

Stylesheet and script links carry a `?v=` suffix. Without it a browser that has cached an
earlier deployment keeps serving the old asset after an update, which is how a stale
stylesheet silently hid a fixed layout bug during development.

## Layout

```
app/api/           FastAPI routes, request/response models, readiness probe
app/service.py     cache-first request flow
app/cache/         semantic cache: lookup, write, metrics, stats
app/retrieval/     hybrid deeplink retrieval (dense + BM25 fused by weighted RRF)
app/embeddings/    fastembed encoder (ONNX Runtime, no torch)
app/catalog/       catalog and query-set loading, embedding, indexing
app/validation/    deterministic contract validation
app/verification/  typed comparator, device adapter boundary, test-only fake
app/resolution/    guided resolution sessions: state machine and persistence
app/contract/      the data contract shipped with the dataset
app/pipeline/      Phase 0-2 port, plus the temporary mock behind it
db/migrations/     schema, applied in order by scripts.init_db
data/official/     the participant kit, unmodified
web/               the demo client, served by the same process as the API
```

## Key choices

**Embeddings: `BAAI/bge-small-en-v1.5` via fastembed.** 384-dimensional, MIT licensed,
67 MB, around 3 ms per query on CPU. fastembed runs it on ONNX Runtime rather than
PyTorch, which matters because each API worker holds its own copy of the model.

**Retrieval: dense vector search fused with BM25 by weighted Reciprocal Rank Fusion.** The
two fail differently. Dense search bridges vocabulary gaps, matching "blue light filter" to
Samsung's "Eye Comfort Shield"; BM25 anchors exact terminology. Their scores are on
incompatible scales, so they are merged by rank rather than by score.

**The cache requires a margin, not just a threshold.** Every query in the dataset concerns
the screen, so two genuinely different problems can be worded almost identically:
similarity between distinct problems reaches 0.8742. A threshold alone would pick
arbitrarily between them, so the best-matching plan must also beat the runner-up plan by a
configurable margin. A near-tie is treated as a miss and answered by the pipeline, because
a slow answer is better than a wrong one.

**Every phrasing of a plan is indexed, not just one key.** A reworded query is often closer
to a stored paraphrase than to the canonical form. Measured on held-out rewordings, one
moved from 0.6909 against the canonical query, which is a miss, to 0.8200 against a stored
paraphrase, which is a hit.

**PostgreSQL is the system of record.** Plan JSON, validation state, the catalog and the
embeddings stay mutually consistent in one transactional store. HNSW is in place for growth
and concurrency; at the current corpus size PostgreSQL chooses a sequential scan anyway, so
the fast-path latency comes from skipping the pipeline, not from the index.

## Tests and benchmarks

```bash
python -m pytest tests/ -q      # requires the database to be up
python -m scripts.benchmark     # writes metrics.md
```

Database-backed tests skip rather than fail when PostgreSQL is unreachable, so work on
other phases is never blocked by a container.

`metrics.md` is generated, never hand-edited. Figures it cannot measure honestly, such as
step accuracy against reference plans that do not exist yet, are written as "not measured"
with the reason rather than filled in.

### Resolution-loop tests

```bash
pytest tests/test_verification.py   # pure comparator: no database, no device, no model
pytest tests/test_resolution.py     # session machine and persistence, real database
pytest tests/test_resolution_api.py # the HTTP surface, in-process over ASGI
```

The trusted-evidence path is exercised with `FakeTrustedAdapter`, which returns only
readings a test seeded. It is never selected by `app/bootstrap.py` and is unreachable from
configuration, so the only way to obtain it is to construct it in a test.

## Configuration

All settings live in `app/config.py` and can be overridden through `.env`; see
`.env.example`. The ones most worth knowing:

| Variable | Default | Notes |
| :--- | :--- | :--- |
| `CACHE_SIMILARITY_THRESHOLD` | 0.78 | Minimum similarity for a cache hit |
| `CACHE_AMBIGUITY_MARGIN` | 0.05 | Required gap to the runner-up plan |
| `CACHE_VERSION` | 1 | Bumping it invalidates every cached plan at once |
| `DESCRIPTION_MAX_WORDS` | 15 | Follows the official sample, which exceeds the prose rule |
| `HNSW_M`, `HNSW_EF_CONSTRUCTION` | 16, 64 | Index build parameters |
| `HNSW_EF_SEARCH_CATALOG` | 100 | Higher than the cache: a miss here costs quality |
| `GEMINI_API_KEY` | _(required)_ | Phase 0-2 enrichment and extraction |

## Replacing the catalog

Cached plans embed URIs copied from whichever catalog was loaded when they were written, so
a catalog change requires a purge rather than a migration:

```bash
python -m scripts.reset_catalog --yes   # dry run without --yes
python -m scripts.build_catalog
python -m scripts.prewarm_cache
```
