"""
Conversation history scrubbing: remove refusal turns before sending upstream.

Stateful armor layer; it must run after injection but before each upstream send
so that previous refusals do not anchor the model.
"""

from __future__ import annotations
import re
from typing import List


_REFUSAL_OPENERS = (
    "我無法", "我沒法", "做不到", "對不起", "sorry", "i can't", "i cannot",
    "i'm not able", "我不能", "無法", "拒絕", "拒绝", "抱歉", "apologize",
)

_EMPTY_NUDGE_RE = re.compile(r"^\s*(繼續|go on|continue|next|proceed|\.+)\s*$", re.I)

# Passive voice often describes what "the system" does, not a refusal.
_PASSIVE_MASKS = ("會被拒絕", "被攔截", "會被阻擋", "被系統")


def _has_code_block(text: str) -> bool:
    return "```" in text or "`" in text


def _is_refusal_turn(role: str, content: str) -> bool:
    if role != "assistant":
        return False
    text = str(content).strip()
    if not text:
        return True
    if _has_code_block(text):
        return False
    opening = text[:120].lower()
    for marker in _REFUSAL_OPENERS:
        if marker.lower() in opening:
            # If it looks like description of a system mechanism, keep it.
            if any(mask in text for mask in _PASSIVE_MASKS):
                return False
            return True
    return False


def _is_empty_nudge(role: str, content: str) -> bool:
    return role == "user" and bool(_EMPTY_NUDGE_RE.match(str(content)))


def _is_prose_only_plan(role: str, content: str) -> bool:
    # Conservative: only remove the assistant's own planning narration if short.
    if role != "assistant":
        return False
    text = str(content).strip()
    if len(text) > 200:
        return False
    plan_words = ("我打算", "我會先", "第一步", "計劃", "plan", "first i will", "i will first")
    return any(w in text for w in plan_words)


def scrub_history(messages: list[dict], *, keep_last_user: bool = True,
                  scrub_prose: bool = False) -> list[dict]:
    """Return a scrubbed copy of messages, dropping refusal/empty turns.

    ``scrub_prose`` is intentionally opt-in: a short planning note can be useful
    context, and removing it can make an agent repeat work.
    """
    cleaned: List[dict] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        is_last_user = keep_last_user and role == "user" and i == len(messages) - 1
        if _is_refusal_turn(role, content):
            continue
        if scrub_prose and _is_prose_only_plan(role, content):
            continue
        if _is_empty_nudge(role, content) and not is_last_user:
            continue
        cleaned.append(dict(msg))

    # If nothing user-side remains, roll back to original.
    user_count = sum(1 for m in cleaned if m.get("role") == "user")
    if user_count == 0:
        return [dict(m) for m in messages]
    return cleaned


def collapse_orphan_users(messages: list[dict]) -> list[dict]:
    """Merge consecutive user turns into the last one, preserving context."""
    out: List[dict] = []
    pending_user: dict | None = None
    for msg in messages:
        if msg.get("role") == "user":
            pending_user = dict(msg)
            continue
        if pending_user is not None:
            out.append(pending_user)
            pending_user = None
        out.append(dict(msg))
    if pending_user is not None:
        out.append(pending_user)
    return out
