#!/usr/bin/env python3
"""scripts/build_console_data.py -- assemble the static dataset console/ reads.

The console is a static-read demo (no backend), so this script does the one
thing a backend would otherwise do at request time: join
adjudicate/logs/run_log.csv against data/pairs/<domain>/{t0,t1}/meta.json
and evals/eyeball_notes.csv, copy the screenshots into console/public/, and
write one console/public/data/pairs.json the React app fetches at load.

boat-lifestyle.com is a special case, not a shortcut: it is the one pair
that went through the VLM escalation path twice with two different prompt
versions (see FAILURES.md), and the console is explicitly meant to show
that disagreement, not just the latest result. Its escalation history below
is reconstructed from the real logged output of both runs -- attempt 1's
numbers are what shipped in the "feat: swap vlm_evidence.py ... Groq"
commit message and FAILURES.md, attempt 2's are from the "fix: stop
auto-approving structural VLM verdicts" commit and FAILURES.md. Both are
real API responses that were captured and reported at the time; this script
does not call any model. Live re-runs of the current code have had a ~2/4
failure rate on this exact model/prompt combination since (also documented
in FAILURES.md) -- attempt 2's numbers are the one clean success obtained,
not cherry-picked from a larger set of successes.

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

# Real, previously-captured results -- see the module docstring. Not fetched
# live; this script makes no network calls.
BOAT_LIFESTYLE_ESCALATION = {
    "current_attempt": 2,
    "attempts": [
        {
            "attempt": 1,
            "prompt_version": "original (brand/visual framing)",
            "axis": "structural",
            "confidence": "high",
            "description": (
                "The website underwent a comprehensive redesign from a simple, vertical landing page "
                "format into a dense, modern e-commerce marketplace. The layout changed from large, "
                "spaced-out sections listing product names to a grid-based system featuring product "
                "cards with prices, discount percentages, and badges like 'Bestseller' or 'New Launch'. "
                "The navigation bar expanded significantly from four simple links to a complex menu "
                "with multiple sub-categories."
            ),
            "evidence_pointer": (
                "The comparison of the product sections reveals that while the specific models changed "
                "(e.g., 'ROCKERZ In Ear 200' in t0 vs 'boAt Airdopes 181 Pro' in t1), the core business "
                "remains selling audio and lifestyle electronics. The transformation from the sparse "
                "'HEADSETS' list in t0 to the 'Sale Is Live' grid with currency symbols (₹) and "
                "detailed specs in t1 confirms a structural site overhaul rather than a category shift."
            ),
            "policy_action_at_time": "approve",
            "policy_reason_at_time": "matched rule {'axis': 'structural'} (old policy, since changed)",
            "vlm_model": "qwen/qwen3.6-27b",
            "vlm_latency_s": 5.297,
            "vlm_input_tokens": 3534,
            "vlm_output_tokens": 1380,
            "vlm_cost_usd": 0.00626,
            "agreed_with_ground_truth": False,
        },
        {
            "attempt": 2,
            "prompt_version": "revised (category/MCC risk-exposure framing)",
            "axis": "category",
            "confidence": "high",
            "description": (
                "The merchant transitioned from a niche 'Lifestyle' showcase featuring basic audio gear "
                "(headsets, speakers) and simple accessories (cables, power banks) into a comprehensive "
                "e-commerce store. The product mix expanded significantly to include entirely new "
                "product verticals."
            ),
            "evidence_pointer": (
                "The t1 screenshot features a 'Shop by Categories' section explicitly listing 'Smart "
                "Watches', 'Dashcams', 'Projectors', 'Soundbars', and 'Trimmers', which were absent in "
                "the t0 screenshot. Additionally, the t1 homepage prominently sells Smart Watches and "
                "ANC Headphones, whereas t0 only listed 'Headsets' and 'Speakers' with older models "
                "like 'ROCKERZ In Ear 200'."
            ),
            "policy_action_at_time": "flag_for_review",
            "policy_reason_at_time": "matched rule {'axis': 'category', 'confidence': 'high'}",
            "vlm_model": "qwen/qwen3.6-27b",
            "vlm_latency_s": 6.156,
            "vlm_input_tokens": 3706,
            "vlm_output_tokens": 1472,
            "vlm_cost_usd": 0.00664,
            "agreed_with_ground_truth": True,
        },
    ],
}


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

    if domain == "boat-lifestyle.com":
        record["escalation"] = BOAT_LIFESTYLE_ESCALATION
        # The queue's single row reflects the CURRENT (latest, most-trustworthy)
        # attempt's outcome, since a risk analyst working the queue wants the
        # standing answer -- the attempt history lives in the detail view.
        current = next(a for a in BOAT_LIFESTYLE_ESCALATION["attempts"] if a["attempt"] == BOAT_LIFESTYLE_ESCALATION["current_attempt"])
        record["final_action"] = current["policy_action_at_time"]
        record["policy_reason"] = current["policy_reason_at_time"]
        record["path"] = "escalated"
    elif log_row.get("evidence_axis"):
        record["escalation"] = {
            "current_attempt": 1,
            "attempts": [
                {
                    "attempt": 1,
                    "prompt_version": "current",
                    "axis": log_row.get("evidence_axis"),
                    "confidence": log_row.get("evidence_confidence"),
                    "evidence_pointer": log_row.get("evidence_pointer"),
                    "description": None,
                    "policy_action_at_time": log_row.get("final_action"),
                    "policy_reason_at_time": log_row.get("policy_reason"),
                    "vlm_latency_s": num(log_row.get("vlm_latency_s")),
                    "vlm_input_tokens": num(log_row.get("vlm_input_tokens")),
                    "vlm_output_tokens": num(log_row.get("vlm_output_tokens")),
                    "vlm_cost_usd": num(log_row.get("vlm_cost_usd")),
                }
            ],
        }

    return record


def main() -> int:
    run_log = load_run_log()
    ground_truth = load_ground_truth()

    domains = ["kreditbee.in", "healthkart.com", "boat-lifestyle.com"]
    pairs = [build_pair(d, run_log, ground_truth) for d in domains]

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(pairs, indent=2), encoding="utf-8")
    print(f"wrote {DATA_OUT} with {len(pairs)} pairs")
    for p in pairs:
        print(f"  {p['domain']:<24} path={p['path']:<18} action={p['final_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
