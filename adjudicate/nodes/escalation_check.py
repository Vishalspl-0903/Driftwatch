"""adjudicate/nodes/escalation_check.py -- node 1: route on the drift/ score.
Pure Python, no model call.

Thresholds are derived from the drift/ validation in FAILURES.md: clean
category-drift positives scored ~0.25-0.31 (healthkart.com, faballey.com),
clean negatives scored ~0.11-0.20, and the one documented boundary case
(boat-lifestyle.com) sits at 0.146 -- a real, legitimate category expansion
that the deterministic scorer could not confidently separate from the
negative cluster. LOW/HIGH bracket that boundary rather than the boat-
lifestyle.com score itself, so the escalation zone actually contains it
instead of drawing the line exactly on top of it.
"""

from __future__ import annotations

from pathlib import Path

from adjudicate.logging_utils import DEFAULT_LOG_PATH, append_run_log
from adjudicate.state import AdjudicateState

# Named constants, not magic numbers -- tune here, nowhere else.
LOW_THRESHOLD = 0.14
HIGH_THRESHOLD = 0.22

PATH_NO_ACTION = "no_action"
PATH_AUTO_FLAG = "auto_flag"
PATH_ESCALATED = "escalated"
PATH_INSUFFICIENT_DATA = "insufficient_data"


def escalation_check(state: AdjudicateState) -> dict:
    score = state.get("image_drift_score")

    if score is None:
        # drift/'s image signal has no usable centroid for this pair (extraction
        # found too few catalog images on one side -- see FAILURES.md / the
        # kreditbee.in case). Not a "below LOW" or "above HIGH" case: there is no
        # deterministic signal at all, so this stops here rather than silently
        # defaulting to a decision the rules layer has no evidence for.
        path = PATH_INSUFFICIENT_DATA
        action = PATH_INSUFFICIENT_DATA
    elif score < LOW_THRESHOLD:
        path = PATH_NO_ACTION
        action = PATH_NO_ACTION
    elif score >= HIGH_THRESHOLD:
        # The precision=1.00 zone from validation -- clear category drift,
        # no LLM needed to confirm what the deterministic scorer is already
        # confident about.
        path = PATH_AUTO_FLAG
        action = PATH_AUTO_FLAG
    else:
        path = PATH_ESCALATED
        action = ""  # not final yet -- policy_adjudicate sets this after the VLM path

    update: dict = {"path": path}
    if path != PATH_ESCALATED:
        update["action"] = action
        # Terminal outcome -- log it here since no later node will.
        append_run_log({**state, **update}, log_path=Path(DEFAULT_LOG_PATH))
    return update
