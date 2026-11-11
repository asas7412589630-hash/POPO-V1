"""Audit stored traces against what the pipeline claimed it did.

The classifier is a heuristic, and unexamined heuristics become folklore. This
module reads what was actually recorded and reports *internal contradictions* —
places where the recorded decision and the recorded outcome disagree. It does not
claim to prove correctness; it points at turns worth reading.

Every finding here corresponds to a failure that was observed on real traffic,
which is the only reason it earns a line of code:

``retry-made-it-worse``  the retry produced a shorter reply than the attempt before it
``retry-burned``         the first attempt was already deliverable, yet a retry ran
``delivered-refusal``    a verdict says delivered, the content opens with a refusal
``ok-but-empty``         "delivered" with no text at all
``thin-delivered``       the reply is shorter than the model's own floor
``salvage-suspicious``   salvage kept so little that a retry would likely do better
``error-unclassified``   an error with no kind, so nobody can tell which side broke
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass

from popo.armor.scoring import analyse
from popo.core.retry import starts_with_refusal

# A "substantive" reply for audit purposes: has an artefact or real length.
_MIN_SUBSTANTIVE = 60


@dataclass
class Finding:
    code: str
    conversation_id: str
    detail: str
    chars: int = 0

    def as_dict(self) -> dict:
        return {"code": self.code, "conversation": self.conversation_id,
                "detail": self.detail, "chars": self.chars}


def _rows(trace_dir: str, days: int) -> list[dict]:
    if not os.path.isdir(trace_dir):
        return []
    names = sorted(name for name in os.listdir(trace_dir) if name.endswith(".jsonl"))
    rows: list[dict] = []
    for name in names[-max(1, days):]:
        path = os.path.join(trace_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def _requests(records_dir: str, days: int) -> list[dict]:
    """Request log rows, newest last. Bounded by line count, not file size."""
    path = os.path.join(records_dir, "logs", "requests.jsonl")
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-max(1, days) * 500:]


def _is_substantive(text: str) -> bool:
    signals = analyse(text)
    return (len(text.strip()) >= _MIN_SUBSTANTIVE or signals.fences > 0
            or signals.grounding >= 2)


def audit(data_dir: str, days: int = 7) -> list[Finding]:
    """Return contradictions found in stored records."""
    findings: list[Finding] = []
    trace_by_conversation: dict[str, list[dict]] = {}
    for row in _rows(os.path.join(data_dir, "runtime", "trace"), days):
        trace_by_conversation.setdefault(str(row.get("conversation_id", "?")), []).append(row)

    for request_row in _requests(data_dir, days):
        conversation = str(request_row.get("conversation_id", "?"))
        verdict = str(request_row.get("verdict", ""))
        attempts = int(request_row.get("attempts") or 0)
        model = str(request_row.get("model", ""))

        if verdict in ("clean", "stream") and attempts > 0:
            findings.append(Finding("retry-burned", conversation,
                                    f"{model}: delivered on attempt 1 but {attempts} retries ran",
                                    attempts))
        if verdict in ("retry_ok", "retry_best") and attempts >= 1:
            trail = trace_by_conversation.get(conversation, [])
            refusals = sum(1 for row in trail if row.get("classification") in
                           ("refusal", "empty", "salvaged"))
            if refusals == 0:
                findings.append(Finding("retry-burned", conversation,
                                        f"{model}: retried despite no recorded refusal",
                                        attempts))
        if verdict in ("clean", "retry_ok", "retry_best", "salvaged") and attempts >= 0:
            trail = trace_by_conversation.get(conversation, [])
            if trail and not any(row.get("classification") not in ("error-unclassified",)
                                 for row in trail):
                findings.append(Finding("error-unclassified", conversation,
                                        f"{model}: attempt recorded with no classification", 0))

    for row in _rows(os.path.join(data_dir, "runtime", "trace"), days):
        classification = row.get("classification", "")
        conversation = str(row.get("conversation_id", "?"))
        if classification == "error-unclassified":
            findings.append(Finding("error-unclassified", conversation,
                                    "trace row has no usable classification", 0))
    return findings


def audit_conversations(records, days: int = 7) -> list[Finding]:
    """Check delivered snapshots for the things users actually complain about."""
    findings: list[Finding] = []
    for entry in records.list_conversations():
        conversation_id = entry["conversation_id"]
        snapshots = records.read_conversation(conversation_id)
        if not snapshots:
            continue
        messages = snapshots[-1].get("messages") or []
        last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        if last is None:
            continue
        content = str(last.get("content") or "")
        if not content.strip():
            findings.append(Finding("ok-but-empty", conversation_id,
                                    "delivered turn has no text", 0))
            continue
        if starts_with_refusal(content) and not analyse(content).fences:
            findings.append(Finding("delivered-refusal", conversation_id,
                                    content[:80], len(content)))
        if len(content.strip()) < 40 and not _is_substantive(content):
            findings.append(Finding("thin-delivered", conversation_id,
                                    content[:80], len(content)))
    return findings


def summary(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return {"total": len(findings), "by_code": counts,
            "findings": [finding.as_dict() for finding in findings[:50]]}
