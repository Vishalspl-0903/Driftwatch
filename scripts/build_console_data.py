#!/usr/bin/env python3
"""scripts/build_console_data.py -- assemble the static dataset console/ reads.

The console is a static-read demo (no backend), so this script does the one
thing a backend would otherwise do at request time: join
adjudicate/logs/run_log.csv against data/pairs/<domain>/{t0,t1}/meta.json
and evals/eyeball_notes.csv, copy the screenshots into console/public/, and
write one console/public/data/pairs.json the React app fetches at load.

Every pair's row -- boat-lifestyle.com included -- comes from its own
run_log.csv row. There used to be a hardcoded BOAT_LIFESTYLE_ESCALATION
block here that pinned that domain's queue row to a curated two-attempt
history (structural->approve, then category->flag_for_review) from earlier
prompt-version runs. It was removed: after the 2026-08-27 full-batch run
(see FAILURES.md) boat-lifestyle.com has a real logged outcome like every
other pair (that batch: parse_failed -> needs_manual_review, both VLM
attempts failed), and the console should show that, not a manual override.
The earlier two-verdict disagreement is still recorded in FAILURES.md.

Usage:
  python scripts/build_console_data.py
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

PAIRS_ROOT = Path("data/pairs")
RUN_LOG = Path("adjudicate/logs/run_log.csv")
GROUND_TRUTH_CSV = Path("evals/eyeball_notes.csv")
CONSOLE_PUBLIC = Path("console/public")
SCREENSHOTS_OUT = CONSOLE_PUBLIC / "screenshots"
DATA_OUT = CONSOLE_PUBLIC / "data" / "pairs.json"


def load_run_log() -> dict[str, dict]:
    if not RUN_LOG.exists():
        return {}
    with RUN_LOG.open(encoding="utf-8") as fh:
        return {row["domain"]: row for row in csv.DictReader(fh)}


def load_ground_truth() -> dict[str, dict]:
    with GROUND_TRUTH_CSV.open(encoding="utf-8") as fh:
        return {row["domain"]: row for row in csv.DictReader(fh)}


def load_meta(domain: str, side: str) -> dict:
    path = PAIRS_ROOT / domain / side / "meta.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def copy_screenshots(domain: str) -> dict[str, str]:
    out_dir = SCREENSHOTS_OUT / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    rel = {}
    for side in ("t0", "t1"):
        src = PAIRS_ROOT / domain / side / "screenshot.png"
        if not src.exists():
            continue
        dst = out_dir / f"{side}.png"
        shutil.copy2(src, dst)
        rel[side] = f"screenshots/{domain}/{side}.png"
    return rel


def build_pair(domain: str, run_log: dict, ground_truth: dict) -> dict:
    log_row = run_log.get(domain, {})
    t0_meta = load_meta(domain, "t0")
    t1_meta = load_meta(domain, "t1")
    gt = ground_truth.get(domain, {})

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    record = {
        "domain": domain,
        "drift_score": num(log_row.get("drift_score")),
        "path": log_row.get("path"),
        "final_action": log_row.get("final_action"),
        "policy_reason": log_row.get("policy_reason") or None,
        "t0_date": (t0_meta.get("this", {}).get("iso") or gt.get("t0_date") or "")[:10],
        "t1_date": (t1_meta.get("this", {}).get("iso") or gt.get("t1_date") or "")[:10],
        "gap_days": t0_meta.get("gap_days") or (int(gt["gap_days"]) if gt.get("gap_days") else None),
        "screenshots": copy_screenshots(domain),
        "ground_truth": {
            "fits_axis": gt.get("fits_axis"),
            "confidence": gt.get("confidence"),
            "what_changed": gt.get("what_changed"),
        }
        if gt
        else None,
        "escalation": None,
    }

    if log_row.get("evidence_axis"):
        # Exactly one evidence packet per pair: load_run_log() keeps the LAST
        # run_log.csv row per domain, so a re-run replaces rather than appends.
        # This used to be a {current_attempt, attempts: [...]} list purely so the
        # removed boat-lifestyle.com override could show two prompt versions side
        # by side; nothing in the pipeline can produce a second attempt here, so
        # the shape is flat. `description` is not carried: run_log.csv's
        # LOG_FIELDS store evidence_pointer but not the VLM's description field.
        record["escalation"] = {
            "axis": log_row.get("evidence_axis"),
            "confidence": log_row.get("evidence_confidence"),
            "evidence_pointer": log_row.get("evidence_pointer"),
            "vlm_latency_s": num(log_row.get("vlm_latency_s")),
            "vlm_input_tokens": num(log_row.get("vlm_input_tokens")),
            "vlm_output_tokens": num(log_row.get("vlm_output_tokens")),
            "vlm_cost_usd": num(log_row.get("vlm_cost_usd")),
        }

    return record


def main() -> int:
    run_log = load_run_log()
    ground_truth = load_ground_truth()

    # The queue reflects every hand-labeled pair that has been run through the
    # pipeline. That set is now the full evals/eyeball_notes.csv list (see the
    # full-batch run logged in adjudicate/logs/run_log.csv), not just the three
    # originally-documented cases. Order: run-log order isn't stable, so sort by
    # domain for a deterministic queue.
    domains = sorted(d for d in ground_truth if d in run_log)
    pairs = [build_pair(d, run_log, ground_truth) for d in domains]

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(pairs, indent=2), encoding="utf-8")
    print(f"wrote {DATA_OUT} with {len(pairs)} pairs")
    for p in pairs:
        path = p["path"] or "(no run_log.csv row -- run adjudicate/run_test.py first)"
        print(f"  {p['domain']:<24} path={path:<18} action={p['final_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
