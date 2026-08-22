#!/usr/bin/env python3
"""evals/run_eval.py -- validate the category drift detector against the
17 hand-labeled ground-truth pairs in evals/eyeball_notes.csv.

This is a sanity check, not a benchmark: n=13 in the category-vs-none
comparison (5 category-drift pairs + 8 none pairs; the 3 structural pairs and
1 content pair are outside that binary comparison by definition, not because
they're low quality). Precision/recall/threshold numbers below are not a
validated metric and should not be read, cited, or reused as one anywhere
outside this sanity check.

Usage:
  python -m evals.run_eval
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.score import score_pair  # noqa: E402

GROUND_TRUTH_CSV = Path("evals/eyeball_notes.csv")
PAIRS_ROOT = Path("data/pairs")

CATEGORY_AXIS = "category"
NONE_AXIS = "none"


def load_ground_truth(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def best_threshold(scores: list[tuple[str, float, bool]]) -> tuple[float | None, dict]:
    """scores: (domain, score, is_category) for usable pairs only.
    Sweeps midpoints between consecutive sorted scores, returns the threshold
    maximizing accuracy (ties broken toward the lower threshold, i.e. more
    recall-favoring), plus precision/recall/accuracy at that point.
    is_category=True is the positive class (score expected to be higher)."""
    if len(scores) < 2:
        return None, {}
    values = sorted(s for _, s, _ in scores)
    candidates = [(values[i] + values[i + 1]) / 2 for i in range(len(values) - 1)]
    candidates = [values[0] - 1e-6] + candidates + [values[-1] + 1e-6]

    def eval_at(thr: float) -> dict:
        tp = fp = tn = fn = 0
        for _, s, is_cat in scores:
            pred_positive = s >= thr
            if is_cat and pred_positive:
                tp += 1
            elif is_cat and not pred_positive:
                fn += 1
            elif not is_cat and pred_positive:
                fp += 1
            else:
                tn += 1
        acc = (tp + tn) / len(scores)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        return {"threshold": thr, "tp": tp, "fp": fp, "tn": tn, "fn": fn, "accuracy": acc,
                 "precision": precision, "recall": recall}

    best = max((eval_at(t) for t in candidates), key=lambda r: (r["accuracy"], -r["threshold"]))
    return best["threshold"], best


def main() -> int:
    rows = load_ground_truth(GROUND_TRUTH_CSV)
    print(f"driftwatch category-drift eval | {len(rows)} ground-truth pairs\n")

    results = []
    for row in rows:
        domain = row["domain"]
        print(f"scoring {domain} ...", file=sys.stderr)
        score = score_pair(domain, pairs_root=PAIRS_ROOT)
        results.append((row, score))

    # --- per-pair table -----------------------------------------------------
    header = f"{'domain':<24} {'fits_axis':<11} {'image_score':>11} {'text_score':>10}  usable_extraction"
    print(header)
    print("-" * len(header))
    for row, score in results:
        img = f"{score.image_drift_score:.4f}" if score.image_drift_score is not None else "n/a"
        txt = f"{score.text_drift_score:.4f}" if score.text_drift_score is not None else "n/a"
        usable_bits = []
        usable_bits.append("img" if score.image_pair_usable else "")
        usable_bits.append("txt" if score.text_pair_usable else "")
        usable = "+".join(b for b in usable_bits if b) or "none"
        print(f"{row['domain']:<24} {row['fits_axis']:<11} {img:>11} {txt:>10}  {usable}")

    # --- category (5) vs none (8) comparison --------------------------------
    print("\n" + "=" * 78)
    print("CATEGORY (5) vs NONE (8) SEPARATION CHECK  --  n=13, sanity check only, NOT a validated metric")
    print("=" * 78)

    comparison = [(r, s) for r, s in results if r["fits_axis"] in (CATEGORY_AXIS, NONE_AXIS)]
    assert len(comparison) == 13, f"expected 13 category+none pairs, found {len(comparison)}"

    for signal_name, score_attr, usable_attr in (
        ("image_drift_score", "image_drift_score", "image_pair_usable"),
        ("text_drift_score", "text_drift_score", "text_pair_usable"),
    ):
        print(f"\n--- signal: {signal_name} ---")
        usable_scores = []
        excluded = []
        for row, score in comparison:
            val = getattr(score, score_attr)
            usable = getattr(score, usable_attr)
            is_cat = row["fits_axis"] == CATEGORY_AXIS
            if usable and val is not None:
                usable_scores.append((row["domain"], val, is_cat))
            else:
                excluded.append(row["domain"])

        cat_vals = sorted(v for _, v, is_cat in usable_scores if is_cat)
        none_vals = sorted(v for _, v, is_cat in usable_scores if not is_cat)
        print(f"  usable pairs: {len(usable_scores)}/13  (excluded, insufficient extraction: {excluded or 'none'})")
        print(f"  category scores ({len(cat_vals)}): {[round(v, 4) for v in cat_vals]}")
        print(f"  none scores     ({len(none_vals)}): {[round(v, 4) for v in none_vals]}")

        if len(cat_vals) >= 1 and len(none_vals) >= 1:
            separates = min(cat_vals) > max(none_vals)
            print(f"  cleanly separable at any threshold: {separates}")
            thr, stats = best_threshold(usable_scores)
            if thr is not None:
                p = f"{stats['precision']:.2f}" if stats["precision"] is not None else "n/a"
                r_ = f"{stats['recall']:.2f}" if stats["recall"] is not None else "n/a"
                print(
                    f"  best-sweep threshold={thr:.4f}  accuracy={stats['accuracy']:.2f}  "
                    f"precision={p}  recall={r_}  (tp={stats['tp']} fp={stats['fp']} "
                    f"tn={stats['tn']} fn={stats['fn']})"
                )
        else:
            print("  not enough usable pairs on both sides to compute separation")

    # --- per-pair ambiguity / failure attribution ---------------------------
    print("\n" + "=" * 78)
    print("PER-PAIR NOTES  --  extraction issue vs. possible genuine detector disagreement")
    print("=" * 78)
    for row, score in results:
        flags = []
        for side in (score.t0, score.t1):
            for note in side.notes:
                flags.append(f"{side.side}: {note}")
            if side.render_broken_image_ratio is not None and side.render_broken_image_ratio > 0.4:
                flags.append(f"{side.side}: render broken_image_ratio={side.render_broken_image_ratio} (mine_wayback.py quality probe)")
        if flags:
            print(f"{row['domain']} [{row['fits_axis']}]:")
            for f in flags:
                print(f"    {f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
