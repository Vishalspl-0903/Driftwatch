#!/usr/bin/env python3
"""adjudicate/run_test.py -- end-to-end test of the escalation graph on the
three documented cases:

  - kreditbee.in        known clean negative -- but see FAILURES.md /
                         escalation_check.py: its image signal is actually
                         unusable (too few catalog images extracted on t0),
                         so this is expected to land on insufficient_data,
                         not no_action. Reported explicitly, not silently
                         forced to match the original expectation.
  - healthkart.com       known clean positive, score 0.3145 -- should land
                         auto_flag (the precision=1.00 zone).
  - boat-lifestyle.com    the documented boundary case, score 0.146 -- MUST
                         exercise the full path through the VLM. This is the
                         one case deterministic validation couldn't resolve.

Usage:
  python -m adjudicate.run_test
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adjudicate.graph import get_app  # noqa: E402
from adjudicate.logging_utils import DEFAULT_LOG_PATH, BatchSummary, append_run_log  # noqa: E402
from adjudicate.state import load_pair_paths  # noqa: E402
from drift.score import score_pair  # noqa: E402

PAIRS_ROOT = Path("data/pairs")
TEST_DOMAINS = ["kreditbee.in", "healthkart.com", "boat-lifestyle.com"]
SUMMARY_OUT = Path("adjudicate/logs/test_run_summary.md")


def build_initial_state(domain: str) -> dict:
    score = score_pair(domain, pairs_root=PAIRS_ROOT)
    state = {"domain": domain, "image_drift_score": score.image_drift_score}
    state.update(load_pair_paths(domain, PAIRS_ROOT))
    return state


def main() -> int:
    app = get_app()
    summary = BatchSummary()
    report_lines = ["# adjudicate/ end-to-end test run\n"]

    for domain in TEST_DOMAINS:
        print(f"\n=== {domain} ===", file=sys.stderr)
        initial = build_initial_state(domain)
        print(f"  image_drift_score = {initial['image_drift_score']}", file=sys.stderr)

        try:
            result = app.invoke(initial)
            graph_error = None
        except Exception as exc:  # noqa: BLE001 -- batch runner must survive one domain failing
            # This is now a secondary safety net, not the mechanism that makes
            # failures safe -- structure_output.py + graph.py's conditional edge
            # handle the expected "VLM never produced valid evidence" case
            # without raising at all (see FAILURES.md). What lands here is a
            # genuinely unexpected exception (a bug, a filesystem error in
            # fetch_context, etc.), which still deserves a logged row rather
            # than a silently lost pair.
            graph_error = f"{type(exc).__name__}: {exc}"
            result = {**initial, "path": "graph_error", "vlm_error": graph_error}
            append_run_log(result, log_path=DEFAULT_LOG_PATH)

        summary.add(result)

        report_lines.append(f"## {domain}\n")
        report_lines.append(f"- image_drift_score: `{initial['image_drift_score']}`")
        report_lines.append(f"- path: `{result.get('path')}`")
        if graph_error:
            report_lines.append(f"- **unexpected graph error:** {graph_error}")
        report_lines.append(f"- final action: `{result.get('action')}`")
        if result.get("path") == "parse_failed":
            report_lines.append(f"- schema_error: {result.get('schema_error')}")
            report_lines.append(f"- vlm_attempts: {result.get('vlm_attempts')}  retry_used: {result.get('vlm_retry_used')}")
            report_lines.append(f"- policy reason: {result.get('policy_reason')}")
        if result.get("path") == "escalated":
            report_lines.append(f"- VLM latency: {result.get('vlm_latency_s')}s")
            report_lines.append(
                f"- tokens: {result.get('vlm_input_tokens')} in / {result.get('vlm_output_tokens')} out"
            )
            report_lines.append(f"- estimated cost: ${result.get('vlm_cost_usd')}")
            report_lines.append(f"- vlm_attempts: {result.get('vlm_attempts')}  retry_used: {result.get('vlm_retry_used')}")
            evidence = result.get("evidence") or {}
            report_lines.append(f"- VLM axis: `{evidence.get('axis')}`  confidence: `{evidence.get('confidence')}`")
            report_lines.append(f"- VLM description: {evidence.get('description')}")
            report_lines.append(f"- VLM evidence pointer: {evidence.get('evidence_pointer')}")
            report_lines.append(f"- policy reason: {result.get('policy_reason')}")
        report_lines.append("")

        print(f"  path={result.get('path')} action={result.get('action')}", file=sys.stderr)
        if graph_error:
            print(f"  graph_error={graph_error}", file=sys.stderr)

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
