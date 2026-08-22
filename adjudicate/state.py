"""adjudicate/state.py -- the LangGraph state schema shared by all five nodes.

One dict flows through the graph; each node only reads the keys it needs and
writes the keys it's responsible for. Optional fields default to None/empty
so a pair that stops early (no_action, auto_flag, insufficient_data) has a
well-defined state even though nodes 2-5 never ran on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict


class AdjudicateState(TypedDict, total=False):
    # --- set before the graph runs (drift/'s output, not this package's job) ---
    domain: str
    image_drift_score: float | None
    t0_screenshot_path: str
    t1_screenshot_path: str
    t0_dom_text_path: str
    t1_dom_text_path: str

    # --- escalation_check ---
    path: str  # "no_action" | "auto_flag" | "insufficient_data" | "escalated"

    # --- fetch_context ---
    t0_image_bytes: bytes
    t1_image_bytes: bytes
    dom_diff_summary: str

    # --- vlm_evidence ---
    vlm_raw_text: str
    vlm_latency_s: float
    vlm_input_tokens: int
    vlm_output_tokens: int
    vlm_cost_usd: float
    vlm_model: str
    vlm_error: str | None

    # --- structure_output ---
    evidence: dict[str, Any] | None
    schema_error: str | None

    # --- policy_adjudicate ---
    action: str
    policy_reason: str


def load_pair_paths(domain: str, pairs_root: Path) -> dict[str, str]:
    d = pairs_root / domain
    return {
        "t0_screenshot_path": str(d / "t0" / "screenshot.png"),
        "t1_screenshot_path": str(d / "t1" / "screenshot.png"),
        "t0_dom_text_path": str(d / "t0" / "dom.txt"),
        "t1_dom_text_path": str(d / "t1" / "dom.txt"),
    }
