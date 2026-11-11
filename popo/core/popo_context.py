"""
popo/core/popo_context.py — 上下文控制：stateq / summary / fold 統一介面

責任：
- 決定何時啟動狀態壓縮
- 把 messages 轉成送上游的最終 messages
- 提供診斷用的 stats
"""
from __future__ import annotations

from typing import Any

from popo.core.stateq import StateCfg, compress, fold, restore, stats


def apply_context(messages: list[dict[str, Any]], armor_enabled: bool = True,
                  extra_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    對 messages 執行 context 管理。

    回傳 dict:
      messages: 最終 messages
      dropped: 被取代/壓縮掉的消息數
      summary: 人類可讀的壓縮說明
      stats: dict
    """
    cfg = StateCfg(enabled=bool(armor_enabled))
    if extra_cfg:
        cfg2 = StateCfg(
            enabled=extra_cfg.get("enabled", cfg.enabled),
            max_messages=extra_cfg.get("max_messages", cfg.max_messages),
            compress_threshold=extra_cfg.get("compress_threshold", cfg.compress_threshold),
            keep_first_system=extra_cfg.get("keep_first_system", cfg.keep_first_system),
            keep_last_user_pair=extra_cfg.get("keep_last_user_pair", cfg.keep_last_user_pair),
        )
        cfg = cfg2

    result = compress(messages, cfg)
    return {
        "messages": result.messages,
        "dropped": result.dropped,
        "summary": result.summary or "",
        "stats": stats(result.messages),
        "stateq_enabled": cfg.enabled,
    }


def cleanup_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除非原始 stateq system 訊息，用於回寫對話歷史前。"""
    return restore(messages)
