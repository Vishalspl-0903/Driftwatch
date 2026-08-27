#!/usr/bin/env python3
"""scripts/run_full_batch.py -- run every hand-labeled pair in
evals/eyeball_notes.csv through drift/score.py + the full adjudicate/ graph.

Same mechanics as adjudicate/run_test.py (which only covers the 3 documented
cases), scaled to the full set of usable pairs. No thresholds, prompts,
policy rules, or detector logic are touched -- this is a scale-up run against
the frozen system.

Each graph node self-logs its terminal outcome to adjudicate/logs/run_log.csv
exactly as in run_test.py; this script only adds a row itself for a genuinely
unexpected exception (the same secondary safety net run_test.py has).

Usage:
  python scripts/run_full_batch.py
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adjudicate.graph import get_app  # noqa: E402
from adjudicate.logging_utils import DEFAULT_LOG_PATH, BatchSummary, append_run_log  # noqa: E402
from adjudicate.state import load_pair_paths  # noqa: E402
from drift.score import score_pair  # noqa: E402

PAIRS_ROOT = Path("data/pairs")
GROUND_TRUTH_CSV = Path("evals/eyeball_notes.csv")
SUMMARY_OUT = Path("adjudicate/logs/full_batch_summary.md")


def load_domains() -> list[str]:
    with GROUND_TRUTH_CSV.open(encoding="utf-8") as fh:
        return [row["domain"] for row in csv.DictReader(fh)]


def build_initial_state(domain: str) -> dict:
    score = score_pair(domain, pairs_root=PAIRS_ROOT)
    state = {"domain": domain, "image_drift_score": score.image_drift_score}
    state.update(load_pair_paths(domain, PAIRS_ROOT))
    return state


def main() -> int:
    app = get_app()
    summary = BatchSummary()
    domains = load_domains()
    report_lines = [f"# adjudicate/ full-batch run -- {len(domains)} hand-labeled pairs\n"]
    rows_for_console: list[dict] = []

    for i, domain in enumerate(domains, 1):
        print(f"\n=== [{i}/{len(domains)}] {domain} ===", file=sys.stderr, flush=True)
        t_start = time.monotonic()
        initial = build_initial_state(domain)
        print(f"  image_drift_score = {initial['image_drift_score']}", file=sys.stderr, flush=True)

        try:
            result = app.invoke(initial)
            graph_error = None
        except Exception as exc:  # noqa: BLE001 -- batch runner must survive one domain failing
            graph_error = f"{type(exc).__name__}: {exc}"
            result = {**initial, "path": "graph_error", "vlm_error": graph_error}
            append_run_log(result, log_path=DEFAULT_LOG_PATH)

        summary.add(result)
        elapsed = round(time.monotonic() - t_start, 1)

        report_lines.append(f"## {domain}\n")
        report_lines.append(f"- image_drift_score: `{initial['image_drift_score']}`")
        report_lines.append(f"- path: `{result.get('path')}`")
        report_lines.append(f"- final action: `{result.get('action')}`")
        report_lines.append(f"- wall time: {elapsed}s")
        if graph_error:
            report_lines.append(f"- **unexpected graph error:** {graph_error}")
        if result.get("path") == "parse_failed":
            report_lines.append(f"- schema_error: {result.get('schema_error')}")
            report_lines.append(
                f"- vlm_attempts: {result.get('vlm_attempts')}  retry_used: {result.get('vlm_retry_used')}"
            )
        if result.get("path") == "escalated":
            report_lines.append(f"- VLM latency: {result.get('vlm_latency_s')}s")
            report_lines.append(
                f"- tokens: {result.get('vlm_input_tokens')} in / {result.get('vlm_output_tokens')} out"
            )
            report_lines.append(f"- estimated cost: ${result.get('vlm_cost_usd')}")
            report_lines.append(
                f"- vlm_attempts: {result.get('vlm_attempts')}  retry_used: {result.get('vlm_retry_used')}"
            )
            evidence = result.get("evidence") or {}
            report_lines.append(
                f"- VLM axis: `{evidence.get('axis')}`  confidence: `{evidence.get('confidence')}`"
            )
            report_lines.append(f"- policy reason: {result.get('policy_reason')}")
        report_lines.append("")

        print(
            f"  path={result.get('path')} action={result.get('action')} ({elapsed}s)",
            file=sys.stderr,
            flush=True,
        )

    batch_summary_text = summary.render()
    report_lines.append("## Batch summary\n")
    report_lines.append("```")
    report_lines.append(batch_summary_text)
    report_lines.append("```")

    SUMMARY_OUT.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_OUT.write_text("\n".join(report_lines), encoding="utf-8")

    print("\n" + "=" * 60)
    print(batch_summary_text)
    print("=" * 60)
    print(f"\nfull report written to {SUMMARY_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
