# Failures and fixes

A working log of things that broke, what caused them, and how they were
addressed or ruled out. This is a debugging record, not a status report —
entries stay even after the fix, so the reasoning is still here later.

## scripts/mine_wayback.py

### CDX truncation silently drops the newest captures

The archive.org CDX API's `limit` parameter truncates from the OLDEST end
of the result set. On a heavily-archived domain (tens of thousands of daily
captures), a naive daily-granularity query hits the limit before it reaches
recent captures — t1 ends up not being t1 at all, just however far back
from "now" the truncation point happened to land.

**Fix:** collapse to monthly granularity (`collapse=timestamp:6`, the
`--collapse-digits` default) — the right resolution for pair mining anyway —
and raise `--cdx-limit` to 10000. The miner also detects when a domain still
hits the limit even at monthly collapse and surfaces a `WARNING cdx
truncated` in that domain's status detail rather than silently shipping a
wrong t1.

### 403 from archive.org is ambiguous

A 403 from the CDX endpoint means either "you're being throttled, back off
and retry" or "this URL has a permanent exclusion request on file with
archive.org." There's no way to tell which from the response alone.

**Fix:** retry with exponential backoff (`Retry-After` header if present,
otherwise `5 * 2^attempt`, capped at 60s) up to `--cdx-retries` times. Only
call it a permanent exclusion (`cdx_forbidden`) if the 403 survives every
retry attempt.

### A broken render and a gutted storefront look identical downstream

A Wayback replay that lost its images or CSS (rewritten asset URLs 404,
JS-heavy pages that never finish loading) produces the same downstream
signature as a merchant who actually deleted their catalogue — both are
"the page has way less content than expected." Conflating the two would
corrupt drift labels.

**Fix:** a two-tier quality gate in `render_capture()`.
- **Hard reject** (try the next candidate capture instead): more than
  `--max-broken-image-ratio` (default 0.5) of on-page images failed to
  load, or the page has real DOM structure (>50 nodes) but zero CSS rules
  applied anywhere.
- **Soft flag** (`needs_manual_review`, not rejected): the shorter side's
  text or usable-image count falls below `--review-text-ratio` (default
  0.35) of the longer side's. A merchant genuinely shrinking their
  catalogue produces the same signature as a half-loaded replay, and only
  a human can tell them apart — so this case is recorded, not decided.

## drift/ category drift detector — signal validation (2026-08-22)

Both catalog-centroid signals (image via CLIP, text via BGE) were validated
against 23 hand-labeled real merchant pairs (5 category-drift, 9 structural,
8 none, 1 content), after two rounds of fixing the usability gates
themselves: `drift/extract.py` refuses a side outright when
`broken_image_ratio` (from `mine_wayback.py`'s own render-quality probe)
exceeds 0.3, and strips cookie-consent/parked-domain boilerplate before the
text usability gate. Both fixes were necessary before validation meant
anything — the first pass had contaminated negatives (broken renders
scoring as "stable"), and after that fix there were zero usable negative
examples for the image signal and only 2 for text. A second mining batch of
25 domains, deliberately biased toward large/established brands (banks,
FMCG, media, classifieds) likely to have simple, non-JS-heavy legacy
captures, produced 6 clean gate-passing negatives and brought the usable
negative counts up to 7 (image) and 10 (text).

- **Text signal (BGE catalog/nav centroid): rejected.** On the 10 clean,
  gate-passing negative examples spanning 20+ year site redesigns (e.g.
  bankofbaroda.in, dabur.com, hdfcbank.com, sbi.co.in,
  timesofindia.indiatimes.com — all "same business, redesigned" pairs), the
  two highest text-drift scores in the entire 23-pair set belong to
  *negative* examples, not category-drift positives. This isn't an
  extraction artifact — both pairs pass every usability gate cleanly. The
  centroid appears to measure how much a site's text/nav footprint grew
  across a redesign, not whether its category changed. That's a real
  architectural limitation of "cosine distance between BGE centroids of
  extracted catalog text," not a data problem — more mining will not fix
  it.
- **Image signal (CLIP catalog-image centroid): retained as the core
  detector.** On the same validation set (n=10: 3 category-drift positive,
  7 negative), best-threshold precision=1.00, recall=0.67. The two clean
  category-drift hits (healthkart.com, faballey.com) separate unambiguously
  from all 7 negatives; the one miss (boat-lifestyle.com) is a
  mild/adjacent category shift (audio brand into broader consumer
  electronics) sitting near the boundary, not a random failure.
- **Decision:** the image signal becomes the deterministic drift score
  driving escalation. The text signal is removed from the pipeline.
  Boundary-zone scores (between the clear-positive and clear-negative
  clusters) are explicitly the job of a VLM escalation layer, not the
  deterministic scorer — this detector is not expected to resolve
  ambiguous cases on its own.

n=10 is accepted as the validation sample given time constraints. This is
the stated confidence level for the image signal, not a placeholder pending
more mining — no further threshold tuning or targeted mining against this
specific validation is planned.

## adjudicate/ VLM escalation — boat-lifestyle.com disagreed with ground truth (2026-08-22)

The first live end-to-end run of the escalation graph on boat-lifestyle.com
(the one documented boundary case, image_drift_score=0.146) reached the VLM
and got a real, complete answer — but the wrong one. The VLM
(`qwen/qwen3.6-27b` on Groq — see the model-substitution history in
`adjudicate/nodes/vlm_evidence.py`'s docstring) returned `axis=structural,
confidence=high`, and `policy_adjudicate` correctly applied the
then-current policy (structural → `approve`) and approved the pair.

That's the wrong outcome. boat-lifestyle.com is hand-labeled ground truth in
`evals/eyeball_notes.csv` as `category` drift, medium confidence: the
merchant expanded from a pure audio brand (headphones, speakers only, 2015)
to broad consumer electronics — smartwatches, power banks, earbuds,
corporate gifting (2026). The VLM's own `evidence_pointer` for the wrong
verdict *noticed* the product change ("specific models changed... e.g.
'ROCKERZ In Ear 200' in t0 vs 'boAt Airdopes 181 Pro' in t1") but then
reasoned past it to a brand-identity conclusion: "the core business remains
selling audio and lifestyle electronics." It correctly extracted the
evidence and then applied the wrong lens to it — the model was reasoning
about whether this still felt like the same company and the same visual
category (redesign) of site, not about whether the sellable product mix had
moved into a materially different risk category. That distinction is
exactly what a category/MCC-risk read requires and a "does this look like
the same brand" read does not.

**Policy change made in response:** `adjudicate/policy.yaml` — `axis:
structural` no longer routes to `approve` at any confidence; it now routes
to `flag_for_review`, the same as it would if a human were seeing this
verdict cold. An axis label the VLM demonstrably got wrong on a real,
hand-labeled case is not something this policy auto-approves on. `approve`
is left defined but unreachable by any rule (documented inline in the YAML)
rather than deleted, so the policy is explicit that auto-approval was a
deliberate choice that got turned off, not an oversight.

**One prompt revision attempted, per instruction not to iterate further
than this:** `vlm_evidence.py`'s prompt was rewritten to frame the task
explicitly as category/MCC risk-exposure assessment from the product
catalog — "assess category/MCC risk exposure from product mix change, not
brand identity or visual redesign" — and to tell the model directly not to
let brand/visual continuity override what the product mix shows.

Three live calls were made with the revised prompt at the time (the
designated re-run, plus two attempts to refresh the logged test artifacts):
- **1 succeeded, and matched ground truth**: `axis=category,
  confidence=high`. The new evidence_pointer is materially more specific
  than the original run's: it names the actual new product verticals —
  "the t1 screenshot features a 'Shop by Categories' section explicitly
  listing 'Smart Watches', 'Dashcams', 'Projectors', 'Soundbars', and
  'Trimmers', which were absent in the t0 screenshot" — rather than
  concluding on brand continuity. Cost $0.00664, 3706 in / 1472 out
  tokens, 6.16s latency.
- **2 failed outright** with `400 json_validate_failed` before producing
  any usable JSON — one returned an empty completion, the other hit
  `max completion tokens reached before generating a valid document`
  (this model's thinking/reasoning tokens consumed the completion budget
  before it reached the final answer). Both are the same structured-output
  reliability limitation already documented in `vlm_evidence.py`'s
  docstring, not a new failure mode, and both are consistent with Groq's
  own description of `qwen/qwen3.6-27b` as a preview model.

**Update, building the console (`console/`) surfaced 2 more data points**:
regenerating `adjudicate/logs/run_log.csv` for the console's demo dataset
required re-running the pipeline on boat-lifestyle.com twice more (same
revised prompt, no code changes). Both failed the same way as above (empty
completion, `400 json_validate_failed`). Neither is a new failure mode --
recorded here so the reliability count below is complete rather than
stopping at the first three attempts because that's where the writeup
happened to pause.

**Running total across every live call made with the current model +
schema config, both prompt versions combined: 2 successes / 6 attempts
(33%)** -- 1 with the original (brand/visual-framing) prompt, 1 with the
revised (category/risk-framing) prompt, 4 failures (3 empty completion, 1
token-budget exhaustion), all with the revised prompt since that's what
every post-revision run used. This is the number the README's metrics
table cites; it will keep moving as the pipeline runs more real pairs, and
should be re-derived from actual attempts rather than assumed stable.

**Reported plainly, per instruction, not spun:** the revised prompt got the
right answer on its one clean pass, on the one case that mattered most to
get right. It is a single data point, not a validated fix, and the full
run history shows this model's structured-output reliability is low enough
that most attempts don't produce a usable answer at all. The policy
change (structural → flag_for_review) is what's actually load-bearing here,
not the prompt revision — it means a wrong or unreliable VLM verdict on
this axis still reaches a human rather than auto-approving, regardless of
which of these outcomes any given real call lands on. No further prompt
iteration was done, per instruction.

## adjudicate/ -- a parse failure had no logged outcome (2026-08-23)

### structure_output raised into a dead end; only the test runner's own try/except gave failed pairs any logged row at all

Traced, not assumed: `structure_output.py -> policy_adjudicate` in
`graph.py` was a plain, unconditional `add_edge`. LangGraph aborts the
whole run when a node raises, so when `structure_output.py` raised
`SchemaValidationError` on a parse failure (which it did on every one of
the 4 real failures behind the 2/6 reliability number above),
`policy_adjudicate` never ran and the pair got no final action at all.

The only reason those 4 failures show up in `adjudicate/logs/run_log.csv`
today is that `adjudicate/run_test.py` happened to wrap `app.invoke()` in
its own `try/except` and manually logged the exception -- a test-runner
convenience, never something the graph itself guaranteed. Confirmed
against the actual row on disk before changing anything:
`boat-lifestyle.com,0.146,graph_error,...` has a **blank `final_action`
field** -- `path` said something failed, but nothing said what to do
about it. Any other real caller of `get_app().invoke(...)` (the graph's
actual public interface, not the test script) would get an uncaught
exception and the pair would leave zero trace in `run_log.csv` -- only a
raw text dump in `schema_failures.log`, which isn't a queue entry.

**Fix:** `structure_output.py` no longer raises. On any failure -- the VLM
call itself failing, or a response that doesn't validate against the
evidence schema -- it now returns a normal state update
(`path="parse_failed"`, `action` = `policy.yaml`'s new
`parse_failure_action`, currently `needs_manual_review`) and logs it
itself, the same self-logging pattern `escalation_check.py` already uses
for its own terminal paths. `graph.py` gained a conditional edge after
`structure_output`: `parse_failed` routes straight to `END`; anything else
proceeds to `policy_adjudicate` as before. The fallback action name lives
in `policy.yaml`, not hardcoded in Python, for the same reason every other
action this project hands out is config-driven, not a model or code
decision. **A parse failure now has a defined, logged, safe outcome -- it
never disappears and never defaults to approve**, regardless of how the
graph is called.

### One retry added to vlm_evidence; re-measured, both numbers reported

`vlm_evidence.py` now retries exactly once (`MAX_ATTEMPTS = 2`, not a
loop) when an attempt doesn't produce a response that validates -- covers
both failure modes actually observed: the API call itself throwing, and
the rarer case (seen once in pre-ship testing) of a 200 response whose
content doesn't parse. This is a retry decision, made in `vlm_evidence.py`
only; the safe-failure routing above is a separate, unconditional policy
decision that fires regardless of how many attempts were made.

Re-measured on 5 fresh, real pipeline invocations of boat-lifestyle.com
with the retry-enabled code (`adjudicate/logs/run_log.csv`, 2026-08-23):

| | successes | out of | rate |
|---|---|---|---|
| **Without retry** (original single-attempt code; the 2/6 figure above) | 2 | 6 attempts | 33% |
| **With retry** (current code, this batch) | 3 | 5 pipeline runs | 60% |

Both are real, small-sample measurements -- not fixed rates, and not
directly comparable sample sizes. What's worth noting: the underlying
*per-call* reliability did not change. Of the 9 raw API calls inside the
5 with-retry runs (1 run succeeded on its first call, 2 runs failed their
first call but succeeded on the retry, 2 runs failed both calls), 3
succeeded -- also 33%, consistent
with the no-retry baseline. The retry doesn't make any single call more
reliable; it gives the pipeline a second independent draw, which is why
the pipeline-level success rate moved from roughly 1-in-3 to roughly
3-in-5 while the per-call rate held steady. All 3 successes in this batch
independently agreed with each other and with hand-labeled ground truth
(`axis=category, confidence=high`).
