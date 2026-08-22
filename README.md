# Driftwatch

Driftwatch mines paired `t0`/`t1` snapshots of Indian merchant storefronts from
the Wayback Machine and produces a dataset for studying how a storefront
changes over time — category shifts, content rewrites, trust-signal changes,
structural redesigns, or no meaningful drift at all.

The dataset unit is a **pair**: two renders of the same homepage, separated by
enough time that a real business pivot could plausibly have happened, each
captured as a full-page screenshot plus rendered DOM.

## Pipeline

```
data/domains.txt  -->  scripts/mine_wayback.py  -->  data/pairs/<domain>/{t0,t1}
                                                  -->  evals/ (manual review)
```

1. **`scripts/mine_wayback.py`** queries the archive.org CDX API per domain,
   filters out non-content captures (parked pages, non-200s, truncated
   grabs), picks the widest-gap viable pair, and renders both sides through
   Playwright against the Wayback replay. A two-tier quality gate rejects
   renders with broken replay assets (>50% failed images) or no applied CSS,
   and flags — but does not reject — pairs with a low text/image retention
   ratio, since a merchant genuinely shrinking their catalogue looks the same
   as a half-loaded replay and both need a human to tell apart.
2. **`scripts/build_eyeball_review.py`** turns every successfully mined pair
   into one static HTML page: t0/t1 screenshots side by side, gap in days,
   and quality-gate flags on top, followed by a copy-pasteable CSV row for
   manual judgment.
3. Judgments are recorded by hand in **`evals/eyeball_notes.csv`**.

## Setup

```
pip install -r requirements.txt
playwright install chromium
```

For the drift detector (`drift/`, `evals/run_eval.py`), also install
`requirements-drift.txt` — see that file for the CLIP/BGE model dependencies
and the CPU-vs-GPU torch install note.

## Usage

Mine the full target list:

```
python scripts/mine_wayback.py data/domains.txt --collapse-digits 6 --cdx-limit 10000
```

Useful flags: `--only <domain>` to re-run a single domain, `--dry-run` for
CDX-only candidate selection with no browser, `--force` to re-mine a domain
already marked `ok`, `--max-gap-days` to cap the pair to a specific rescan
interval instead of the widest available gap. Every domain ends with exactly
one status written to `data/pairs/<domain>/status.json` and appended to
`data/mine_wayback_log.csv` — nothing fails silently.

Generate the review page after a run:

```
python scripts/build_eyeball_review.py --out evals/eyeball_review.html
```

Open the HTML file locally and fill in `evals/eyeball_notes.csv` as you go
(`domain,t0_date,t1_date,gap_days,what_changed,fits_axis,confidence,notes`).

## Data layout

```
data/
  domains.txt          target list — see the file header for bucket definitions
  cdx_cache/            cached CDX responses, keyed by domain (gitignored)
  pairs/<domain>/
    status.json          one terminal status per domain: ok, cdx_forbidden,
                          no_viable_captures, all_candidate_pairs_failed, ...
    t0/, t1/
      screenshot.png      full-page render
      dom.txt, dom.html   rendered text and HTML
      meta.json           timestamps, gap, quality metrics, review flags
  (pairs/, cdx_cache/, and the run log are gitignored — regenerate via the miner)
evals/
  eyeball_notes.csv     hand-filled manual review
  eyeball_review.html   generated viewer (gitignored, regenerate as needed)
```

## Target list

`data/domains.txt` holds ~50 Indian merchant domains across four buckets:

| Bucket | Count | Purpose |
|---|---|---|
| Small/mid D2C | 20 | Instagram-native / Shopify-lite storefronts likely to have decent Wayback coverage |
| Drift-prone | 10 | Nutraceutical/supplement D2C, lending-adjacent fintech, crypto — categories where real pivots happen |
| Stable control | 10 | Large established merchants, expected to show minimal drift |
| Buffer | 10 | Extra slots to absorb replay failures (JS-lazy-load pages, broken asset replay, etc.) |

## Drift detector (`drift/`)

Given a mined pair, `drift/` scores how much a storefront's catalog changed
between t0 and t1: `extract.py` pulls candidate product images and
catalog/nav text out of the render (with a usability gate that refuses a
side outright when its render was too broken to trust — see FAILURES.md),
`fetch.py` resolves image candidates to actual bytes, `embed.py` turns
images and text into vectors, and `score.py` reports the cosine distance
between t0 and t1 centroids. `evals/run_eval.py` validates both candidate
signals against hand-labeled ground truth in `evals/eyeball_notes.csv`.

**Validated result:** the image signal (CLIP catalog-image centroid) is the
detector — on 23 hand-labeled real pairs it separates 2 of 3 usable
category-drift positives cleanly from all 7 usable negatives (precision
1.00, recall 0.67 at best threshold, n=10). The text signal (BGE
catalog/nav-text centroid) was rejected: it measures how much a site's
text/nav footprint grew across a redesign, not whether its category
changed, and was dropped after clean structural-redesign negatives
outscored every category-drift positive. Full validation write-up,
including the two gate fixes that got the negative-example sample clean
enough to trust, is in `FAILURES.md`.

## Current dataset status

Latest full run against all 50 domains: **17 usable pairs**, 28 domains where
every candidate pair failed the render/quality gate, 2 CDX exclusions
(`cdx_forbidden`), and 3 domains with no viable candidate pair. See
`data/mine_wayback_log.csv` for the per-domain breakdown and each domain's
`status.json` for the full attempt history.
