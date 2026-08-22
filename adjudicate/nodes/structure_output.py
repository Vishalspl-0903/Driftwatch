"""adjudicate/nodes/structure_output.py -- node 4: parse/validate the VLM
response into the fixed evidence schema. Pure Python, no model call.

vlm_evidence.py already asked Gemini for schema-constrained JSON output, so
this is a validation backstop, not a free-text parser -- but it is not
optional. If the response doesn't fit, this fails loudly: raises, and logs
the raw response first so the failure is debuggable. No silent coercion, no
guessed default axis/confidence. A policy layer that gets to make risk
decisions off of evidence must not itself be built on a node that quietly
made up the evidence when the model output didn't parse.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from adjudicate.nodes.vlm_evidence import CANDIDATE_AXES, CONFIDENCE_LEVELS, VLMEvidence
from adjudicate.state import AdjudicateState

logger = logging.getLogger("adjudicate.structure_output")

RAW_FAILURE_LOG = Path("adjudicate/logs/schema_failures.log")


class SchemaValidationError(RuntimeError):
    pass


def _log_raw_failure(domain: str, raw_text: str, reason: str) -> None:
    RAW_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RAW_FAILURE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"--- {domain} :: {reason} ---\n{raw_text}\n\n")


def structure_output(state: AdjudicateState) -> dict:
    domain = state.get("domain", "?")

    if state.get("vlm_error"):
        msg = f"vlm_evidence failed before returning a response: {state['vlm_error']}"
        _log_raw_failure(domain, state.get("vlm_raw_text", ""), msg)
        raise SchemaValidationError(msg)

    raw_text = state.get("vlm_raw_text", "")
    try:
        evidence = VLMEvidence.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        msg = f"VLM response did not fit the evidence schema: {exc}"
        _log_raw_failure(domain, raw_text, msg)
        raise SchemaValidationError(msg) from exc

    if evidence.axis not in CANDIDATE_AXES:
        msg = f"VLM returned axis={evidence.axis!r}, outside the locked taxonomy {CANDIDATE_AXES}"
        _log_raw_failure(domain, raw_text, msg)
        raise SchemaValidationError(msg)
    if evidence.confidence not in CONFIDENCE_LEVELS:
        msg = f"VLM returned confidence={evidence.confidence!r}, outside {CONFIDENCE_LEVELS}"
        _log_raw_failure(domain, raw_text, msg)
        raise SchemaValidationError(msg)

    return {
        "evidence": evidence.model_dump(),
        "schema_error": None,
    }
