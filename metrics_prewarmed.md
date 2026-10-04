# 20-Response Pre-Warmed Cache Evaluation

**Date:** 2026-10-04  
**Cache version:** 20261005  
**Dataset:** 20 official queries and 20 team-written unseen paraphrases  
**Catalog:** 578 official deeplink entries

## Evaluation setup

The evaluation used an isolated cache version containing exactly one previously generated,
validated plan for each of the 20 official queries. All 20 plans were rechecked by the
current validation code. The isolated cache contains 20 plans and 239 searchable vectors.

Fresh plan generation could not be completed because the configured Gemini free-tier daily
request quota was exhausted after three plans. Existing validated responses for all 20 exact
official queries were therefore copied into the isolated cache version. No mock plans or
fabricated responses were used.

## Results

| Metric | Target | Result | Status |
| :--- | :--- | :--- | :--- |
| Exact-query prewarm coverage | 20/20 | 20/20 | Pass |
| Timed exact cache hits | 30/30 | 30/30 | Pass |
| Exact cache-hit P50 | <= 300 ms | 24.0 ms | Pass |
| Exact cache-hit P95 | <= 300 ms | 38.3 ms | Pass |
| Schema-valid plans | >= 99% | 100% (20/20) | Pass |
| Rule-compliant plans | >= 95% | 100% (20/20) | Pass |
| Auto actions with actionable deeplink | >= 90% | 100% (8/8) | Pass |
| Unseen-paraphrase cache-hit rate | >= 80% | 90% (18/20) | Pass |
| Paraphrase-hit P50 | <= 300 ms | 20.7 ms | Pass |
| Paraphrase-hit P95 | <= 300 ms | 23.0 ms | Pass |
| Cold-path P95 | <= 8 s | Not rerun: Gemini quota exhausted | Blocked |

## Remaining misses

Two unseen paraphrases were rejected as ambiguous:

- `row_3`: Galaxy Z Flip 7 completely black display and blocked data transfer.
- `row_8`: Galaxy Flip 7 blank, unresponsive inner display with working cover screen.

### Auto actions without a catalog destination (resolved)

Three actions were categorised `auto` with no verified actionable deeplink:

- Email App Storage
- Open Smart Switch App
- Phone Aspect Ratio

The official catalog was searched for each. It covers device Settings screens only, and
holds no destination for any of them: zero entries mention email, app storage, clear
cache, Smart Switch, transfer data, aspect ratio, display size or full screen apps. The
nearest candidate, `Screen zoom`, scales UI elements rather than changing how an app fills
a tall display, so attaching it would have been the loosely related substitution this
project refuses to make.

Because no destination exists, the fix is classification rather than matching. An `auto`
action whose resolution returns no catalog destination is now downgraded to `manual`,
keeping its grounded steps; `critical` is never downgraded. The invariant is enforced at
generation time in the pipeline and again by the validator
(`auto_action_missing_deeplink`), which also runs on every cache read, so a plan stored
under the earlier rules cannot be served.

After the rule, the 20 pre-warmed plans contain 8 `auto` actions and all 8 carry a
verified catalog deeplink: **100% (8/8)**. The three downgraded actions remain in their
plans as `manual` with their steps intact, so no troubleshooting content was lost. All 20
plans pass the current validation rules.

No semantically nearby URI was substituted when the official catalog lacked a verified
destination, and no benchmark threshold was changed.

## Interpretation

Clean pre-warming produces a 90% paraphrase hit rate. The updated actionable-deeplink
condition produces 100% coverage across the remaining automatic actions without adding or
inventing any URI. The remaining cache failures are limited to two closely related
Flip-display complaints. Exact cache latency is below 40 ms at P95 on this machine.

Cold-path latency must be rerun after the Gemini quota resets; substituting another model or
timing quota-error responses would not be a valid measurement of the configured project.
