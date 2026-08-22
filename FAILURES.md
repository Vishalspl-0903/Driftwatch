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
