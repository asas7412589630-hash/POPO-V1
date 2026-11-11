"""
popo/core/stateq.py — 純 body 轉換：摺疊 / 壓� / 回復

注入點：chain 在 armor 判定為 on 後、送上游前呼叫 stateq.fold(messages, cfg)。
不依賴 openai/chat 格式以外的任何結構；只會插入/修改 system 訊息與調整 user 訊息。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# -- 常數 ------------------------------------------------

MAX_MESSAGES_DEFAULT = 20          # 預設保留最近 N 則
MAX_COMPRESS_CHARS_DEFAULT = 1600    # 預設壓縮閾值
COMPRESS_MARKER = "[對話摘要]"
HISTORY_MARKER = "[歷史消息已暫存]"


@dataclass(frozen=True)
class FoldResult:
    """fold 產出的結構，足夠上鏈接下段或送給持有者。"""
    messages: list[dict[str, Any]]
    dropped: int = 0
    summary: str | None = None


@dataclass
class StateCfg:
    enabled: bool = True
    max_messages: int = MAX_MESSAGES_DEFAULT
    compress_threshold: int = MAX_COMPRESS_CHARS_DEFAULT
    keep_first_system: bool = True          # 永遠保留最原始 system
    keep_last_user_pair: bool = True        # 永遠保留最後一則 user(+assistant) 對


def as_state_cfg(cfg: dict[str, Any] | None) -> StateCfg:
    """從設定字典轉出狀態層設定。"""
    if cfg is None:
        return StateCfg(enabled=False)

    d = dict(cfg or {})
    raw = d.get("state", d)
    if isinstance(raw, bool):
        return StateCfg(enabled=raw)
    if not isinstance(raw, dict):
        return StateCfg(enabled=False)
    if isinstance(raw, bool):
        return StateCfg(enabled=raw)
    if not isinstance(raw, dict):
        return StateCfg(enabled=False)
    return StateCfg(
        enabled=raw.get("enabled", True),
        max_messages=raw.get("max_messages", MAX_MESSAGES_DEFAULT),
        compress_threshold=raw.get("compress_threshold", MAX_COMPRESS_CHARS_DEFAULT),
        keep_first_system=raw.get("keep_first_system", True),
        keep_last_user_pair=raw.get("keep_last_user_pair", True),
    )


def _content_text(msg: dict[str, Any]) -> str:
    """對 oai 訊息取可讀文字；不認證格式。"""
    c = msg.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                t = it.get("text") or ""
                parts.append(t)
        return "".join(parts)
    return str(c)


def _is_system(m: dict[str, Any]) -> bool:
    return m.get("role") == "system"


def _is_user(m: dict[str, Any]) -> bool:
    return m.get("role") == "user"


def _is_assistant(m: dict[str, Any]) -> bool:
    return m.get("role") == "assistant"


def _estimate_tokens(text: str) -> int:
    """簡易估算：英文拆字，CJK 每字 1 token，標點 0.5。"""
    # 先把連續空白 normalize
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0
    # CJK
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+", text))
    # 英文單字
    words = len(re.findall(r"[a-zA-Z_]+", text))
    # 數字與其它符號
    others = len(re.findall(r"[^\sA-Za-z_\u4e00-\u9fff]", text))
    return int(cjk + words * 0.6 + others * 0.3)


def _compress_pair(user_msg: dict[str, Any], assistant_msg: dict[str, Any] | None) -> dict[str, Any]:
    """把一對 user/assistant 壓成一行摘要 system 訊息。"""
    user_text = _content_text(user_msg)
    assistant_text = _content_text(assistant_msg) if assistant_msg else ""
    if assistant_text:
        summary = f"{COMPRESS_MARKER} 用戶：{user_text[:120]}\n助手：{assistant_text[:240]}"
    else:
        summary = f"{COMPRESS_MARKER} 用戶：{user_text[:120]}\n（未回覆）"
    return {"role": "system", "name": "_popo_stateq", "content": summary}


def fold(messages: list[dict[str, Any]], cfg: StateCfg | None = None) -> FoldResult:
    """
    把過長歷史折疊成「首 system + 壓縮摘要 + 尾段 N 則」。
    純粹轉換，不改變最後 user 訊息內容。
    """
    cfg = cfg or StateCfg()
    if not cfg.enabled or len(messages) <= cfg.max_messages:
        return FoldResult(messages=list(messages))

    messages = list(messages)
    kept_systems: list[dict[str, Any]] = []
    kept_tail: list[dict[str, Any]] = []

    cursor = 0
    if cfg.keep_first_system and messages and _is_system(messages[0]):
        kept_systems.append(messages[0])
        cursor += 1

    # 尾巴幾則直接保留
    tail_start = max(cursor, len(messages) - cfg.max_messages)
    tail = messages[tail_start:]

    # 中間可壓縮範圍 = cursor .. tail_start
    compress_start = cursor
    compress_end = tail_start

    # 若尾巴最後一對是完整的 user/assistant， always 保留
    last_pair_start = -1
    if cfg.keep_last_user_pair:
        # 從 tail 往前找最後一則 user，並把它後面的 assistant 一起保留
        for i in range(len(tail) - 1, -1, -1):
            if _is_user(tail[i]):
                last_pair_start = i
                break

    if last_pair_start != -1:
        # 把尾巴拆成「可壓縮中段」+「必保留最後 pair」
        pair = tail[last_pair_start:]
        tail = tail[:last_pair_start]
        compress_end = tail_start
        tail_start = len(messages) - len(pair)
        compress_end = tail_start
        tail = messages[tail_start:]
    else:
        pair = []

    # 再把 cursor .. tail_start 依照 user/assistant 成對壓縮
    body = messages[cursor:compress_end]
    compressed: list[dict[str, Any]] = []
    i = 0
    while i < len(body):
        u = body[i]
        a = body[i + 1] if i + 1 < len(body) else None
        if _is_user(u) and (a is None or _is_assistant(a)):
            compressed.append(_compress_pair(u, a))
            i += 2 if a is not None else 1
        else:
            if _is_system(u):
                compressed.append(u)
            else:
                compressed.append(_compress_pair(u, None))
            i += 1

    final = kept_systems + compressed + tail + pair
    dropped = len(messages) - len(final) + max(0, len(compressed) - 1)
    summary = None
    if compressed:
        summary = f"壓縮了 {len(messages) - len(final)} 則消息為 {len(compressed)} 則摘要"
    return FoldResult(messages=final, dropped=dropped, summary=summary)


def compress(messages: list[dict[str, Any]], cfg: StateCfg | None = None) -> FoldResult:
    """
    在 fold 基礎上再做一次「字數壓縮」：若前半段超過 threshold 字符，進一步截短。
    """
    cfg = cfg or StateCfg()
    if not cfg.enabled:
        return FoldResult(messages=list(messages))

    fr = fold(messages, cfg)
    msgs = list(fr.messages)

    total_chars = sum(len(_content_text(m)) for m in msgs)
    if total_chars <= cfg.compress_threshold:
        return fr

    # 從前面開始截短（保留 system 摘要與最後 user pair）
    first_system: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    middle: list[dict[str, Any]] = []

    if msgs and _is_system(msgs[0]):
        first_system = [msgs[0]]

    # 找最後一則 user
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if _is_user(msgs[i]):
            last_user_idx = i
            break

    if last_user_idx > 0:
        tail = msgs[last_user_idx:]
        middle = msgs[len(first_system):last_user_idx]

    # 對 middle 做字數漸進截短
    cap = cfg.compress_threshold
    while middle and sum(len(_content_text(m)) for m in middle) > cap:
        # 優先吃掉最舊的摘要
        if middle:
            middle.pop(0)

    if middle and len(first_system) < 2:
        # 添加一則統一標記
        first_system.append({"role": "system", "name": "_popo_stateq", "content": HISTORY_MARKER})

    compressed = first_system + middle + tail
    summary = fr.summary or ""
    summary += f"; 字數自 {total_chars} 壓縮至 {sum(len(_content_text(m)) for m in compressed)}"
    return FoldResult(messages=compressed, dropped=len(messages) - len(compressed), summary=summary.strip("; "))


def restore(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 stateq 插入的 system 摘要拿掉；如果已有下游使用則不需要呼叫。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("name") == "_popo_stateq" or m.get("content", "").startswith(COMPRESS_MARKER):
            continue
        out.append(m)
    return out


def stats(messages: list[dict[str, Any]]) -> dict[str, int]:
    """統計目前上下文狀態。"""
    by_role: dict[str, int] = {}
    chars = 0
    for m in messages:
        r = m.get("role") or "unknown"
        by_role[r] = by_role.get(r, 0) + 1
        chars += len(_content_text(m))
    return {
        "messages": len(messages),
        "characters": chars,
        "estimated_tokens": _estimate_tokens(" ".join(_content_text(m) for m in messages)),
        **{f"role_{k}": v for k, v in by_role.items()},
    }
