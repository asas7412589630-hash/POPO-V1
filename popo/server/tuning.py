"""Live tuning: change behaviour without a restart, with bounds.

The rules here are the ones that keep a tuning endpoint from becoming an outage:

*Every field has a range.* ``retry_max = 1000`` is not a preference, it is a bill.
Out-of-range means rejected, not clamped — silently correcting an operator hides
a typo.

*Only live-safe fields are mutable.* ``bind_port`` or ``ephemeral`` need the
process restarted (socket, writer); pretending otherwise produces a UI that says
"applied" while nothing changed.

*The change is persisted and reported.* A restart must not silently revert the
operator's tuning, and the response says exactly what took effect.
"""

from __future__ import annotations
import json
import os
import tempfile
from dataclasses import dataclass

from popo.config import Config
from popo.errors import ErrorKind, ProxyError

TUNING_FILE = "tuning.json"


@dataclass(frozen=True)
class TuningField:
    name: str
    kind: type
    low: object
    high: object
    needs_restart: bool = False
    note: str = ""
    group: str = "general"
    label: str = ""


# V1's four tuning groups, with each item connected to a live Config field.
FIELDS: tuple[TuningField, ...] = (
    TuningField("retry_on_refusal", bool, False, True,
                note="關閉後仍可重試空回覆或品質不足的答案。", group="retry", label="拒絕自動重試"),
    TuningField("retry_max", int, 0, 20,
                note="額外重試次數，不含第一次請求；8 代表最多 9 次上游呼叫。", group="retry", label="重試次數上限"),
    TuningField("retry_budget_s", float, 0.0, 900.0,
                note="整輪重試的總時間上限；0 代表不再發起重試。", group="retry", label="重試總預算（秒）"),
    TuningField("upstream_budget_s", float, 1.0, 900.0,
                note="單次上游呼叫的逾時上限。", group="retry", label="單次上游預算（秒）"),
    TuningField("compress", bool, False, True,
                note="優先整理較舊工具輸出；超長時保留 system 開頭與結尾並裁去中段。", group="context", label="上下文壓縮"),
    TuningField("fold", bool, False, True,
                note="有損整理：中段歷史換成摘要，原文只在紀錄中保留。", group="context", label="滾動摺疊"),
    TuningField("compress_at_chars", int, 0, 2_000_000,
                group="context", label="壓縮觸發（字元）"),
    TuningField("compress_target_chars", int, 2000, 2_000_000,
                group="context", label="壓縮目標（字元）"),
    TuningField("safe_trim", int, 0, 100_000,
                note="小於此長度的工具結果不修剪。", group="context", label="最小修剪門檻（字元）"),
    TuningField("fold_at_messages", int, 4, 500,
                group="context", label="摺疊觸發訊息數"),
    TuningField("fold_keep_turns", int, 1, 100,
                group="context", label="保留最近輪數"),
    TuningField("advertised_context", int, 1000, 2_000_000,
                note="回報給 /v1/models 的 context_window；不會限制上游。", group="context", label="宣告 context（tokens）"),
    TuningField("context_handoff_at", int, 0, 2_000_000,
                note="0 關閉；達門檻時請模型整理長任務進度。", group="context", label="交接門檻（tokens）"),
    TuningField("context_rotate_at", int, 0, 2_000_000,
                note="0 關閉；有交接摘要且達門檻時縮短舊歷史。", group="context", label="輪替門檻（tokens）"),
    TuningField("state_ledger", bool, False, True,
                note="跨對話保存已確認的標的、符號、位址與工具名稱；不重放舊提示全文。", group="memory", label="狀態帳本"),
    TuningField("archive", bool, False, True,
                note="定期保存對話快照到 runtime/archive。", group="memory", label="定期歸檔"),
    TuningField("artifact", bool, False, True,
                note="把回答中的程式碼區塊存到 artifacts/，以 Markdown 檔保存。", group="memory", label="成品落盤"),
    TuningField("capture_responses", bool, False, True,
                note="另存完整模型回覆到 logs/responses.jsonl。", group="capture", label="擷取回應全文"),
    TuningField("capture_full", bool, False, True,
                note="額外保存完整請求訊息與工具定義；可能包含敏感資料。", group="capture", label="擷取完整請求"),
    TuningField("scrub_refusals", bool, False, True,
                note="移除對話歷史中的拒答回合。", group="capture", label="清除拒絕歷史"),
    TuningField("scrub_prose", bool, False, True,
                note="移除短篇計畫敘述；可能使代理重複規劃，預設關閉。", group="capture", label="清除敘述輪"),
    TuningField("require_client_key", bool, False, True,
                note="對外 API 是否要求用戶端金鑰；關閉會讓任何連線者使用代理。", group="capture", label="對外要求金鑰"),
)

# Public V1 field name -> runtime Config attribute. Keeping the V1 names in the
# API and data file means old panel settings can be migrated without guessing.
CONFIG_NAMES = {
    "retry_budget_s": "retry_budget_seconds",
    "upstream_budget_s": "upstream_timeout",
    "compress": "compress_enabled",
    "fold": "fold_enabled",
    "archive": "archive_enabled",
    "artifact": "artifact_enabled",
    "scrub_refusals": "scrub_enabled",
}
INPUT_ALIASES = {
    # Fields exposed by the short-lived V2 tuning panel remain accepted for API
    # compatibility, but the V1 names are canonical in the new console.
    "retry_budget_seconds": "retry_budget_s",
    "upstream_timeout": "upstream_budget_s",
    "scrub_enabled": "scrub_refusals",
}

RESTART_ONLY = {
    "bind_port": "the listening socket is already bound",
    "bind_host": "the listening socket is already bound",
    "ephemeral": "the record writer is already open",
    "data_dir": "stores are already open on the old path",
}


def describe() -> list[dict]:
    return [{"name": field.name, "type": field.kind.__name__,
             "min": field.low, "max": field.high,
             "needs_restart": field.needs_restart, "note": field.note,
             "group": field.group, "label": field.label or field.name}
            for field in FIELDS]


def current(config: Config) -> dict:
    return {field.name: getattr(config, CONFIG_NAMES.get(field.name, field.name))
            for field in FIELDS}


def load(config: Config, data_dir: str) -> dict:
    """Apply persisted tuning on startup.

    Without this a restart silently reverts the operator's settings, which looks
    like "the tuning page does nothing" rather than "the change was not saved".
    """
    path = os.path.join(data_dir, TUNING_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[POPO] tuning file ignored ({exc})")
        return {}
    if not isinstance(saved, dict):
        return {}
    applied = {}
    for saved_name, raw in saved.items():
        name = INPUT_ALIASES.get(saved_name, saved_name)
        field = next((item for item in FIELDS if item.name == name), None)
        if field is None:
            continue
        try:
            attr = CONFIG_NAMES.get(name, name)
            setattr(config, attr, _coerce(field, raw))
            applied[name] = getattr(config, attr)
        except ProxyError as exc:
            print(f"[POPO] stored tuning {name} rejected: {exc.message}")
    return applied


def save(data_dir: str, applied: dict) -> None:
    """Persist only what changed, atomically, with private permissions."""
    if not data_dir or not applied:
        return
    path = os.path.join(data_dir, TUNING_FILE)
    merged = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
            if isinstance(existing, dict):
                merged = existing
        except (OSError, ValueError):
            merged = {}
    merged.update(applied)
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".tuning-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[POPO] could not persist tuning: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply(config: Config, payload: dict) -> dict:
    """Validate and apply tuning values onto ``config`` in place."""
    if not isinstance(payload, dict):
        raise ProxyError(ErrorKind.BAD_REQUEST, "tuning payload must be an object")

    restart_fields = sorted(name for name in payload if name in RESTART_ONLY)
    if restart_fields:
        detail = "; ".join(f"{name}: {RESTART_ONLY[name]}" for name in restart_fields)
        raise ProxyError(ErrorKind.BAD_REQUEST,
                         f"these settings require a restart instead of a live change: {detail}")

    by_name = {field.name: field for field in FIELDS}
    normalized = {INPUT_ALIASES.get(name, name): value
                  for name, value in payload.items()}
    unknown = sorted(name for name in normalized if name not in by_name)
    if unknown:
        raise ProxyError(ErrorKind.BAD_REQUEST,
                         f"unknown tuning field(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(sorted(by_name))}")

    applied: dict = {}
    for name, raw in normalized.items():
        field = by_name[name]
        applied[name] = _coerce(field, raw)
    for name, value in applied.items():
        setattr(config, CONFIG_NAMES.get(name, name), value)
    return {"ok": True, "applied": applied, "note": "in force for subsequent requests",
            "live": True}


def _coerce(field: TuningField, raw):
    if field.kind is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ProxyError(ErrorKind.BAD_REQUEST, f"{field.name} must be a boolean")
    if field.kind is int:
        if isinstance(raw, bool):
            raise ProxyError(ErrorKind.BAD_REQUEST, f"{field.name} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"{field.name} must be an integer") from exc
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"{field.name} must be a number") from exc
    if not field.low <= value <= field.high:
        # Reject rather than clamp: silently correcting a typo hides it.
        raise ProxyError(
            ErrorKind.BAD_REQUEST,
            f"{field.name}={value} is outside {field.low}..{field.high}")
    return value
