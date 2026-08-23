"""adjudicate/nodes/structure_output.py -- node 4: parse/validate the VLM
response into the fixed evidence schema. Pure Python, no model call.

vlm_evidence.py already retries once and validates before returning
success, so a state reaching this node with vlm_error set means BOTH
attempts failed; this node re-parses and re-validates independently
regardless, per the same "never trust blindly" logic. No silent coercion,
no guessed default axis/confidence, ever.

What changed here (see FAILURES.md, "structure_output raised into a dead
end"): this node used to raise SchemaValidationError on any failure. That
was "fail loudly" in name only -- LangGraph aborts the whole run on a
raised exception, so `structure_output -> policy_adjudicate` (a plain
unconditional edge) never executed, and the only reason any failed pair
ever got a logged outcome at all was that adjudicate/run_test.py happened
to wrap app.invoke() in its own try/except. Any other real caller of the
graph got an uncaught exception and the pair left no trace in
run_log.csv. Failing loudly is right for a node that would otherwise
silently coerce bad evidence into good; it is wrong for a node whose
failure has one correct, known outcome (mandatory human review) that the
system should be able to state on its own.

Now: a failure here never raises. It returns a normal state update --
path="parse_failed", action=policy.yaml's parse_failure_action -- and logs
it itself (same self-logging pattern escalation_check.py already uses for
its terminal paths), so any caller of the graph gets a safe, logged,
non-approving outcome, not an exception they have to know to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from adjudicate.logging_utils import DEFAULT_LOG_PATH, append_run_log
from adjudicate.nodes.vlm_evidence import CANDIDATE_AXES, CONFIDENCE_LEVELS, VLMEvidence
from adjudicate.policy_loader import load_policy
from adjudicate.state import AdjudicateState

RAW_FAILURE_LOG = Path("adjudicate/logs/schema_failures.log")

PATH_PARSE_FAILED = "parse_failed"


def _log_raw_failure(domain: str, raw_text: str, reason: str) -> None:
    RAW_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RAW_FAILURE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"--- {domain} :: {reason} ---\n{raw_text}\n\n")


def _fail_safely(state: AdjudicateState, reason: str, raw_text: str) -> dict:
    domain = state.get("domain", "?")
    _log_raw_failure(domain, raw_text, reason)

    policy = load_policy()
    action = policy.get("parse_failure_action", "needs_manual_review")

    update = {
        "evidence": None,
        "schema_error": reason,
        "path": PATH_PARSE_FAILED,
        "action": action,
        "policy_reason": f"structure_output could not obtain valid VLM evidence -- {reason}",
    }
    append_run_log({**state, **update}, log_path=DEFAULT_LOG_PATH)
    return update


def structure_output(state: AdjudicateState) -> dict:
    if state.get("vlm_error"):
        reason = f"vlm_evidence failed after {state.get('vlm_attempts', '?')} attempt(s): {state['vlm_error']}"
        return _fail_safely(state, reason, state.get("vlm_raw_text", ""))

    raw_text = state.get("vlm_raw_text", "")
    try:
        evidence = VLMEvidence.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        return _fail_safely(state, f"VLM response did not fit the evidence schema: {exc}", raw_text)

    if evidence.axis not in CANDIDATE_AXES:
        return _fail_safely(
            state, f"VLM returned axis={evidence.axis!r}, outside the locked taxonomy {CANDIDATE_AXES}", raw_text
        )
    if evidence.confidence not in CONFIDENCE_LEVELS:
        return _fail_safely(
            state, f"VLM returned confidence={evidence.confidence!r}, outside {CONFIDENCE_LEVELS}", raw_text
        )

    return {
        "evidence": evidence.model_dump(),
        "schema_error": None,
    }
