"""adjudicate/nodes/fetch_context.py -- node 2: assemble what the VLM needs
to look at. Pure Python, no model call.

drift/extract.py's candidate-image extraction pulls individual <img> URLs
for CLIP embedding, not screenshot region crops -- there is no crop
extraction in this codebase to reuse, so per the task's documented
fallback, this node hands the VLM the full t0/t1 screenshots instead. Those
are exactly what a human reviewer would look at in evals/eyeball_review.html
anyway, so it's the more faithful choice, not just the available one.

The DOM text diff is a cheap, cost-free hint for the VLM (dom.txt is
already the browser's rendered innerText, no re-parsing needed) -- it is
not the evidence itself, just context to point the model at what changed
textually before it looks at the pixels.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from adjudicate.state import AdjudicateState

MAX_DIFF_LINES = 120  # keep the prompt small; this is a hint, not the evidence


def _read_lines(path_str: str) -> list[str]:
    path = Path(path_str)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _summarize_diff(t0_lines: list[str], t1_lines: list[str]) -> str:
    diff = list(
        difflib.unified_diff(t0_lines, t1_lines, fromfile="t0", tofile="t1", lineterm="", n=0)
    )
    if not diff:
        return "(no line-level text differences detected)"
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"... ({len(diff) - MAX_DIFF_LINES} more diff lines truncated)"]
    return "\n".join(diff)


def fetch_context(state: AdjudicateState) -> dict:
    t0_shot = Path(state["t0_screenshot_path"])
    t1_shot = Path(state["t1_screenshot_path"])
    t0_bytes = t0_shot.read_bytes()
    t1_bytes = t1_shot.read_bytes()

    t0_lines = _read_lines(state.get("t0_dom_text_path", ""))
    t1_lines = _read_lines(state.get("t1_dom_text_path", ""))
    dom_diff_summary = _summarize_diff(t0_lines, t1_lines)

    return {
        "t0_image_bytes": t0_bytes,
        "t1_image_bytes": t1_bytes,
        "dom_diff_summary": dom_diff_summary,
    }
