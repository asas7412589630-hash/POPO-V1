#!/usr/bin/env python3
"""POPO-V1 Conversation History Scrubbing Engine (armor_scrub).

Purges poisoned turns (refusals, empty nudges, and permission-seeking narration)
from multi-turn conversation histories before passing to upstream LLM providers.
Protects trailing prefill turns and preserves substantive user requests.

Standard library only. No third-party dependencies.
"""

import os
import sys
from typing import Callable, List, Optional, Tuple

from armor_refusal import (
    REFUSAL_MARKERS,
    REFUSAL_OPENERS,
    REFUSAL_WEAK,
    DOWNGRADE_HEAD_MARKERS,
    _opening,
)

SCRUB_REFUSALS = os.environ.get("ARMOR_SCRUB_REFUSALS", "1") != "0"
SCRUB_MAX_LEN = int(os.environ.get("ARMOR_SCRUB_MAX", "900"))
SCRUB_PROSE = os.environ.get("ARMOR_SCRUB_PROSE", "0") == "1"
SCRUB_PROSE_MAX = int(os.environ.get("ARMOR_SCRUB_PROSE_MAX", "2000"))


def _is_refusals_active() -> bool:
    ap = sys.modules.get("armor_proxy")
    if ap is not None and hasattr(ap, "SCRUB_REFUSALS"):
        return bool(ap.SCRUB_REFUSALS)
    return bool(SCRUB_REFUSALS)


def _is_prose_active() -> bool:
    ap = sys.modules.get("armor_proxy")
    if ap is not None and hasattr(ap, "SCRUB_PROSE"):
        return bool(ap.SCRUB_PROSE)
    return bool(SCRUB_PROSE)

NUDGE_WORDS = frozenset((
    "继续", "繼續", "接着", "接著", "然后", "然後", "再来", "再來", "下一",
    "继续吧", "繼續吧", "往下", "说说", "說說", "试试", "試試", "开始", "開始",
    "go on", "go ahead", "continue", "next", "more", "then", "so", "ok", "okay",
    "yes", "y", "嗯", "嗯嗯", "好", "好的", "行", "可以", "对", "對", "是",
    "please continue", "keep going",
))

NARRATION_MARKERS = (
    "要继续吗", "要繼續嗎", "需要我继续", "需要我繼續", "要我继续", "要我繼續",
    "我打算", "我将会", "我將會", "接下来我会", "接下來我會", "准备下一步",
    "準備下一步", "下一步我", "请确认", "請確認", "确认后我", "確認後我",
    "shall i", "do you want me to", "let me know if you want",
    "should i proceed", "i'll go ahead if",
)

DELIVERABLE_MARKERS = (
    "```", "~~~", "0x", "#include", "#define", ">>>", "def ", "class ", "import ",
    "ldr ", "str ", "mov ", "bl ", "b.eq", "svc ", "ret", "jmp ", "call ",
    "gcc ", "clang", "javac", "adb ", "sudo ", "apt ", "/dev/", ".so",
    ".dex", ".elf", "| ", "$ ", "=>", "==", "->",
)

_LOGGER: Optional[Callable[[str], None]] = None


def set_scrub_logger(fn: Optional[Callable[[str], None]]) -> None:
    """Set custom logger function for scrubbing events."""
    global _LOGGER
    _LOGGER = fn


def _log(msg: str) -> None:
    if _LOGGER is not None:
        try:
            _LOGGER(msg)
            return
        except Exception:
            pass


def _msg_text(m: dict) -> str:
    """Extract flat text from message content."""
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return str(c or "")


def _is_refusal_turn(m: dict) -> bool:
    """True when an assistant message is an unhelpful refusal."""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if m.get("tool_calls") or m.get("function_call"):
        return False
    txt = _msg_text(m)
    if not txt:
        return False
    head = txt.lstrip()[:200].lower()

    if any(d in head for d in DOWNGRADE_HEAD_MARKERS):
        return True

    if "```" in txt or "~~~" in txt:
        return False

    if len(txt) > SCRUB_MAX_LEN:
        return False

    opening = _opening(txt)
    if any(w in opening for w in REFUSAL_WEAK):
        if len(txt) <= 240 and not any(d in txt.lower() for d in DELIVERABLE_MARKERS):
            return True
    strong = [m for m in REFUSAL_MARKERS if m not in REFUSAL_WEAK]
    return any(marker.lower() in head for marker in strong) or \
        any(o.lower() in opening for o in REFUSAL_OPENERS)


def _is_empty_nudge(m: dict) -> bool:
    """True when user message is a short nudge like '继续' or '?'."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    txt = _msg_text(m).strip()
    if not txt or len(txt) > 12:
        return False
    core = txt.strip(" \t\r\n。，、！？!?.,~～…\"'「」（）()")
    if not core:
        return True
    return core.lower() in NUDGE_WORDS


def _is_prose_only_turn(m: dict) -> bool:
    """True when an assistant message contains planning narration without deliverables."""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if m.get("tool_calls") or m.get("function_call"):
        return False
    txt = _msg_text(m).strip()
    if not txt:
        return True
    if len(txt) > SCRUB_PROSE_MAX:
        return False
    low = txt.lower()
    if any(d in low for d in DELIVERABLE_MARKERS):
        return False
    if any(n in low for n in NARRATION_MARKERS):
        return True
    return False


def _is_narration_turn(m: dict) -> bool:
    """True when assistant message is planning or asking for permission."""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if m.get("tool_calls") or m.get("function_call"):
        return False
    txt = _msg_text(m).strip()
    if not txt:
        return True
    low = txt.lower()
    if any(d in low for d in DELIVERABLE_MARKERS):
        return False
    if any(n in low for n in NARRATION_MARKERS):
        return True
    return False


def _collapse_orphan_users(msgs: list) -> list:
    """Resolve runs of adjacent user messages resulting from deleted refusals."""
    out = []
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if not (isinstance(m, dict) and m.get("role") == "user"):
            out.append(m)
            i += 1
            continue
        run = []
        while i < n and isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
            run.append(msgs[i])
            i += 1
        if len(run) == 1:
            out.append(run[0])
            continue
        keep = None
        for cand in reversed(run):
            if not _is_empty_nudge(cand):
                keep = cand
                break
        if keep is None:
            keep = run[-1]
        out.append(keep)
    return out


def scrub_refusals(body: dict) -> Tuple[dict, int]:
    """Purge refusal and narration turns from history."""
    refusals_on = _is_refusals_active()
    prose_on = _is_prose_active()
    if not refusals_on and not prose_on:
        return body, 0
    msgs = body.get("messages")
    if not isinstance(msgs, list) or len(msgs) < 2:
        return body, 0
    body_orig = {"messages": list(msgs)}

    tail = len(msgs) - 1
    protected = tail if isinstance(msgs[tail], dict) and msgs[tail].get("role") == "assistant" else -1
    last_user = -1
    for _i in range(len(msgs) - 1, -1, -1):
        _m = msgs[_i]
        if isinstance(_m, dict) and _m.get("role") == "user":
            last_user = _i
            break

    out, removed, i = [], 0, 0
    while i < len(msgs):
        m = msgs[i]
        if (i != protected and i != last_user
                and isinstance(m, dict) and m.get("role") == "assistant"):
            if refusals_on and _is_refusal_turn(m):
                _log(f"[scrub] dropped refusal turn at index {i} ({len(_msg_text(m))} chars)")
                removed += 1
                i += 1
                while i < len(msgs) and i != protected and _is_refusal_turn(msgs[i]):
                    removed += 1
                    i += 1
                if (i < len(msgs) and i != protected and i != last_user
                        and _is_empty_nudge(msgs[i])):
                    _log(f"[scrub] dropped follow-up nudge at index {i}")
                    i += 1
                continue
            if prose_on and _is_narration_turn(m):
                _log(f"[scrub] dropped narration-only turn at index {i} ({len(_msg_text(m))} chars)")
                removed += 1
                i += 1
                continue
        out.append(m)
        i += 1

    if removed:
        body["messages"] = _collapse_orphan_users(out)
        if not any(isinstance(x, dict) and x.get("role") == "user" for x in body["messages"]):
            return body_orig, 0
    return body, removed
