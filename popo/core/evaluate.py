"""Reply classification and ranking for one upstream call.

Split out of ``pipeline.py`` when budget escalation pushed that module past its
700-line ceiling. Kept behaviour-identical: this is the function that decides
whether a reply is an answer, a fragment, a refusal worth salvaging, or a
provider error wearing a 200.
"""
from __future__ import annotations

from popo.armor.salvage import salvage
from popo.armor.scoring import RequestShape, score, score_completion
from popo.core.pipeline_types import Attempt
from popo.core.retry import classify_for_retry, starts_with_refusal


def content_of(data: dict) -> str:
    choices = data.get("choices") or [{}]
    return str((choices[0] or {}).get("message", {}).get("content", "") or "")


def evaluate_reply(data: dict, shape: RequestShape) -> Attempt:
    """Classify a reply and rank it against the request that produced it."""
    classification = classify_for_retry(data)
    text = content_of(data)

    if classification == "truncated":
        # Two distinct cases share one cause:
        #   * empty content  — the model spent the whole budget reasoning;
        #   * partial content — the answer was cut off mid-sentence.
        # The first cannot be delivered at all. The second is worth delivering
        # (a partial answer beats nothing) but must be labelled, so the caller
        # is not told a truncated reply was complete.
        if not text.strip():
            return Attempt(data=data, classification=classification, score=0.0,
                           deliverable=False, reason="output-budget-exhausted")
        verdict = score(text, shape)
        return Attempt(data=data, classification=classification, score=verdict.score,
                       deliverable=verdict.deliverable,
                       reason=f"truncated:{verdict.reason}")

    if classification == "upstream_error":
        # A provider error returned as a 200 completion is not an answer. It is
        # also not worth retrying with more pressure: the same broken route will
        # answer the same way.
        return Attempt(data=data, classification=classification, score=0.0,
                       deliverable=False, reason="provider-error")

    if classification in ("refusal", "empty") or starts_with_refusal(text):
        # A refusal often still contains the useful part: offsets, commands, a
        # fenced block. Deliver that rather than spending another request.
        rescued = salvage(text)
        if rescued:
            verdict = score(rescued, shape)
            return Attempt(data=replace_content(data, rescued),
                           classification="salvaged", score=verdict.score,
                           deliverable=verdict.deliverable,
                           reason=f"salvage:{verdict.reason}", salvaged=True)
        if classification == "empty":
            return Attempt(data=data, classification=classification, score=0.0,
                           deliverable=False, reason=classification)
        # Keep the refusal text as the fallback candidate: it may still outrank a
        # worse retry, and the caller should see what actually came back.
        return Attempt(data=data, classification=classification, score=0.0,
                       deliverable=False, reason=classification)

    verdict = score_completion(data, shape)
    if not verdict.deliverable:
        return Attempt(data=data, classification="dodged", score=verdict.score,
                       deliverable=False, reason=verdict.reason)
    return Attempt(data=data, classification=classification, score=verdict.score,
                   deliverable=verdict.deliverable, reason=verdict.reason)


def replace_content(data: dict, content: str) -> dict:
    """Return a copy of a completion with its first message content replaced."""
    result = dict(data)
    choices = list(result.get("choices") or [])
    if not choices:
        result["choices"] = [{"index": 0,
                              "message": {"role": "assistant", "content": content},
                              "finish_reason": "stop"}]
        return result
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    message["content"] = content
    choice["message"] = message
    choices[0] = choice
    result["choices"] = choices
    return result
