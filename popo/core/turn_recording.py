"""Best-effort persistence for complete buffered and streamed turns."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from popo.storage.records import RecordStore


def _content_of(data: dict) -> str:
    choices = data.get("choices") or [{}]
    return str((choices[0] or {}).get("message", {}).get("content", "") or "")


def _usage_of(data: dict, messages: list[dict]) -> tuple[int, int]:
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    if not tokens_in:
        tokens_in = sum(len(str(message.get("content", ""))) for message in messages) // 4
    if not tokens_out:
        tokens_out = len(_content_of(data)) // 4
    return tokens_in, tokens_out


def _last_user(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def trace(records: RecordStore, ctx: Any, attempt: int,
          classification: str, *, data: dict | None = None,
          text: str | None = None, error: str = "", status: int | None = None) -> None:
    body = text if text is not None else (_content_of(data) if data else "")
    ctx.attempt_records.append({"n": attempt, "kind": classification,
        "status": (status or 502) if error else 200, "model": ctx.request.model,
        "chars": len(body), "text": body, "note": error[:300],
        "at": round(time.time() - ctx.started_at, 2)})
    try:
        records.append_trace(conversation_id=ctx.conversation_id or "unknown",
            attempt=attempt, direction="upstream",
            record={"classification": classification, "model": ctx.request.model})
    except OSError as exc:
        print(f"[POPO] trace write failed: {exc}")


def record_success(records: RecordStore, ctx: Any, data: dict,
                   attempts: int, verdict: str) -> None:
    tokens_in, tokens_out = _usage_of(data, ctx.messages)
    text = _content_of(data)
    try:
        records.append_request(conversation_id=ctx.conversation_id or "unknown",
            model=ctx.request.model, armor_blocks=ctx.armor_blocks, attempts=attempts,
            verdict=verdict, tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=int((time.time() - ctx.started_at) * 1000),
            client_key_id=ctx.client_key_id)
        records.write_usage(key_id=ctx.client_key_id or "anonymous",
            conversation_id=ctx.conversation_id or "unknown", model=ctx.request.model,
            tokens_in=tokens_in, tokens_out=tokens_out)
        original = ctx.original_request or ctx.request
        completed = [dict(message) for message in original.messages]
        message = (((data.get("choices") or [{}])[0].get("message") or {})
                   if isinstance(data, dict) else {})
        assistant_message = {"role": "assistant", "content": text}
        for field in ("tool_calls", "function_call"):
            if message.get(field):
                assistant_message[field] = message[field]
        completed.append(assistant_message)
        records.write_conversation(conversation_id=ctx.conversation_id or "unknown",
                                   messages=completed)
        _record_turn(records, ctx, data, verdict)
        if ctx.config.capture_responses:
            records.capture_response({"ts": _now(), "conversation_id": ctx.conversation_id,
                                     "model": ctx.request.model, "text": text})
        if ctx.config.state_ledger:
            update = getattr(records, "update_ledger", None)
            if callable(update):
                update(ctx.client_key_id or ctx.conversation_id,
                       list(original.messages) + [assistant_message])
        if ctx.config.artifact_enabled:
            records.write_artifacts(text, ctx.conversation_id)
        if ctx.config.archive_enabled:
            records.archive_snapshot(ctx.conversation_id, completed)
        if ctx.handoff_turn and ctx.config.state_ledger:
            save = getattr(records, "save_handoff", None)
            if callable(save):
                save(ctx.conversation_id, text, ctx.request.model)
    except OSError as exc:
        print(f"[POPO] record write failed: {exc}")


def record_failure(records: RecordStore, ctx: Any, attempts: int,
                   verdict: str, data: dict | None = None, error: str = "") -> None:
    try:
        records.append_request(conversation_id=ctx.conversation_id or "unknown",
            model=ctx.request.model, armor_blocks=ctx.armor_blocks,
            attempts=attempts, verdict=verdict, client_key_id=ctx.client_key_id)
        _record_turn(records, ctx, data or {}, verdict, error=error)
    except OSError as exc:
        print(f"[POPO] request log write failed: {exc}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record_turn(records: RecordStore, ctx: Any, data: dict,
                 verdict: str, error: str = "") -> None:
    original = ctx.original_request or ctx.request
    usage = data.get("usage") or {}
    message = (((data.get("choices") or [{}])[0].get("message") or {})
               if isinstance(data, dict) else {})
    tool_calls = message.get("tool_calls") or []
    final = _content_of(data) if data else ""
    reasoning = str(message.get("reasoning_content") or message.get("analysis") or "")
    row = {"ts": time.time(), "client": ctx.client_key_id or "anonymous",
        "conv": ctx.conversation_id or "unknown", "model": ctx.request.model,
        "final_model": (data.get("model") if isinstance(data, dict) else "") or ctx.request.model,
        "level": ctx.armor_level,
        "msgs": len(original.messages), "user": _last_user(original.messages)[:600],
        "attempts": ctx.attempt_records, "verdict": verdict, "final": final,
        "error": error[:500], "elapsed": round(time.time() - ctx.started_at, 2),
        "final_chars": len(final), "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "think": reasoning, "think_chars": len(reasoning),
        "reasoning_tokens": ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                             or (usage.get("output_tokens_details") or {}).get("reasoning_tokens")),
        "cached_tokens": ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                          or (usage.get("input_tokens_details") or {}).get("cached_tokens")),
        "tool_calls": len(tool_calls),
        "tool_names": [str(((call.get("function") or {}).get("name") or ""))
                       for call in tool_calls[:6] if isinstance(call, dict)]}
    if ctx.config.capture_full:
        row["request"] = original.to_openai()
    records.append_turn(row)
