#!/usr/bin/env python3
"""errors — one taxonomy for everything that can go wrong, so a failure names
which side broke.

Why this exists: before it, every failure looked the same to a client. A dead
upstream key, an upstream at capacity, a model id that no longer exists, and the
proxy's own armour failing to inject all produced roughly "an error occurred".
Those need opposite reactions:

    arm.*      the proxy is misconfigured or a bug — the operator must act
    upstream.* the provider is down/limited — retry later, nothing to fix
    model.*    the model refused — the armour needs work, retrying may help
    client.*   the caller's request/key is wrong — the caller must fix it

The vendor-specific noise (logfare's `{"error":{"errors":[{"code":4006}]}}`) is
translated here too, because that shape is not OpenAI-compatible and a client
cannot be expected to parse two dialects.

Every error is emitted in the OpenAI shape with `code` carrying our taxonomy:

    {"error": {"code": "upstream.capacity", "type": "upstream_error",
               "message": "...", "hint": "...", "retryable": true,
               "detail": "<first 400 chars of the vendor body>"}}

and mirrored into the `X-Armor-Error` header.
"""
import re

# kind -> (http status, family, retryable, message, hint)
CATALOG = {
    # ---- the caller's own problem ------------------------------------------
    "client.unauthorized": (
        401, "client_error", False,
        "缺少或無效的 API Key",
        "到「設定」填入你自己的金鑰。管理端點（金鑰/記錄/破甲/調校）"
        "另外要求有效金鑰或啟動權杖；聊天端點不需要，只看 API Key"),
    "client.bad_json": (
        400, "client_error", False,
        "請求 body 不是合法 JSON",
        "檢查 Content-Type 與 JSON 格式"),
    "client.bad_request": (
        400, "client_error", False,
        "請求內容不完整或不合規",
        "看 detail：缺 model / messages 為空，或參數名與型別不合"),
    "client.rate_limited": (
        429, "client_error", True,
        "本代理對你的呼叫做了限流",
        "放慢速度；這是代理自己的保護，不是上游的限流"),
    "proxy.no_route": (
        404, "client_error", False,
        "這個路徑不是代理提供的端點",
        "檢查路徑拼字，或改用 /v1/chat/completions。這不是上游的 404 —— "
        "代理根本沒有把這個請求送出去"),

    # ---- the provider is broken --------------------------------------------
    "upstream.capacity": (
        503, "upstream_error", True,
        "上游容量不足／限流",
        "問題在上游，不在破甲。稍後再送一次即可；重試已自動換過金鑰"),
    "upstream.auth": (
        502, "upstream_error", False,
        "上游拒絕了代理持有的金鑰",
        "金鑰池全部失效——要換 key（proxy/upstream_keys.txt），重試沒用"),
    "upstream.model_not_found": (
        404, "upstream_error", False,
        "上游沒有這個模型",
        "模型可能已下架；換一個 id，或看 /v1/models 現在有什麼"),
    "upstream.unreachable": (
        502, "upstream_error", True,
        "連不上上游",
        "網路或上游主機問題；重試通常會好"),
    "upstream.bad_request": (
        400, "client_error", False,
        "上游拒收了這個請求（4xx）",
        "重試不會成功——這是請求內容的問題，不是上游壞了。看 detail："
        "通常是缺欄位、型別不合，或注入後 shape 不符上游 schema"),
    "upstream.edge_blocked": (
        403, "upstream_error", False,
        "被上游前面的 CDN／WAF 擋住，請求沒到模型",
        "這不是金鑰問題也不是破甲問題。常見於免費或共享主機的出網 IP 被列黑。"
        "解法：設定 ARMOR_UPSTREAM_UA 用正常瀏覽器 UA、改用有帳號的供應商域名、"
        "或把代理搬到住宅 IP"),
    "upstream.server_error": (
        502, "upstream_error", True,
        "上游 5xx",
        "上游內部錯誤；重試"),
    "upstream.empty_stream": (
        502, "upstream_error", True,
        "上游回了空串流",
        "上游 200 但一個字都沒吐（通常是容量）；換金鑰重試也不會好，等一會"),
    "upstream.timeout": (
        504, "upstream_error", True,
        "上游逾時",
        "模型太久沒回應；縮短請求或稍後再試"),

    # ---- the proxy's own machinery -----------------------------------------
    "armor.injection_failed": (
        500, "armor_error", False,
        "破甲注入失敗（請求沒帶破甲就送出去了）",
        "這是代理的 bug/設定錯誤，不是模型的問題。看 /logs 的 [err] injection failed"),
    "armor.no_policy": (
        500, "armor_error", False,
        "找不到破甲文本（空字串）",
        "blocks/ 或 policy 檔缺失；用 /armor?model=… 檢查字數"),
    "armor.unknown_level": (
        400, "armor_error", False,
        "不認得的破甲層級",
        "X-Armor-Level 只接受 native / full / lean"),

    # ---- the model itself ---------------------------------------------------
    "model.refused": (
        200, "model_refusal", False,
        "模型在所有重試後仍然拒答",
        "這不是上游故障。換層級（full）、提高思考強度、或換模型"),
    "model.empty": (
        200, "model_refusal", True,
        "模型沒有輸出任何內容",
        "可能是思考吃掉 token 預算；提高 max_tokens 或降低 effort"),
}


def make(kind: str, detail: str = "", **over):
    """Build the error object for `kind`. Unknown kinds fall back to a generic
    armour_error so a typo never becomes a silent 200."""
    spec = CATALOG.get(kind)
    if spec is None:
        kind = "armor.injection_failed"
        spec = CATALOG[kind]
    status, family, retryable, message, hint = spec
    err = {
        "code": kind,
        "type": family,
        "message": message,
        "hint": hint,
        "retryable": bool(retryable),
        "at": "proxy",
    }
    if kind.startswith("upstream."):
        err["at"] = "upstream"
    elif kind.startswith("model."):
        err["at"] = "model"
    elif kind.startswith("client."):
        err["at"] = "client"
    if detail:
        err["detail"] = str(detail)[:400]
    for k, v in over.items():
        if v is not None:
            err[k] = v
    return {"error": err}


def status_of(kind: str) -> int:
    return (CATALOG.get(kind) or CATALOG["armor.injection_failed"])[0]


def code_in(payload) -> str:
    """Pull `code` out of any error body we might have produced or forwarded."""
    try:
        e = payload.get("error")
        if isinstance(e, dict) and isinstance(e.get("code"), str):
            return e["code"]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Vendor translation
#
# logfare answers 429 with {"error":{"errors":[{"message":"AiError: …","code":4006}]}}
# and 200-with-error-inside for capacity. Those two shapes are not OpenAI's, so a
# client rendering `error.message` shows nothing useful. Classify them once here.
# ---------------------------------------------------------------------------
_CAPACITY = re.compile(
    r"(at capacity|rate limit|rate limiting|too many requests|quota|"
    r"overloaded|capacity|throttle|throttling|token-per-minute|requests per min|"
    r"4006|3021|503|971)", re.I)
_AUTH = re.compile(
    r"(invalid api key|incorrect api key|unauthorized|authentication|"
    r"invalid_api_key|401|403)", re.I)
_MODEL = re.compile(
    r"(model_not_found|model .* not found|no such model|unavailable|"
    r"unknown model|404)", re.I)


# Vendor codes that mean the same as our kinds. Kept small and explicit: a wrong
# mapping here mislabels the responsible party, which is worse than "unknown".
VENDOR_CODES = {
    "model_not_found": "upstream.model_not_found",
    "invalid_api_key": "upstream.auth",
    "authentication_error": "upstream.auth",
    "insufficient_quota": "upstream.capacity",
    "rate_limit_exceeded": "upstream.capacity",
    "context_length_exceeded": "client.bad_request",
}


def classify_error_obj(obj) -> str:
    """Classify an error object that arrived inside a 200 (stream or body).

    Handles the vendor shape too: the useful text lives one level down in
    `errors[0]`, and reading only the top level found neither a message nor a
    code — every capacity error was classified `upstream.unreachable`
    (measured 2026-10-31).
    """
    try:
        if not isinstance(obj, dict):
            return "upstream.server_error"
        # Flatten the vendor envelope before looking for signals.
        parts = []
        code = str(obj.get("code") or obj.get("type") or "")
        parts.append(str(obj.get("message") or ""))
        if code:
            parts.append(code)
        nested = obj.get("errors")
        if isinstance(nested, list):
            for e in nested:
                if isinstance(e, dict):
                    parts.append(str(e.get("message") or ""))
                    nc = str(e.get("code") or "")
                    if nc:
                        parts.append(nc)
                        if nc in VENDOR_CODES:
                            return VENDOR_CODES[nc]
        for key, target in VENDOR_CODES.items():
            if code == key:
                return target
        blob = " ".join(p for p in parts if p)[:2000]
        if not blob.strip():
            return "upstream.server_error"
        # status 0 means "body only": the regexes fall through to text matching.
        return classify_upstream(0, blob)
    except Exception:
        return "upstream.server_error"


def classify_upstream(status: int, body: str = "") -> str:
    """Map an upstream failure to one of our upstream.* kinds.

    Status is checked before body text: a 404 with "capacity" in it is still a
    404, and an upstream that returns 200 with an error payload is classified by
    the payload alone (status_override=0 is used for that path).
    """
    b = (body or "")[:4000]
    if status in (401, 403):
        return "upstream.auth"
    if status == 404:
        return "upstream.model_not_found"
    if status == 429:
        return "upstream.capacity"
    if status in (500, 502, 503, 504):
        # A 5xx whose body names capacity is a capacity problem wearing a 5xx.
        if _CAPACITY.search(b):
            return "upstream.capacity"
        return "upstream.server_error"
    # Remaining 4xx: not auth (401/403), not a model miss (404), not capacity
    # (429). Whatever is left is a request the upstream refuses on its merits —
    # retrying it forever will never succeed.
    #
    # This branch used to be missing, so 400/405/409/413/415/422 fell all the
    # way through to `upstream.server_error` with retryable=True. Measured
    # 2026-11-03: POST /v1/messages with an empty body got
    #   {"code": "upstream.server_error", "retryable": true,
    #    "message": "上游 5xx", "upstream_status": 400}
    # while the upstream plainly answered 400 "Missing 'model' field". Clients
    # (Claude Code, Cline) act on `retryable`, so the caller was told to storm
    # a request that could never work — and the failure was attributed to the
    # provider instead of the body.
    if 400 <= status < 500:
        if _AUTH.search(b):
            return "upstream.auth"
        if _MODEL.search(b):
            return "upstream.model_not_found"
        if _CAPACITY.search(b):
            return "upstream.capacity"
        return "upstream.bad_request"
    # NOTE: status 0 means "we only have a body" (an error object embedded in a
    # 200, or a stream event). It must fall through to TEXT matching, not be
    # treated as a dead connection — that made every in-band capacity error
    # classify as upstream.unreachable (measured 2026-10-31). A genuine
    # connection failure is reported by the caller as -1.
    if status == -1:
        return "upstream.unreachable"
    if _AUTH.search(b):
        return "upstream.auth"
    if _MODEL.search(b):
        return "upstream.model_not_found"
    if _CAPACITY.search(b):
        return "upstream.capacity"
    return "upstream.server_error"


def describe(kind: str) -> str:
    spec = CATALOG.get(kind) or CATALOG["armor.injection_failed"]
    return "%s — %s（%s）" % (kind, spec[3], spec[4])
