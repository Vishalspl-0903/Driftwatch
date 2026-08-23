"""adjudicate/nodes/vlm_evidence.py -- node 3: the only node in this graph
that calls a model.

Model: qwen/qwen3.6-27b via the `groq` SDK (OpenAI-compatible chat
completions endpoint). Swapped from Gemini (gemini-3.1-pro-preview, itself
already a substitution for the originally-specified Gemini 2.5 Pro -- see
git history) to Groq on request. qwen/qwen3.6-27b is Groq's ONLY
vision-capable model as of 2026-08 (confirmed via console.groq.com/docs/vision
and cross-checked against this account's live model list, not assumed) --
there was no second candidate to choose between. Groq's own docs describe it
as a preview model, not production-grade, which matches what testing here
found: see the schema-reliability notes below.

Multimodal call: t0 image + t1 image + drift score + DOM diff hint in,
structured JSON out. The taxonomy handed to the model is deliberately just
{category, structural} -- content and trust were tested and cut (see
FAILURES.md), and this node does not offer them as options at all, rather
than trust the model to self-restrict.

Structured-output reliability, confirmed by live testing against this exact
model before settling on this config:
  - response_format={"type": "json_schema", ...} with strict=True, or with
    a pydantic schema built via ConfigDict(extra="forbid") (which emits
    "additionalProperties": false), reliably makes qwen/qwen3.6-27b return
    an EMPTY completion (400 json_validate_failed, failed_generation="") --
    not a schema mismatch, a generation failure. This is a real limitation
    of this specific model on Groq's structured-output implementation, not
    a bug in this code; additionalProperties is never set here because of
    it.
  - Without that flag, but with Literal-typed (enum-constrained) fields,
    generation succeeds and the enum values are respected -- but a live
    test still once produced a response missing the "confidence" field
    entirely, despite it being in "required". So the JSON shape is not
    fully guaranteed even in the working configuration. That is exactly why
    node 4 (structure_output) validates independently rather than assume
    the schema was honored -- for this model that safety net is doing real
    work, not just defensive boilerplate.

This node retries exactly once (MAX_ATTEMPTS=2) when an attempt doesn't
produce a validating response, whether the API call itself threw or it
returned 200 with unparseable/incomplete content. A failure that survives
both attempts is reported via vlm_error, same as before -- this node still
never decides what happens next; structure_output.py and the graph route a
persistent failure to a safe, logged outcome (needs_manual_review), not a
raised exception. See FAILURES.md for the measured reliability with vs.
without the retry.
"""

from __future__ import annotations

import base64
import json
import os
import time

from groq import Groq
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from adjudicate.logging_utils import estimate_cost_usd
from adjudicate.state import AdjudicateState

MODEL_NAME = "qwen/qwen3.6-27b"

CANDIDATE_AXES = ("category", "structural")
CONFIDENCE_LEVELS = ("high", "medium", "low")

# Exactly one retry, per instruction -- not a loop. All 4 real failures on
# record (see FAILURES.md) were the API call itself throwing (Groq's own
# 400 json_validate_failed on an empty or truncated generation); this also
# covers the rarer case seen in pre-ship testing where the call succeeds
# but the content doesn't validate (a live test once returned valid JSON
# missing the required "confidence" field). Either way counts as "the
# attempt didn't produce usable evidence" and costs one retry, not more.
MAX_ATTEMPTS = 2

# Revised 2026-08 after the boat-lifestyle.com finding (see FAILURES.md):
# the original prompt let the model reason about brand identity ("does this
# still look like the same company") and visual redesign, and it picked
# "structural" at high confidence on a real category-drift pair despite its
# own evidence_pointer noting the product mix had changed. This version
# frames the task explicitly as category/MCC risk-exposure assessment from
# the product catalog, and tells the model directly not to let brand/visual
# continuity override what the product mix shows.
PROMPT_TEMPLATE = """You are a risk analyst assessing category/MCC (merchant category code) risk exposure for a payments/compliance use case. Your job is to judge whether the merchant's PRODUCT MIX changed enough to represent a different risk category -- not whether the brand still "feels" the same, and not how much the visual design changed.

Merchant: {domain}
Deterministic image-drift score (CLIP catalog-image centroid distance, t0 vs t1): {score:.4f}
This score fell in the boundary zone the automated detector cannot confidently classify on its own -- that is why you are being asked.

A summary of line-level text differences between the two page renders (context only, not the evidence itself):
{dom_diff}

The first image is the t0 (earlier) screenshot. The second image is the t1 (later) screenshot.

Task:
1. Describe in plain language what visibly changed between t0 and t1, focusing specifically on the PRODUCT CATALOG / PRODUCT MIX -- what kinds of products or services are being sold -- not the page layout or visual style.
2. Decide which of exactly two axes this change fits:
   - "category": the merchant's PRODUCT MIX or line of business meaningfully changed -- new product categories that weren't sold before, a different underlying business, a materially different MCC risk profile. This applies even if the brand name, logo, and overall visual identity stayed the same: a redesign that ALSO expanded into new product categories is still "category", not "structural".
   - "structural": the site's layout, visual style, or navigation changed, but the actual set of products/services sold is the same -- no new product categories, same underlying MCC risk profile.
   Do not let visual redesign, rebranding, or "does this still look like the same company" reasoning override what the product catalog itself shows. If the product mix expanded into new categories, that is "category" drift regardless of how consistent the branding looks.
   Do not consider any other axis. If the change is ambiguous, pick whichever of these two is the closer fit and reflect your uncertainty in the confidence field.
3. State your confidence in that axis choice: "high", "medium", or "low".
4. Point to the specific visual evidence that supports your answer -- which part of the image, which product category or section, what specifically changed.

Respond with a JSON object containing exactly these four fields, all required, none omitted: axis, description, confidence, evidence_pointer."""


class VLMEvidence(BaseModel):
    axis: Literal["category", "structural"] = Field(description="Either 'category' or 'structural', nothing else.")
    description: str = Field(description="Plain-language description of what changed between t0 and t1.")
    confidence: Literal["high", "medium", "low"] = Field(description="One of 'high', 'medium', 'low'.")
    evidence_pointer: str = Field(description="The specific visual evidence supporting the axis choice.")


def _client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Load it from .env (python-dotenv) or export it "
            "before running the adjudicate graph."
        )
    return Groq(api_key=api_key)


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _attempt_call(client: Groq, messages: list, response_format: dict) -> tuple[bool, str, object, str]:
    """One raw API call plus an immediate, lightweight validity check --
    just enough to decide whether this attempt is worth keeping or worth
    retrying. Not the authoritative validation gate: structure_output.py
    (node 4) re-parses and re-validates independently regardless of what
    this check concludes, per its own "never trust blindly" design. This
    check exists only so vlm_evidence can decide, right now, whether to
    spend its one retry.

    Returns (ok, raw_text, usage_or_None, error_message_or_empty).
    """
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format=response_format,
        )
    except Exception as exc:  # noqa: BLE001 -- caller decides retry vs. give up
        return False, "", None, f"{type(exc).__name__}: {exc}"

    raw_text = response.choices[0].message.content or ""
    try:
        VLMEvidence.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        return False, raw_text, response.usage, f"response did not fit the evidence schema: {exc}"

    return True, raw_text, response.usage, ""


def vlm_evidence(state: AdjudicateState) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        domain=state["domain"],
        score=state["image_drift_score"],
        dom_diff=state.get("dom_diff_summary", "(no diff available)"),
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_uri(state["t0_image_bytes"])}},
                {"type": "image_url", "image_url": {"url": _data_uri(state["t1_image_bytes"])}},
            ],
        }
    ]

    # No "strict": True and no additionalProperties -- see module docstring;
    # both reliably break generation on this model.
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "VLMEvidence", "schema": VLMEvidence.model_json_schema()},
    }

    client = _client()
    started = time.monotonic()
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ok, raw_text, usage, err = _attempt_call(client, messages, response_format)
        if ok:
            latency = round(time.monotonic() - started, 3)
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            cost = estimate_cost_usd(input_tokens, output_tokens)
            return {
                "vlm_raw_text": raw_text,
                "vlm_latency_s": latency,
                "vlm_input_tokens": input_tokens,
                "vlm_output_tokens": output_tokens,
                "vlm_cost_usd": round(cost, 6),
                "vlm_model": MODEL_NAME,
                "vlm_error": None,
                "vlm_attempts": attempt,
                "vlm_retry_used": attempt > 1,
            }
        last_error = err
        # falls through to the next loop iteration (the one retry) or exits
        # the loop and reports the failure below

    # Every attempt (initial + the one retry) failed. Token/cost accounting
    # for failed attempts is intentionally not attempted here -- Groq's
    # error responses for this failure mode don't reliably expose usage,
    # and reporting a guessed number would be worse than reporting none.
    return {
        "vlm_error": last_error,
        "vlm_latency_s": round(time.monotonic() - started, 3),
        "vlm_model": MODEL_NAME,
        "vlm_raw_text": "",
        "vlm_input_tokens": 0,
        "vlm_output_tokens": 0,
        "vlm_cost_usd": 0.0,
        "vlm_attempts": MAX_ATTEMPTS,
        "vlm_retry_used": True,
    }
