# System Performance Metrics & Evaluation Report
**Model(s):** mock (temporary stand-in for Phase 0-2)
**Embeddings:** BAAI/bge-small-en-v1.5 (384d, ONNX Runtime)
**Environment:** 16 logical CPUs / 17 GB RAM / Windows-11-10.0.26200-SP0
**Generated:** 2026-09-28 20:04:16 by `python -m scripts.benchmark`

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
| Auto actions carrying valid actionable deeplink | >= 90% | 100.0% |
| Query variations within 8-10 distinct | informational | 100.0% |

---

## 2. Accuracy Benchmarks

| Evaluation Metric | Scale / Anchor | Score |
| :--- | :--- | :--- |
| Step accuracy (completeness, correctness, ordering) | 0.0 - 3.0 | not measured |
| Deeplink relevance (exact target screen vs. parent menu) | 0.0 - 2.0 | not measured |

**Why these are blank.** Both require reference plans to score against. The kit ships a single worked example, which is too small a sample to score, and the Phase 0-2 pipeline behind the port is still the temporary mock: it selects catalog screens by retrieval and fills a fixed template with no language understanding. Any number here would describe that template rather than the delivered system, so it is left unmeasured on purpose.

One narrow correctness check is available and does pass: retrieving from the natural-language intent "back up my data to Samsung Cloud" returns `bixby://masked/act/b3ed3ed663`, the exact URI the official `sample_output.json` pairs with its backup action.

---

## 3. Latency Benchmarks (N = 30 requests per path)

| Execution Path | Target (P95) | P50 (ms) | P95 (ms) |
| :--- | :--- | :--- | :--- |
| Cache hit - exact query match | <= 300 ms | 12.2 | 14.8 |
| Cache hit - unseen semantic paraphrase | <= 300 ms | 13.5 | 27.8 |
| Cold query - full pipeline extraction & mapping | <= 8000 ms | 596.4 | 645.5 |

The cold-path row is **not a meaningful result**. The mock pipeline sleeps for a fixed interval to stand in for model round-trip time, so that row measures the harness. It is recorded only to show the path is exercised.

Fast-path breakdown on a cache hit: embedding 6.4 ms median, cache lookup 4.6 ms median.

The paraphrase row times only the rewordings that actually hit the cache. A rewording that misses runs the pipeline, so folding it into a row labelled "cache hit" would report the cold path under a fast-path heading.

---

## 4. Operational Cost & Cache Efficacy

| Metric Item | Target | Measured Value |
| :--- | :--- | :--- |
| Cold query average inference cost | Tracked | $0.00 |
| Cache hit inference cost | $0.00 | $0.00 |
| Semantic cache hit rate (on unseen paraphrases) | >= 80% | 80% (16/20) |
| Cost derivation method | - | (prompt tokens + completion tokens) x rate |

Both cost rows are genuinely $0.00 because the pipeline behind the port is the mock and makes no paid calls. The accounting path is wired: `PipelineMeta` carries model, cost and token counts, the cache stores them per plan, and `request_metrics` records cost per request, so real figures appear as soon as a paid pipeline is bound.

**The hit-rate figure is measured against 20 rewordings this team wrote** (`data/handwritten_paraphrases.json`), one per official query. The kit ships no paraphrase ground truth. This number is therefore evidence that the cache generalises across phrasing, not an official evaluation result.

Rewordings that did not hit (ambiguous x4):
- `ambiguous` - The display on my Z Flip 7 is totally dark so I cannot see or tap anything, and I have no way to
- `ambiguous` - The folding inner display on my Flip 7 has gone blank and ignores touch, although the outside co
- `ambiguous` - Every time I unfold my Z Flip 6 the picture flickers then disappears, so I cannot reach settings
- `ambiguous` - One half of my Flip 6 display is completely dark while the other half looks normal, so I cannot 

Every miss was an ambiguity rejection rather than a similarity failure, and all of them name a Flip device. The cache holds four separate Flip plans covering a dark inner screen, a blank fold, a flickering fold and a half-dark display; a rewording of any one of them sits nearly as close to the other three, so the margin check declines to choose and runs the pipeline instead. That is the intended trade: these four are exactly the cases where answering from cache would have been a coin flip between similar plans. Removing the margin would raise this figure while allowing the wrong Flip plan to be served.

---

## 5. Architectural Ablation Analysis

| Architecture Variant | Step Accuracy | Latency P50 / P95 (ms) | Resolved | Agreement with A | Key Observations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Variant A: Hybrid BM25 + Dense | not measured | 19.4 / 39.7 | 20/20 | 100% | Shipped configuration. Weighted RRF, dense 1.0 and sparse 0.8. |
| Baseline: Dense embedding only | not measured | 18.3 / 27.6 | 20/20 | 30% | Bridges vocabulary gaps such as blue light filter to Eye Comfort Shield, but drifts to topically near screens. |
| Variant B: BM25 keyword only | not measured | 14.7 / 20.0 | 20/20 | 25% | Exact terminology only. Misses any screen whose catalog wording differs from the user's. |

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

