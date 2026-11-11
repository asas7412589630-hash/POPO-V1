"""V1-style context compression and rolling fold.

Compression only edits older tool results and keeps the newest results intact.
Folding is deliberately separate and lossy: it replaces the middle of a long
history with a short extractive digest while retaining the original request and
the live tail.
"""
from __future__ import annotations

import json
from typing import Any

COMPRESS_SYSTEM_MAX = 18000


def _text(message: dict[str, Any]) -> str:
    value = message.get("content", "")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(part.get("text") or "") for part in value
                       if isinstance(part, dict) and part.get("type") == "text")
    return "" if value is None else str(value)


def _with_text(message: dict[str, Any], text: str) -> dict[str, Any]:
    clone = dict(message)
    clone["content"] = text
    return clone


def _size(messages: list[dict[str, Any]]) -> int:
    return sum(len(_text(message)) for message in messages if isinstance(message, dict))


def _shrink_json(text: str, target: int) -> str | None:
    """Drop complete older list entries while keeping valid JSON."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, list):
        while len(value) > 1 and len(json.dumps(value, ensure_ascii=False)) > target:
            value.pop(max(0, len(value) // 2))
        candidate = json.dumps(value, ensure_ascii=False)
        return candidate if len(candidate) < len(text) else None
    if isinstance(value, dict):
        # Keep the first and last keys to preserve the shape of common tool output.
        keys = list(value)
        while len(keys) > 2 and len(json.dumps(value, ensure_ascii=False)) > target:
            value.pop(keys.pop(len(keys) // 2), None)
        candidate = json.dumps(value, ensure_ascii=False)
        return candidate if len(candidate) < len(text) else None
    return None


def compress(messages: list[dict[str, Any]], *, threshold: int = 55000,
             target: int = 30000, safe_trim: int = 900,
             system_max: int = COMPRESS_SYSTEM_MAX) -> tuple[list[dict[str, Any]], dict]:
    """Deterministically shrink duplicate and oversized older tool results."""
    result = [dict(message) for message in messages]
    info = {"before": _size(result), "after": 0, "trimmed": 0, "deduped": 0,
            "system_trimmed": 0}
    if threshold <= 0 or info["before"] <= threshold:
        info["after"] = info["before"]
        return result, info
    indexes = [i for i, message in enumerate(result)
               if message.get("role") == "tool" and _text(message)]
    protected = (set(indexes[-min(10, max(1, len(indexes) // 2)):])
                 if indexes else set())
    seen: set[str] = set()
    for index in reversed(indexes):
        if index in protected:
            continue
        content = _text(result[index])
        if len(content) < 200:
            continue
        if content in seen:
            result[index] = _with_text(result[index],
                f"[重複工具輸出已省略；後續步驟保留了相同內容，共 {len(content)} 字]")
            info["deduped"] += 1
        else:
            seen.add(content)
    for index in indexes:
        if _size(result) <= target:
            break
        if index in protected:
            continue
        content = _text(result[index])
        if len(content) <= max(100, safe_trim):
            continue
        shrunk = _shrink_json(content, max(500, target // max(1, len(indexes))))
        if shrunk is None:
            keep = max(200, min(len(content) // 2, target // 4))
            half = max(100, keep // 2)
            shrunk = (content[:half] + f"\n[…中段省略 {len(content) - keep} 字…]\n"
                      + content[-half:])
        if len(shrunk) < len(content):
            result[index] = _with_text(result[index], shrunk)
            info["trimmed"] += 1

    # V1's last-resort system prompt handling keeps the beginning (identity and
    # rules) and end (format requirements), where trimming tool results alone
    # cannot bring the request under its compression target.
    if _size(result) > target and system_max > 0:
        for index, message in enumerate(result):
            if message.get("role") != "system":
                continue
            content = _text(message)
            if len(content) <= system_max:
                continue
            head = int(system_max * 0.62)
            tail = int(system_max * 0.30)
            omitted = len(content) - head - tail
            shortened = (content[:head]
                         + f"\n\n… [{omitted} chars of the system prompt omitted to fit context.] …\n\n"
                         + content[-tail:])
            result[index] = _with_text(message, shortened)
            info["system_trimmed"] += 1
            if _size(result) <= target:
                break
    info["after"] = _size(result)
    return result, info


def _digest(middle: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for message in middle:
        role = str(message.get("role") or "message")
        content = _text(message).strip()
        if not content and role == "assistant":
            calls = message.get("tool_calls") or []
            content = "; ".join(str((call.get("function") or {}).get("name") or "tool")
                                 for call in calls if isinstance(call, dict))
        if not content:
            continue
        for line in content.splitlines():
            clean = " ".join(line.split())
            if not clean:
                continue
            entry = f"{role}: {clean[:260]}"
            if entry not in seen:
                lines.append(entry)
                seen.add(entry)
            if len(lines) >= 24:
                break
        if len(lines) >= 24:
            break
    if not lines:
        return "[中段歷史已整理；原始內容仍保存在紀錄中。]"
    return "[中段歷史摘要；保留重要原文片段]\n" + "\n".join(lines)


def fold(messages: list[dict[str, Any]], *, at_messages: int = 14,
         keep_turns: int = 8) -> tuple[list[dict[str, Any]], dict]:
    """Replace old middle history with a digest and keep the current tail."""
    result = [dict(message) for message in messages]
    info = {"before": len(result), "after": len(result), "folded": 0}
    if len(result) < max(4, at_messages):
        return result, info
    users = [i for i, message in enumerate(result) if message.get("role") == "user"]
    if len(users) < 2:
        return result, info
    head_end = users[0] + 1
    tail_start = max(head_end, len(result) - max(6, keep_turns * 2))
    floor = max(head_end, tail_start - max(6, keep_turns))
    snapped = next((i for i in range(tail_start, floor - 1, -1)
                    if result[i].get("role") == "user"), -1)
    if snapped >= 0:
        tail_start = snapped
    if tail_start <= head_end:
        return result, info
    head, middle, tail = result[:head_end], result[head_end:tail_start], result[tail_start:]
    if not middle:
        return result, info
    digest = {"role": "system", "name": "_popo_fold", "content": _digest(middle)}
    folded = head + [digest] + tail
    info.update(after=len(folded), folded=len(middle))
    return folded, info

