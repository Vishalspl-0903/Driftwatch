"""adjudicate/logging_utils.py -- run logging and the cost/escalation-rate
summary printed after a test batch.

Pricing constants below are qwen/qwen3.6-27b's published per-token rate on
Groq as of 2026-08 (console.groq.com/docs/models, cross-checked against
independent pricing aggregators). Looked up, not guessed, per the task
instruction.

Model history: the task originally specified Gemini 2.5 Pro; that model
returned 404 "no longer available to new users" for the project's API key,
so gemini-3.1-pro-preview was substituted (with its own re-looked-up
pricing, $2.00/$12.00 per 1M). vlm_evidence.py has since been swapped again,
this time to Groq's qwen/qwen3.6-27b on request -- its rate ($0.60/$3.00 per
1M) is materially different again, hence a fresh lookup rather than reusing
either prior number. Groq has no >200k-token pricing tier to worry about
here (qwen3.6-27b's context window is 131K tokens total, and these calls
are two images plus a short prompt regardless).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# console.groq.com/docs/models -- qwen/qwen3.6-27b
VLM_INPUT_USD_PER_1M_TOKENS = 0.60
VLM_OUTPUT_USD_PER_1M_TOKENS = 3.00

LOG_FIELDS = [
    "timestamp", "domain", "drift_score", "path",
    "vlm_latency_s", "vlm_input_tokens", "vlm_output_tokens", "vlm_cost_usd",
    "evidence_axis", "evidence_confidence", "evidence_pointer",
    "final_action", "policy_reason", "error",
]

DEFAULT_LOG_PATH = Path("adjudicate/logs/run_log.csv")


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * VLM_INPUT_USD_PER_1M_TOKENS
        + output_tokens / 1_000_000 * VLM_OUTPUT_USD_PER_1M_TOKENS
    )


def append_run_log(state: dict[str, Any], log_path: Path = DEFAULT_LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    evidence = state.get("evidence") or {}
    with log_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "domain": state.get("domain"),
                "drift_score": state.get("image_drift_score"),
                "path": state.get("path"),
                "vlm_latency_s": state.get("vlm_latency_s"),
                "vlm_input_tokens": state.get("vlm_input_tokens"),
                "vlm_output_tokens": state.get("vlm_output_tokens"),
                "vlm_cost_usd": state.get("vlm_cost_usd"),
                "evidence_axis": evidence.get("axis"),
                "evidence_confidence": evidence.get("confidence"),
                "evidence_pointer": evidence.get("evidence_pointer"),
                "final_action": state.get("action"),
                "policy_reason": state.get("policy_reason"),
                "error": state.get("vlm_error") or state.get("schema_error") or "",
            }
        )


@dataclass
class BatchSummary:
    results: list[dict[str, Any]] = field(default_factory=list)

    def add(self, state: dict[str, Any]) -> None:
        self.results.append(state)

    def render(self) -> str:
        total = len(self.results)
        if total == 0:
            return "no pairs run"
        by_path: dict[str, int] = {}
        for r in self.results:
            by_path[r.get("path", "?")] = by_path.get(r.get("path", "?"), 0) + 1

        escalated_costs = [
            r["vlm_cost_usd"] for r in self.results
            if r.get("path") == "escalated" and r.get("vlm_cost_usd") is not None
        ]
        avg_cost = sum(escalated_costs) / len(escalated_costs) if escalated_costs else None

        lines = [
            f"total pairs run: {total}",
        ]
        for path_name in ("no_action", "auto_flag", "escalated", "insufficient_data", "graph_error"):
            n = by_path.get(path_name, 0)
            lines.append(f"  {path_name:<18} {n:>3}  ({n/total:.0%})")
        other = total - sum(
            by_path.get(p, 0)
            for p in ("no_action", "auto_flag", "escalated", "insufficient_data", "graph_error")
        )
        if other:
            lines.append(f"  (other/unrecognized path: {other})")
        if avg_cost is not None:
            lines.append(f"average cost per escalated VLM call: ${avg_cost:.6f}")
        else:
            lines.append("average cost per escalated VLM call: n/a (no escalated calls)")
        return "\n".join(lines)
