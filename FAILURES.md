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

## Full-batch scale-up run -- 23 hand-labeled pairs against the frozen system (2026-08-27)

Ran every pair in `evals/eyeball_notes.csv` (17 from the first 50-domain
batch, 6 from the second 25-domain batch) through `drift/score.py` + the
full `adjudicate/` graph in one pass -- `scripts/run_full_batch.py`, same
mechanics as `adjudicate/run_test.py` scaled up. No thresholds, prompts,
policy rules, or detector logic changed; this was a scale-up against the
frozen system, not a tuning run. All 23 outcomes are in
`adjudicate/logs/run_log.csv` (rows dated 2026-08-27); the per-pair report
is `adjudicate/logs/full_batch_summary.md`.

### Outcome distribution (n=23)

| outcome | n | share |
|---|---|---|
| `insufficient_data` | 12 | 52% |
| `parse_failed` (-> `needs_manual_review`) | 5 | 22% |
| `no_action` | 3 | 13% |
| `auto_flag` | 3 | 13% |
| `escalated` (VLM verdict adjudicated) | 0 | 0% |

Only 11 of 23 pairs produced a usable deterministic image-drift score at
all; the other 12 tripped `drift/extract.py`'s `broken_image_ratio > 0.3`
gate on at least one side and landed on `insufficient_data`. That 52% is
the dominant fact of this run: on a real, unfiltered set of Wayback pairs,
most did not clear the render-quality bar the detector needs. This is the
`mine_wayback.py` REVIEW-flag population (`img_ratio` / `text_ratio` low)
from `data/mine_wayback_log.csv` showing up downstream exactly as the
extract gate's docstring says it should -- the detector refusing to score a
render it can't trust, not a bug.

### Real escalation rate at this n

5/23 = **22%** of all pairs reached the VLM. Measured only against the 11
pairs with a usable score, it's 5/11 = **45%** -- the escalation band
(0.14-0.22) caught nearly half of everything the deterministic scorer could
actually score. The 5: boat-lifestyle.com (0.146), timesofindia (0.144),
dabur.com (0.1837), dailyobjects.com (0.2027), bankofbaroda.in (0.2051).

### Real VLM reliability at this n -- 0 successes

**Every VLM call in this batch failed.** Per-call: 0/10 raw API calls
succeeded (5 pipeline escalations x 2 attempts each, retry always used).
Pipeline-level: 0/5. Every failure was Groq `400 json_validate_failed` on
`qwen/qwen3.6-27b` -- 9 with an empty `failed_generation` (the empty-completion
mode), 1 with `max completion tokens reached before generating a valid
document` (the reasoning-token-budget mode). Both are the exact failure
modes already documented in `vlm_evidence.py`'s docstring and the
2026-08-22/23 entries above; nothing new, just all of it at once.

**Cumulative reliability -- stated as two separate figures, because only one
of them is auditable.** An earlier draft of this entry fused every era into
"~3 successes / 19 attempts (~16%)". That number was wrong: it mixed
measurement eras whose rows no longer coexist in any one file, and it
reconciles with neither the log on disk nor the prose above. Corrected:

| source | successes / raw calls | rate | auditable? |
|---|---|---|---|
| `adjudicate/logs/run_log.csv` as it exists on disk (2026-08-24 -> 2026-08-27, this batch included) | **3 / 16** | **19%** | yes -- re-derive with `vlm_attempts` vs `path=='escalated'` |
| pre-retry era, 2026-08-22 (prose above) | 2 / 6 | 33% | no -- rows lost |
| retry era, 2026-08-23 (prose above) | 3 / 9 | 33% | no -- rows lost |

The two prose eras predate the current `run_log.csv`: the log was
regenerated while building the console (see that entry above), so its
earliest surviving row is 2026-08-24 and the 08-22/08-23 calls cannot be
re-derived from it. They are left as recorded prose rather than folded into
a single cumulative number that no file could support. Summing all three
eras would give 8/31 (26%), but that sum assumes the three sets are
disjoint, which cannot be verified -- so it is not the number this project
cites.

**The figure to cite is 3/16 (19%) from the log**, and the direction is the
part that matters more than the decimal: the earlier 33% per-call estimate
was a small sample, this batch (0/10) pulls it down, and the honest read is
that this model's structured-output path is not dependable enough to rely
on for any single pair. The retry (a second independent draw) is worthless
when the underlying success rate is this close to zero -- 0/5 pipelines
despite 10 draws.

**What actually held: the safety net.** All 5 failed escalations routed to
`needs_manual_review` via `structure_output.py` + the `graph.py` conditional
edge -- logged, terminal, non-approving, exactly as the 2026-08-23 entry
promised. Zero pairs were lost, zero defaulted to approve, zero raised an
uncaught exception. The VLM layer is currently non-functional and the
system degraded to "send the boundary cases to a human," which is the
designed fallback.

### Cost and latency across escalated calls

Total cost: **$0.00** -- Groq does not bill `400 json_validate_failed`, and
no call produced usable output to bill for. Latency per pipeline escalation
(each = 2 failed attempts): **35.3s, 35.4s, 37.6s, 59.3s, 76.4s** -- range
35.3s-76.4s. These are failure latencies, not the 5-6s a clean success has
historically taken; the model burns wall-clock time producing nothing on
this failure mode, and the retry doubles it.

### Final action vs. hand-labeled ground truth -- disagreements

Three pairs where the frozen system's action disagrees with
`evals/eyeball_notes.csv`:

1. **hdfcbank.com -- false positive (hard disagreement).** Ground truth:
   `structural`, medium confidence -- "Banking site both sides... same
   business," a clean 24-year redesign. Image-drift score **0.2489**, above
   the 0.22 HIGH threshold, so the pipeline `auto_flag`ged it with no VLM
   check (the "precision=1.00 zone" from the n=10 validation). This is the
   first real auto_flag false positive on record. The eyeball note already
   flagged lower confidence here ("t1 has partial render gap... despite
   image_ratio=0.505 passing gate"), so the render is borderline -- but it
   passed every gate and produced a high-confidence-zone score on a pair
   that is not category drift. The CLIP catalog-image centroid can be moved
   past the HIGH line by a big enough visual overhaul of a content-dense
   site (bank homepage: tiles, product cards, imagery all replaced), which
   is exactly the "measures how much the footprint changed, not whether the
   category changed" limitation that got the *text* signal cut in the
   2026-08-22 entry -- here it's the image signal showing a milder version
   of the same failure at the top of its range, not just the boundary.

2. **wakefit.co -- miss (false negative).** Ground truth: `category`, high
   confidence -- mattress-only DTC (2017) -> full home-furniture retailer
   with 150+ stores (2026), "clean, large category expansion... exactly
   what re-review should catch." Outcome: `insufficient_data` -- one side's
   render failed the `broken_image_ratio` gate, so no score, no escalation,
   no flag. A real category pivot produced no signal at all.

3. **wellbeingnutrition.com -- miss (false negative).** Ground truth:
   `category`, medium confidence -- effervescent wellness drinks (2020) ->
   whey protein / collagen / kids nutrition (2026), a move into a different
   regulatory/risk profile. Outcome: `insufficient_data`, same cause as
   wakefit.co. Another real category pivot with no signal.

Both misses are the same root cause as the 52% `insufficient_data` rate:
the render-quality gate is doing its job (refusing an untrustworthy render)
but the cost is that genuine category drift on a badly-replayed pair is
invisible to this pipeline. The detector has no path between "good enough
render -> score it" and "bad render -> insufficient_data"; a category pivot
hiding behind a broken replay is indistinguishable from a stable business
behind a broken replay, and both get the same non-answer.

### Softer notes (not counted as disagreements)

- **boat-lifestyle.com** (ground truth `category`): the one documented
  boundary case. When the VLM worked (prior runs), it resolved to
  `category, high -> flag_for_review`. This run its 2 calls both failed, so
  it landed on `needs_manual_review`. Still reaches a human. (The console
  queue row initially still showed `flag_for_review` from a hand-maintained
  `BOAT_LIFESTYLE_ESCALATION` block in `build_console_data.py`; that block
  was removed on 2026-08-27 -- see the console entry below -- so the row now
  shows this run's `parse_failed` / `needs_manual_review` like every other
  pair. The earlier two-verdict disagreement is still recorded above.)
- **bankofbaroda.in, dabur.com, timesofindia.indiatimes.com** (all ground
  truth `structural`, high confidence -- clean redesigns of content-dense
  bank/news sites): all three scored into the 0.14-0.22 escalation band and
  would have gone to the VLM for a `structural` vs `category` call. Because
  every VLM call failed, all three got `needs_manual_review` instead. Not a
  hard disagreement (a human-review punt on a clean pair is conservative,
  not wrong), but three clean redesigns hitting the escalation band on top
  of the hdfcbank.com auto_flag is the same signal: **the image score is
  responding to redesign magnitude on content-dense sites**, pushing them
  up into and past the escalation zone. n is small, but 4 of the 6
  large-brand redesign pairs from the second mining batch scored >= 0.14.
- **dailyobjects.com** (ground truth `structural`, mild): scored 0.2027,
  escalated, VLM failed -> `needs_manual_review`. Consistent with the above.

## Re-mine attempt for the two broken-render category misses -- both unrecovered (2026-08-27)

The full-batch run above left two hand-labeled category-drift merchants
(`wakefit.co`, `wellbeingnutrition.com`) with no score at all: on each, the
t1 render's `broken_image_ratio` exceeded `drift/extract.py`'s 0.3 usability
gate, so `score.py` returned `None` and the pair stopped at
`insufficient_data` -- a real category pivot made invisible by a bad Wayback
replay. This was a targeted per-domain re-mine check: does a cleaner capture
of either side exist near the original date? No thresholds, gates, or
detector logic were changed -- the mine gate (`--max-broken-image-ratio`
0.5) and the drift gate (0.3) both stayed at their documented values.

**What was on disk (mine-time `broken_image_ratio`):**
- `wakefit.co`: t0 `20170702140100` = 0.0 (clean), t1 `20260618033818` =
  0.474. t0 is fine; t1 is the broken side. The miner's own attempt #1 had
  already tried a wider-gap t1 (`20260815084054`) and rejected it at the
  mine gate.
- `wellbeingnutrition.com`: t0 `20200217062857` = 0.061 (clean), t1
  `20260421033211` = 0.5. Same shape -- t0 fine, t1 broken, attempt #1's
  wider t1 (`20260516014224`) already mine-gate-rejected.

**What was tried:** refreshed the CDX list for both (`--refresh-cdx`,
2026-08-27 -- a handful of new 2026 captures had appeared), then rendered
every candidate t1 capture from mid-2025 through the newest available (9
per domain) through `mine_wayback.py`'s own `render_capture` with the
quality gate disabled, purely to *measure* each one's `broken_image_ratio`
(diagnostic only -- produced no dataset pair). t0 was not re-probed on
either domain; both t0 renders are already clean and are not the problem.

**What happened -- no candidate clears the 0.3 drift gate on either domain:**

| domain | candidates rendered (mid-2025 → newest) | best `broken_image_ratio` | 
|---|---|---|
| `wakefit.co` | 8 (+1 nav-timeout) | **0.384** (`20250621023301`) |
| `wellbeingnutrition.com` | 6 (+3 nav-timeout) | **0.364** (`20260421033211`, the capture already on disk) |

Every other `wakefit.co` candidate landed 0.54–0.87; every other
`wellbeingnutrition.com` candidate landed 0.42–0.68. The single best
`wakefit.co` alternative (0.384) is still above 0.3 *and* is a June-2025
capture -- a full year earlier than the original t1, which would shorten
the pair's gap from 3272d to ~2911d and change what the pair measures. For
`wellbeingnutrition.com` the lowest ratio observed was the capture that's
*already on disk*.

Render quality is also not deterministic: the two on-disk t1 captures were
mined at 0.474 / 0.5 but re-probed this run at 0.538 / 0.364 -- roughly
±0.1 run to run, as `wm-`/CDN-hosted lazy-loaded product imagery 404s a
different subset each replay. Even a lucky low-variance re-render of the
best candidate would sit right at the 0.3 line, not safely under it, and
chasing that variance is not a recovery.

**Result: both remain unrecovered misses.** No re-mine was performed (no
candidate would have passed the gate), so nothing downstream changed --
`data/pairs/`, `run_log.csv`, `evals/run_eval.py`'s numbers (still
precision 1.00 / recall 0.67 / n=10), and the README metrics table are all
unchanged. The root cause stands as described in the full-batch entry
above: these are modern JS-heavy storefronts whose Wayback captures never
replay their catalog imagery cleanly, and the gate correctly refuses to
score a render it can't trust. `wakefit.co` and `wellbeingnutrition.com`
stay in `evals/eyeball_notes.csv` as hand-labeled category drift that this
pipeline cannot currently see.

(The refreshed CDX caches were left in place -- they're gitignored and
regenerate on purpose. `status.json` for both domains was restored to its
pre-check `ok` state afterward; the `--refresh-cdx --dry-run --force`
candidate check had rewritten it to `dry_run_pair_found` as a side effect,
with the underlying pair files untouched.)

## Console -- removed the hardcoded boat-lifestyle.com escalation override (2026-08-27)

`scripts/build_console_data.py` had a `BOAT_LIFESTYLE_ESCALATION` constant
that pinned that one domain's queue row and detail view to a curated
two-attempt history (attempt 1: `structural` → `approve` under the old
policy; attempt 2: `category` → `flag_for_review`), overriding whatever was
in `run_log.csv`. It existed so the console could show the original
prompt-version disagreement (see the 2026-08-22 entry above).

After the 2026-08-27 full-batch run, boat-lifestyle.com has a real logged
outcome like every other pair (`parse_failed` → `needs_manual_review`, both
VLM attempts failed). The override was removed: the queue row and detail
view now come from that domain's `run_log.csv` row via the same code path
as the other 22 pairs, with no manual data. Regenerated
`console/public/data/pairs.json` (23 pairs) and confirmed boat-lifestyle.com
now shows `path=parse_failed`, `final_action=needs_manual_review`,
`escalation=null`. The earlier two-verdict story is preserved here in
FAILURES.md; it's no longer hand-injected into the console dataset. (The
`DetailView.jsx` "went through VLM escalation twice" callout is now
unreachable -- it keyed on `attempts.length > 1`, which no pair produces
anymore -- but it's harmless dead UI and the React app was left untouched.)
