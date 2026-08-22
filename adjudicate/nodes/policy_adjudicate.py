"""adjudicate/nodes/policy_adjudicate.py -- node 5: turn VLM evidence into a
final action. Pure config-driven logic, no model call, ever.

Reads adjudicate/policy.yaml fresh on every call (cheap, and means editing
the policy doesn't require a code change or process restart). This is the
node the project's design principle is actually about: the model (node 3)
only assembled and described evidence; this is where a deterministic,
inspectable rules layer -- not the model -- decides the action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from adjudicate.logging_utils import DEFAULT_LOG_PATH, append_run_log
from adjudicate.state import AdjudicateState

POLICY_PATH = Path("adjudicate/policy.yaml")


def _load_policy(path: Path = POLICY_PATH) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _matches(when: dict[str, Any], evidence: dict[str, Any]) -> bool:
    return all(evidence.get(k) == v for k, v in when.items())


def policy_adjudicate(state: AdjudicateState) -> dict:
    policy = _load_policy()
    evidence = state.get("evidence") or {}

    action = None
    reason = None
    for rule in policy.get("rules", []):
        if _matches(rule["when"], evidence):
            action = rule["action"]
            reason = f"matched rule {rule['when']}"
            break

    if action is None:
        action = policy.get("default_action", "escalate_further")
        reason = f"no rule matched evidence {evidence!r} -- fell through to default_action"

    update = {"action": action, "policy_reason": reason}
    append_run_log({**state, **update}, log_path=DEFAULT_LOG_PATH)
    return update
