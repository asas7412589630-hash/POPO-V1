"""Long-task context sizing, handoff instructions and history rotation."""

from __future__ import annotations
import json


def estimate_tokens(messages: list[dict]) -> int:
    """Estimate prompt size with the state compressor's CJK-aware counter."""
    from popo.core.stateq import _estimate_tokens
    try:
        return _estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _estimate_tokens(str(messages))


def rotate_messages(messages: list[dict], handoff: str,
                    keep_messages: int) -> list[dict]:
    """Keep the original system, a saved handoff, and a valid recent tail."""
    source = [message for message in messages if message.get("name") != "_popo_handoff"]
    first_system = [source[0]] if source and source[0].get("role") == "system" else []
    start = max(len(first_system), len(source) - max(4, int(keep_messages)))
    # Do not start the retained tail with a tool result detached from its call.
    if start < len(source) and source[start].get("role") == "tool":
        start = max(len(first_system), start - 1)
        while start > len(first_system) and source[start].get("role") == "tool":
            start -= 1
    tail = source[start:]
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    carried = {"role": "system", "name": "_popo_handoff",
               "content": "[已保存的長任務交接摘要；作為前文紀錄使用]\n\n"
                          + handoff.strip()}
    return first_system + [carried] + tail


def append_context_instruction(messages: list[dict], text: str) -> list[dict]:
    """Add a proxy instruction to an existing system message or create one."""
    output = list(messages)
    for index, message in enumerate(output):
        if message.get("role") == "system":
            updated = dict(message)
            current = str(updated.get("content") or "").strip()
            updated["content"] = (current + "\n\n" + text).strip()
            output[index] = updated
            return output
    return [{"role": "system", "name": "_popo_handoff_instruction",
             "content": text}] + output
