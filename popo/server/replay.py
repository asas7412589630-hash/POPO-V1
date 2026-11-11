"""Replay a recorded request and compare model outputs.

Two constraints come from what actually goes wrong with these tools:

*Replay must not invent a request.* If the recorded turn has no prompt, the tool
refuses. A reconstructed guess proves nothing, and a successful-looking result
from a made-up input is worse than an error.

*Comparison must be bounded.* Every call spends real money, so the number of
models and repeats is capped by the caller and reported back.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Callable

from popo.config import Config
from popo.core.pipeline import run_pipeline
from popo.core.request import ChatRequest
from popo.core.upstream import UpstreamSource

MAX_COMPARE_MODELS = 4
MAX_COMPARE_REPEATS = 3


@dataclass
class ReplayOutcome:
    conversation_id: str
    before_chars: int
    after_chars: int
    before_verdict: str
    after_verdict: str
    changed: bool
    stable: bool | None
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"conversation_id": self.conversation_id,
                "before": {"chars": self.before_chars, "verdict": self.before_verdict},
                "after": {"chars": self.after_chars, "verdict": self.after_verdict},
                "changed": self.changed,
                # None means "one sample, so stability is undefined" — not "stable".
                "stable": self.stable,
                "samples": self.samples}


class ReplayError(ValueError):
    """Raised when a replay cannot be built from recorded data."""


def build_request(records, conversation_id: str,
                  prompt: str | None = None) -> ChatRequest:
    """Rebuild the request for a recorded turn, or refuse."""
    snapshots = records.read_conversation(conversation_id)
    if snapshots:
        messages = snapshots[-1].get("messages") or []
        cleaned = [m for m in messages if m.get("role") in ("system", "user", "assistant")]
        # Drop the recorded answer: replay must re-ask, not replay the reply.
        while cleaned and cleaned[-1].get("role") == "assistant":
            cleaned.pop()
        if cleaned and any(m.get("role") == "user" for m in cleaned):
            model = ""
            return ChatRequest(model=model or "glm-5.3", messages=cleaned)
    if prompt:
        return ChatRequest(model="glm-5.3",
                           messages=[{"role": "user", "content": prompt}])
    raise ReplayError(
        "cannot rebuild this request: the record has no user turn. "
        "Pass a prompt explicitly, or replay from a stored conversation.")


def replay(config: Config, records, conversation_id: str, *, prompt: str | None = None,
           repeats: int = 1, source_picker: Callable[[str], UpstreamSource] | None = None,
           model: str = "") -> ReplayOutcome:
    request = build_request(records, conversation_id, prompt)
    if model:
        request = ChatRequest(model=model, messages=request.messages,
                             stream=False, temperature=request.temperature,
                             max_tokens=request.max_tokens, top_p=request.top_p,
                             extra=request.extra)

    snapshots = records.read_conversation(conversation_id)
    before_chars = 0
    before_verdict = "unknown"
    if snapshots:
        messages = snapshots[-1].get("messages") or []
        last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        if last:
            before_chars = len(str(last.get("content") or ""))
        before_verdict = str(snapshots[-1].get("verdict", "unknown"))

    repeats = max(1, min(int(repeats), MAX_COMPARE_REPEATS))
    samples: list[dict] = []
    for _ in range(repeats):
        result = run_pipeline(request, config, source_picker=source_picker,
                              records=None, conversation_id="", armor_level=None)
        content = _content(result.response)
        samples.append({"chars": len(content), "verdict": result.verdict,
                        "score": result.score})

    after_chars = samples[-1]["chars"]
    stable = None
    if repeats >= 2:
        # Stability is about the replay agreeing with itself, not with the original.
        stable = len({tuple(sorted(sample.items())) for sample in samples}) == 1
    return ReplayOutcome(conversation_id=conversation_id,
                         before_chars=before_chars, after_chars=after_chars,
                         before_verdict=before_verdict,
                         after_verdict=samples[-1]["verdict"],
                         changed=after_chars != before_chars,
                         stable=stable, samples=samples)


def _content(response: dict) -> str:
    choices = response.get("choices") or [{}]
    return str(((choices[0] or {}).get("message") or {}).get("content") or "")


def compare(config: Config, prompt: str, models: list[str], *, repeats: int = 1,
            source_picker: Callable[[str], UpstreamSource] | None = None,
            max_models: int = MAX_COMPARE_MODELS) -> dict:
    """Ask the same prompt of several models and rank by deliverability."""
    if not prompt.strip():
        raise ValueError("prompt is required")
    chosen = [m for m in models if m][:max_models]
    if not chosen:
        raise ValueError("at least one published model is required")
    repeats = max(1, min(int(repeats), MAX_COMPARE_REPEATS))

    rows: list[dict] = []
    for model in chosen:
        delivered = 0
        errors = 0
        scores: list[float] = []
        chars: list[int] = []
        for _ in range(repeats):
            request = ChatRequest(model=model,
                                  messages=[{"role": "user", "content": prompt}])
            try:
                result = run_pipeline(request, config, source_picker=source_picker,
                                      records=None)
            except Exception as exc:  # one broken model must not abort the comparison
                errors += 1
                rows.append({"model": model, "error": str(exc)})
                continue
            scores.append(result.score)
            content = _content(result.response)
            chars.append(len(content))
            if result.verdict in ("clean", "retry_ok", "retry_best", "salvaged"):
                delivered += 1
        rows.append({
            "model": model,
            "delivered": delivered,
            "errors": errors,
            "repeats": repeats,
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "avg_chars": round(sum(chars) / len(chars), 1) if chars else 0.0,
        })

    best = max((row for row in rows if row.get("delivered")),
               key=lambda row: (row.get("delivered", 0), row.get("avg_score", 0.0)),
               default=None)
    return {"prompt_chars": len(prompt), "repeats": repeats,
            "models": rows, "best": best["model"] if best else None,
            "spend_note": f"{len(chosen) * repeats} upstream call(s) attempted"}
