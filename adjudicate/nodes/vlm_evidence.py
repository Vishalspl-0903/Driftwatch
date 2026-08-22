"""adjudicate/nodes/vlm_evidence.py -- node 3: the only node in this graph
that calls a model.

Model: gemini-3.1-pro-preview via `google-genai` (the current SDK -- NOT
the deprecated `google-generativeai` package). The task specified Gemini
2.5 Pro; generate_content calls to gemini-2.5-pro return 404 "no longer
available to new users" for the API key this project has (confirmed live,
not assumed -- the model is still listed by models.list(), just not
callable). Switched to gemini-3.1-pro-preview, the direct replacement named
in Google's own error message, with explicit user confirmation. See
logging_utils.py for the re-looked-up pricing this substitution required.

Multimodal call: t0 image + t1 image + drift score + DOM diff hint in,
structured JSON out. The taxonomy handed to the model is deliberately just
{category, structural} -- content and trust were tested and cut (see
FAILURES.md), and this node does not offer them as options at all, rather
than trust the model to self-restrict.

response_schema below is Gemini's own structured-output enforcement
(response_mime_type="application/json" + a Pydantic schema), which is why
node 4 (structure_output) can validate rather than free-text-parse. Design
principle carried through: this node gathers and describes evidence, it
does not decide the action -- policy_adjudicate (node 5) does that, off
axis+confidence alone.
"""

from __future__ import annotations

import os
import time

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from adjudicate.logging_utils import estimate_cost_usd
from adjudicate.state import AdjudicateState

MODEL_NAME = "gemini-3.1-pro-preview"

CANDIDATE_AXES = ("category", "structural")
CONFIDENCE_LEVELS = ("high", "medium", "low")

PROMPT_TEMPLATE = """You are reviewing two archived homepage screenshots of the same merchant, taken at different times, to judge whether the merchant's product category changed or the site was merely redesigned.

Merchant: {domain}
Deterministic image-drift score (CLIP catalog-image centroid distance, t0 vs t1): {score:.4f}
This score fell in the boundary zone the automated detector cannot confidently classify on its own -- that is why you are being asked.

A summary of line-level text differences between the two page renders (context only, not the evidence itself):
{dom_diff}

The first image is the t0 (earlier) screenshot. The second image is the t1 (later) screenshot.

Task:
1. Describe in plain language what visibly changed between t0 and t1.
2. Decide which of exactly two axes this change fits:
   - "category": the merchant's core product/service category changed (e.g. a different kind of product being sold, a different line of business).
   - "structural": the site was redesigned (layout, visual style, navigation) but the underlying business and product category are the same.
   Do not consider any other axis. If the change is ambiguous, pick whichever of these two is the closer fit and reflect your uncertainty in the confidence field.
3. State your confidence in that axis choice: "high", "medium", or "low".
4. Point to the specific visual evidence that supports your answer -- which part of the image, which product category or section, what specifically changed.

Respond with the structured JSON fields directly."""


class VLMEvidence(BaseModel):
    axis: str = Field(description="Either 'category' or 'structural', nothing else.")
    description: str = Field(description="Plain-language description of what changed between t0 and t1.")
    confidence: str = Field(description="One of 'high', 'medium', 'low'.")
    evidence_pointer: str = Field(description="The specific visual evidence supporting the axis choice.")


def _client() -> genai.Client:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Load it from .env (python-dotenv) or export it "
            "before running the adjudicate graph."
        )
    return genai.Client(api_key=api_key)


def vlm_evidence(state: AdjudicateState) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        domain=state["domain"],
        score=state["image_drift_score"],
        dom_diff=state.get("dom_diff_summary", "(no diff available)"),
    )

    contents = [
        prompt,
        types.Part.from_bytes(data=state["t0_image_bytes"], mime_type="image/png"),
        types.Part.from_bytes(data=state["t1_image_bytes"], mime_type="image/png"),
    ]

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VLMEvidence,
    )

    client = _client()
    started = time.monotonic()
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=contents, config=config)
    except Exception as exc:  # noqa: BLE001 -- fail loudly downstream, not silently here
        return {
            "vlm_error": f"{type(exc).__name__}: {exc}",
            "vlm_latency_s": round(time.monotonic() - started, 3),
            "vlm_model": MODEL_NAME,
            "vlm_raw_text": "",
            "vlm_input_tokens": 0,
            "vlm_output_tokens": 0,
            "vlm_cost_usd": 0.0,
        }
    latency = round(time.monotonic() - started, 3)

    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count or 0
    # Gemini 2.5's thinking tokens are billed at the output rate but reported
    # separately from candidates_token_count -- both count toward cost.
    output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    cost = estimate_cost_usd(input_tokens, output_tokens)

    return {
        "vlm_raw_text": response.text or "",
        "vlm_latency_s": latency,
        "vlm_input_tokens": input_tokens,
        "vlm_output_tokens": output_tokens,
        "vlm_cost_usd": round(cost, 6),
        "vlm_model": MODEL_NAME,
        "vlm_error": None,
    }
