#!/usr/bin/env python3
"""acct_http — 帳戶層的 HTTP 介面 + 「給別人看的回看連結」頁面。

刻意獨立於 armor_proxy.py（該檔 8,800 行，熱路徑已經很難改）。入口只有一個：

    acct_http.handle(handler, path, query) -> bool      # True = 已回應

三組東西：

  /a/*        帳戶 API（session cookie 驗證；寫入另需 X-Armor-Admin: 1）
  /j/<vid>    公開回看頁 — 最終對話（無需登入，靠不可猜的 vid + 到期）
  /i/<vid>    公開回看頁 — 含思考與被丟棄的重試（Nono 要的「思考重試內容」）

為什麼回看頁是伺服器渲染而不是把資料塞進 URL：
  * URL 會留在對方的手機、截圖、聊天記錄裡。資料不進 URL 就不會外流。
  * 撤銷得了：刪掉一筆 view，連結立刻失效能，而且長度可調、可數點擊。
  * 內容全部從本機 trace 檔渲染，不經過任何第三方服務。

金鑰與對話只存在 proxy/*.json 與 runtime/trace/ 裡，沒有任何對外同步。
"""
import html
import json
import os
import time
import urllib.parse

import accounts as A

_HERE = os.path.dirname(os.path.abspath(__file__))
_ctx = {"tunnel_url": None, "trace": None, "public_base": ""}

SESSION_COOKIE = "triad_sid"


def configure(tunnel_url=None, trace=None, public_base: str = "") -> None:
    """由 armor_proxy 在啟動時注入；不注入也能跑（只是連結會用本機位址）。"""
    _ctx["tunnel_url"] = tunnel_url
    _ctx["trace"] = trace
    _ctx["public_base"] = public_base


def _trace():
    if _ctx["trace"] is not None:
        return _ctx["trace"]
    try:
        import trace_rec
        return trace_rec
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _q(query: str) -> dict:
    out = {}
    for kv in (query or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
    return out


def _cookies(h) -> dict:
    raw = h.headers.get("Cookie") or ""
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _sid(h) -> str:
    return _cookies(h).get(SESSION_COOKIE, "")


def _is_https(h) -> bool:
    proto = (h.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return bool(h.headers.get("CF-Connecting-IP"))


def _client_ip(h) -> str:
    for k in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        v = (h.headers.get(k) or "").strip()
        if v:
            return v.split(",")[0].strip()
    return h.client_address[0] if h.client_address else ""


def _body(h) -> dict:
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except Exception:
        n = 0
    if not n:
        return {}
    try:
        return json.loads(h.rfile.read(n).decode("utf-8")) or {}
    except Exception:
        return {}


def _admin_header(h) -> bool:
    return (h.headers.get("X-Armor-Admin") or "").strip() == "1"


def json_out(h, status: int, obj) -> None:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(payload)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    try:
        h.wfile.write(payload)
    except Exception:
        pass


def _set_cookie(h, sid: str, ttl: int, clear: bool = False) -> None:
    if clear:
        val = "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % SESSION_COOKIE
    else:
        val = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
            SESSION_COOKIE, sid, max(60, int(ttl)))
    if _is_https(h):
        val += "; Secure"
    h.send_header("Set-Cookie", val)


def _public_base(h) -> str:
    if _ctx["tunnel_url"]:
        try:
            u = _ctx["tunnel_url"]()
            if u:
                return u.rstrip("/")
        except Exception:
            pass
    if _ctx.get("public_base"):
        return _ctx["public_base"].rstrip("/")
    host = (h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or "").split(",")[0].strip()
    proto = "https" if _is_https(h) else "http"
    return ("%s://%s" % (proto, host)) if host else ""


# ---------------------------------------------------------------------------
# 身分
# ---------------------------------------------------------------------------
def _session(h) -> dict:
    return A.resolve(_sid(h))


def _require_session(h) -> dict:
    s = _session(h)
    if not s:
        json_out(h, 401, {"ok": False, "error": "請先登入"})
        return {}
    return s


def _require_owner(h, s) -> bool:
    if ((s.get("user") or {}).get("role") or "") != "owner":
        json_out(h, 403, {"ok": False, "error": "需要 owner 權限"})
        return False
    return True


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------
def handle(h, path: str, query: str) -> bool:
    if path.startswith("/j/"):
        return _view_page(h, path[3:].strip("/"), kind="chat")
    if path.startswith("/i/"):
        return _view_page(h, path[3:].strip("/"), kind="think")
    if path == "/a" or path.startswith("/a/"):
        return _api(h, path, query)
    return False


def _api(h, path, query) -> bool:
    method = h.command
    if path == "/a/login" and method == "POST":
        return _login(h)
    if path == "/a/logout" and method == "POST":
        return _logout(h)
    if path == "/a/me" and method in ("GET", "HEAD"):
        return _me(h)
    if path == "/a/password" and method == "POST":
        return _password(h)
    if path == "/a/users" and method == "GET":
        return _users_get(h)
    if path == "/a/users" and method == "POST":
        return _users_post(h)
    if path == "/a/keys" and method == "GET":
        return _keys_get(h)
    if path == "/a/keys" and method == "POST":
        return _keys_post(h)
    if path == "/a/keys" and method == "DELETE":
        return _keys_del(h)
    if path == "/a/views" and method == "GET":
        return _views_get(h)
    if path == "/a/views" and method == "POST":
        return _views_post(h)
    if path == "/a/views" and method == "DELETE":
        return _views_del(h)
    if path == "/a/usage" and method == "GET":
        return _usage_get(h)
    if path == "/a/conv" and method == "GET":
        return _conv_get(h, query)
    if path == "/a/sessions" and method == "GET":
        return _sessions_get(h)
    if path == "/a/sessions" and method == "DELETE":
        return _sessions_del(h)
    if path == "/a/settings" and method == "GET":
        return _settings_get(h)
    if path == "/a/settings" and method == "POST":
        return _settings_post(h)
    json_out(h, 404, {"ok": False, "error": "no such account route"})
    return True


# ---------------------------------------------------------------------------
# 登入 / 登出 / 自己
# ---------------------------------------------------------------------------
def _login(h) -> bool:
    b = _body(h)
    name = str(b.get("name") or b.get("user") or "").strip()
    pw = str(b.get("password") or "")
    ua = h.headers.get("User-Agent") or ""
    # 空 store 首次登入自動 bootstrap 建立 owner
    if A.count_users() == 0:
        if len(pw) < 8:
            json_out(h, 400, {"ok": False, "error": "密碼至少 8 個字元"})
            return True
        A.ensure_owner(name or "owner", pw)
    r = A.login(name or "owner", pw, ip=_client_ip(h), ua=ua)
    if not r.get("ok"):
        json_out(h, 429 if r.get("retry_after") else 403, {
            "ok": False, "error": r.get("error") or "登入失敗",
            "retry_after": r.get("retry_after") or 0})
        return True
    payload = {"ok": True, "user": r["user"], "ttl": r["ttl"]}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(200)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    _set_cookie(h, r["sid"], r["ttl"])
    h.end_headers()
    try:
        h.wfile.write(data)
    except Exception:
        pass
    return True


def _logout(h) -> bool:
    A.revoke(_sid(h))
    data = json.dumps({"ok": True}, ensure_ascii=False).encode()
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    _set_cookie(h, "", 0, clear=True)
    h.end_headers()
    try:
        h.wfile.write(data)
    except Exception:
        pass
    return True


def _me(h) -> bool:
    s = _session(h)
    if not s:
        json_out(h, 200, {"ok": True, "authenticated": False,
                          "bootstrap": A.count_users() == 0})
        return True
    u = s.get("user") or {}
    uid = u.get("id") or ""
    json_out(h, 200, {
        "ok": True, "authenticated": True,
        "user": A._public_user(u),
        "keys": A.list_tokens(uid),
        "views": A.list_views(uid)[:50],
        "usage_today": A.usage_today(uid),
        "is_owner": u.get("role") == "owner",
        "public_base": _public_base(h),
    })
    return True


def _password(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    old = str(b.get("old") or "")
    new = str(b.get("new") or "")
    if len(new) < 8:
        json_out(h, 400, {"ok": False, "error": "新密碼至少 8 個字元"})
        return True
    uid = (s.get("user") or {}).get("id") or ""
    # 驗舊密碼：換密碼不能只靠一個被撿走的 session cookie。
    if not A.login(((s.get("user") or {}).get("name") or ""), old,
                   ip=_client_ip(h)).get("ok"):
        json_out(h, 403, {"ok": False, "error": "舊密碼不正確"})
        return True
    ok = A.set_password(uid, new)
    json_out(h, 200 if ok else 400,
             {"ok": ok, "note": "所有工作階段已撤銷，請重新登入" if ok else "失敗"})
    return True


# ---------------------------------------------------------------------------
# 帳戶管理（owner）
# ---------------------------------------------------------------------------
def _users_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _require_owner(h, s):
        return True
    rows = []
    for u in A.list_users():
        r = dict(u)
        r["keys"] = len(A.list_tokens(u["id"]))
        r["usage_today"] = A.usage_today(u["id"])
        rows.append(r)
    json_out(h, 200, {"ok": True, "users": rows})
    return True


def _users_post(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _require_owner(h, s):
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    act = str(b.get("action") or "create").strip()
    name = str(b.get("name") or "").strip()
    if act == "create":
        if not name:
            json_out(h, 400, {"ok": False, "error": "需要帳戶名稱"})
            return True
        if A.find_by_name(name):
            json_out(h, 409, {"ok": False, "error": "名稱已被使用"})
            return True
        r = A.create_user(name, str(b.get("password") or ""),
                          "owner" if b.get("role") == "owner" else "user")
        pw = r.pop("password", "")
        json_out(h, 200, {"ok": True, "user": r, "password": pw})
        return True
    u = A.find_by_name(name)
    if not u:
        json_out(h, 404, {"ok": False, "error": "找不到帳戶"})
        return True
    uid = u["id"]
    if act == "enable":
        json_out(h, 200, {"ok": A.set_enabled(uid, True)})
        return True
    if act == "disable":
        if uid == (s.get("user") or {}).get("id"):
            json_out(h, 400, {"ok": False, "error": "不能停用自己"})
            return True
        json_out(h, 200, {"ok": A.set_enabled(uid, False)})
        return True
    if act == "reset":
        pw = str(b.get("password") or "") or A.secrets.token_urlsafe(12)
        json_out(h, 200, {"ok": A.set_password(uid, pw), "password": pw})
        return True
    if act == "delete":
        if uid == (s.get("user") or {}).get("id"):
            json_out(h, 400, {"ok": False, "error": "不能刪除自己"})
            return True
        json_out(h, 200, {"ok": A.delete_user(uid)})
        return True
    json_out(h, 400, {"ok": False, "error": "unknown action"})
    return True


# ---------------------------------------------------------------------------
# 金鑰（帳戶範圍）
# ---------------------------------------------------------------------------
def _keys_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    u = s.get("user") or {}
    uid = "" if u.get("role") == "owner" and _q(h.path.split("?")[1] if "?" in h.path else "").get("all") else u.get("id")
    rows = A.list_tokens(uid)
    if u.get("role") == "owner" and not uid:
        # owner 看全部時補上擁有者名字，方便辨識
        for r in rows:
            r["owner"] = (A.get_user(r["uid"]) or {}).get("name", "")
    json_out(h, 200, {"ok": True, "keys": rows})
    return True


def _keys_post(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    me = (s.get("user") or {})
    target = str(b.get("uid") or "").strip()
    if target and target != me.get("id"):
        if me.get("role") != "owner":
            json_out(h, 403, {"ok": False, "error": "只能建立自己的金鑰"})
            return True
    else:
        target = me.get("id") or ""
    r = A.mint_token(target, str(b.get("label") or ""))
    json_out(h, 200 if r.get("ok") else 400, r)
    return True


def _keys_del(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    kid = str(b.get("id") or "").strip()
    me = (s.get("user") or {})
    hard = bool(b.get("hard"))
    uid = "" if me.get("role") == "owner" else (me.get("id") or "")
    ok = A.revoke_token(uid, kid, hard=hard)
    json_out(h, 200 if ok else 404, {"ok": ok})
    return True


# ---------------------------------------------------------------------------
# 回看連結
# ---------------------------------------------------------------------------
def _views_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    u = s.get("user") or {}
    uid = "" if u.get("role") == "owner" and _q(
        h.path.split("?")[1] if "?" in h.path else "").get("all") else u.get("id")
    base = _public_base(h)
    rows = A.list_views(uid)
    now = time.time()
    for r in rows:
        r["expired"] = bool(r.get("exp")) and now > float(r["exp"])
        r["url"] = "%s/%s/%s" % (base, "j" if r.get("kind") == "chat" else "i",
                                 r.get("vid"))
    json_out(h, 200, {"ok": True, "views": rows, "base": base})
    return True


def _views_post(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    conv = str(b.get("conv") or "").strip()
    if not conv:
        json_out(h, 400, {"ok": False, "error": "需要對話 id"})
        return True
    kind = str(b.get("kind") or "chat")
    try:
        ttl = int(b.get("ttl") or 0)
    except Exception:
        ttl = 0
    uid = (s.get("user") or {}).get("id") or ""
    v = A.new_view(uid, kind, conv, ttl, note=str(b.get("note") or ""))
    base = _public_base(h)
    v["url"] = "%s/%s/%s" % (base, "j" if v["kind"] == "chat" else "i", v["vid"])
    json_out(h, 200, v)
    return True


def _views_del(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    vid = str(b.get("vid") or "").strip()
    u = s.get("user") or {}
    uid = "" if u.get("role") == "owner" else (u.get("id") or "")
    json_out(h, 200, {"ok": A.revoke_view(uid, vid)})
    return True


# ---------------------------------------------------------------------------
# 用量 / 對話
# ---------------------------------------------------------------------------
def _usage_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    u = s.get("user") or {}
    qs = _q(h.path.split("?")[1] if "?" in h.path else "")
    uid = u.get("id") or ""
    if qs.get("uid") and u.get("role") == "owner":
        uid = qs["uid"]
    try:
        days = max(1, min(120, int(qs.get("days") or 14)))
    except Exception:
        days = 14
    json_out(h, 200, {"ok": True, "uid": uid, "days": days,
                      "rows": A.usage(uid, days)})
    return True


def _records_for(uid: str, days: int = 7, conv: str = "", limit: int = 200) -> list:
    """帳戶範圍的回合。uid='' 表示 owner（看全部）。

    沒有 uid 的舊記錄（帳戶層之前產生的）只有 owner 看得到：那些對話屬於誰
    無法判定，把它們端給一般帳戶就是把別人的內容送出去。
    """
    T = _trace()
    if T is None:
        return []
    try:
        allr = T.list_records(limit=limit, conv=conv or None, days=days)
    except Exception:
        allr = []
    rows = []
    for r in allr:
        ruid = r.get("uid") or ""
        if uid and ruid != uid:
            continue
        rows.append(r)
    return rows


def _conv_get(h, query) -> bool:
    """帳戶範圍的對話檢視：列出自己的對話，或讀取其中一個的內容。"""
    s = _require_session(h)
    if not s:
        return True
    u = s.get("user") or {}
    uid = u.get("id") or ""
    qs = _q(query)
    T = _trace()
    if T is None:
        json_out(h, 200, {"ok": True, "convs": [], "records": []})
        return True
    # owner 看全部（它本來就管所有帳戶的用量與金鑰），一般帳戶只看自己。
    # `mine=1` 讓 owner 也能只看自己的。
    if u.get("role") == "owner" and not qs.get("mine"):
        uid = ""
    conv = qs.get("conv") or ""
    if conv:
        rows = _records_for(uid, days=int(qs.get("days") or 7), conv=conv, limit=200)
        out = []
        for r in rows:
            rid = r.get("id") or ""
            full = None
            try:
                full = T.get_record(rid) if rid else None
            except Exception:
                full = None
            out.append(full or r)
        json_out(h, 200, {"ok": True, "conv": conv, "records": out})
        return True
    days = 7
    try:
        days = max(1, min(60, int(qs.get("days") or 7)))
    except Exception:
        days = 7
    rows = _records_for(uid, days=days, limit=500)
    seen = {}
    for r in rows:
        c = r.get("conv") or "-"
        e = seen.setdefault(c, {"conv": c, "turns": 0, "last": 0, "retries": 0,
                                "model": r.get("model") or "", "chars": 0})
        e["turns"] += 1
        e["last"] = max(e["last"], float(r.get("ts") or 0))
        e["retries"] += int(r.get("retries") or 0)
        e["chars"] += int(r.get("final_chars") or 0)
        if r.get("model"):
            e["model"] = r["model"]
    convs = sorted(seen.values(), key=lambda x: -x["last"])
    json_out(h, 200, {"ok": True, "days": days, "convs": convs})
    return True


def _sessions_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    uid = (s.get("user") or {}).get("id") or ""
    rows = A.list_sessions(uid)
    cur = A._sid_hash(_sid(h))[:8]
    for r in rows:
        r["current"] = r.get("sid8") == cur
    json_out(h, 200, {"ok": True, "sessions": rows})
    return True


def _sessions_del(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    uid = (s.get("user") or {}).get("id") or ""
    n = A.revoke_all(uid)
    json_out(h, 200, {"ok": True, "revoked": n,
                      "note": "已撤銷全部工作階段，包括這一個"})
    return True


def _settings_get(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _require_owner(h, s):
        return True
    json_out(h, 200, {"ok": True, "settings": A.all_settings(),
                      "stats": A.stats()})
    return True


def _settings_post(h) -> bool:
    s = _require_session(h)
    if not s:
        return True
    if not _require_owner(h, s):
        return True
    if not _admin_header(h):
        json_out(h, 403, {"ok": False, "error": "缺少 X-Armor-Admin"})
        return True
    b = _body(h)
    changed = {}
    for k, v in (b.get("settings") or {}).items():
        if A.set_setting(k, v):
            changed[k] = v
    json_out(h, 200, {"ok": True, "changed": changed,
                      "settings": A.all_settings()})
    return True


# ---------------------------------------------------------------------------
# 公開回看頁
# ---------------------------------------------------------------------------
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'")


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _fmt(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "-"


_CSS = """
:root{--bg:#0a0d12;--panel:#121822;--fg:#e8edf4;--dim:#8b98a8;--line:#1e2733;
--amber:#ff9d4d;--teal:#4ecdc4;--red:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:
radial-gradient(1100px 600px at 88% -8%,rgba(255,157,77,.11),transparent 62%),
radial-gradient(900px 520px at -10% 108%,rgba(78,205,196,.09),transparent 60%),var(--bg);
color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",
"Noto Sans TC",system-ui,sans-serif;padding:22px 16px 70px}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:17px;margin:0 0 4px;letter-spacing:.2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px;word-break:break-all}
.card{background:linear-gradient(180deg,#141a23,#111721);border:1px solid var(--line);
border-radius:14px;padding:16px 16px 14px;margin-bottom:14px}
.turnhd{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:11.5px;
color:var(--dim);margin-bottom:10px}
.tag{border:1px solid var(--line);border-radius:99px;padding:1px 9px;font-size:11px}
.tag.ok{color:var(--teal);border-color:rgba(78,205,196,.4)}
.tag.warn{color:var(--amber);border-color:rgba(255,157,77,.4)}
.tag.bad{color:var(--red);border-color:rgba(255,107,107,.4)}
.user{background:rgba(255,255,255,.04);border-left:2px solid var(--amber);
padding:8px 12px;border-radius:0 8px 8px 0;margin-bottom:12px;font-size:13.5px;
color:#cdd6e2;white-space:pre-wrap;word-break:break-word}
.think{border:1px solid var(--line);border-radius:10px;margin:0 0 12px;overflow:hidden}
.think>summary{cursor:pointer;padding:9px 12px;font-size:12.5px;color:var(--dim);
list-style:none;background:rgba(78,205,196,.05)}
.think>summary::-webkit-details-marker{display:none}
.think>summary:before{content:"▸ ";color:var(--teal)}
.think[open]>summary:before{content:"▾ "}
.think pre{margin:0;padding:12px;border-top:1px solid var(--line);font-size:12.5px;
line-height:1.6;color:#9fb0c3;white-space:pre-wrap;word-break:break-word;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.answer{white-space:pre-wrap;word-break:break-word;font-size:14.5px}
code{background:rgba(255,255,255,.06);padding:1px 5px;border-radius:5px;font-size:13px}
pre.answer{background:#0c1119;border:1px solid var(--line);border-radius:10px;
padding:12px;overflow-x:auto}
.att{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0 0 8px;
font-size:12.5px}
.att .lab{font-size:11px;color:var(--dim);margin-bottom:6px;display:flex;gap:8px;
flex-wrap:wrap;align-items:center}
.att .txt{white-space:pre-wrap;word-break:break-word;color:#a8b6c6;
max-height:340px;overflow:auto;font-family:ui-monospace,Menlo,monospace}
.att.rej{border-color:rgba(255,107,107,.28);background:rgba(255,107,107,.045)}
.att.good{border-color:rgba(78,205,196,.3);background:rgba(78,205,196,.05)}
.empty{color:var(--dim);text-align:center;padding:44px 0;font-size:13.5px}
footer{color:#54616f;font-size:11px;text-align:center;margin-top:26px}
"""


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">%s"
            "</div></body></html>" % (_esc(title), _CSS, body))


def _view_page(h, vid: str, kind: str) -> bool:
    v = A.resolve_view(vid)
    if not v:
        body = ('<div class="card"><h1>連結無效或已過期</h1>'
                '<div class="sub">這個回看連結已被撤銷、到期，或從未存在。</div></div>')
        _send_html(h, 404, _page("連結無效", body))
        return True

    T = _trace()
    conv = v.get("conv") or ""
    want_think = (v.get("kind") == "think") or (kind == "think")
    recs = []
    if T is not None and conv:
        try:
            rows = T.list_records(limit=300, conv=conv, days=60)
        except Exception:
            rows = []
        for r in rows:
            rid = r.get("id")
            full = None
            try:
                full = T.get_record(rid) if rid else None
            except Exception:
                full = None
            rec = full or r
            # 對話 id 由客戶端生成，兩個帳戶撞 id 是可想像的，而回看頁是公開的。
            # 記錄有明確擁有者且與連結的擁有者不同，就不渲染。
            if rec.get("uid") and v.get("uid") and rec["uid"] != v["uid"]:
                continue
            recs.append(rec)
        recs.sort(key=lambda r: float(r.get("ts") or 0))

    owner = (v.get("user") or {}).get("name") or ""
    parts = ['<div class="card"><h1>%s</h1><div class="sub">%s · %s · %d 輪 · 到期 %s'
             '%s</div></div>' % (
                 _esc("對話回看" if not want_think else "對話 + 思考/重試回看"),
                 _esc(conv[:80] or "（無對話 id）"),
                 _esc(owner),
                 len(recs),
                 _fmt(v.get("exp")),
                 "" if not v.get("note") else " · " + _esc(v.get("note")))]

    if not recs:
        parts.append('<div class="card"><div class="empty">這個對話尚無可顯示的紀錄。'
                     '（紀錄只從建立連結之後的回合開始累積）</div></div>')

    for r in recs:
        parts.append(_render_turn(r, want_think))

    parts.append('<footer>本頁由你自己的代理產生，內容不經第三方 · '
                 '%s</footer>' % _esc(_fmt(time.time())))
    _send_html(h, 200, _page("對話回看", "".join(parts)))
    return True


def _render_turn(r: dict, want_think: bool) -> str:
    model = r.get("model") or ""
    retries = int(r.get("retries") or 0)
    verdict = r.get("verdict") or ""
    err = r.get("error") or ""
    tags = []
    if model:
        tags.append('<span class="tag">%s</span>' % _esc(model))
    if retries:
        tags.append('<span class="tag warn">重試 %d</span>' % retries)
    if verdict in ("retry_ok",):
        tags.append('<span class="tag ok">重試成功</span>')
    elif verdict == "passed":
        tags.append('<span class="tag bad">錯誤放行</span>')
    if err:
        tags.append('<span class="tag bad">%s</span>' % _esc(err.split(":")[0][:40]))
    tags.append('<span class="tag">%s 字</span>' % int(r.get("final_chars") or 0))
    tags.append('<span class="tag">%ss</span>' % (r.get("elapsed") or 0))

    out = ['<div class="card"><div class="turnhd">%s<span style="margin-left:auto">%s'
           '</span></div>' % ("".join(tags), _fmt(r.get("ts")))]

    user = (r.get("user") or "").strip()
    if user:
        out.append('<div class="user">%s</div>' % _esc(user[:4000]))

    think = (r.get("think") or "")
    if want_think and think.strip():
        out.append('<details class="think"><summary>思考過程 · %d 字</summary>'
                   '<pre>%s</pre></details>' % (len(think), _esc(think)))

    if want_think:
        for a in (r.get("attempts") or []):
            k = a.get("kind") or ""
            bad = k not in ("ok",)
            cls = "rej" if bad else "good"
            lab = []
            lab.append('<span class="tag %s">%s</span>' % (
                "bad" if bad else "ok", _esc(k or "?")))
            lab.append('<span class="tag">第 %s 次</span>' % _esc(a.get("n")))
            lab.append('<span class="tag">%s 字</span>' % int(a.get("chars") or 0))
            if a.get("model"):
                lab.append('<span class="tag">%s</span>' % _esc(a["model"]))
            if a.get("note"):
                lab.append('<span style="color:var(--dim)">%s</span>' % _esc(a["note"][:160]))
            txt = a.get("text") or "（空）"
            out.append('<div class="att %s"><div class="lab">%s</div>'
                       '<div class="txt">%s</div></div>' % (
                           cls, "".join(lab), _esc(txt)))

    final = (r.get("final") or "")
    if final.strip():
        out.append('<div class="answer">%s</div>' % _esc(final))
    else:
        out.append('<div class="answer" style="color:var(--dim)">（這一輪沒有正文）</div>')
    out.append("</div>")
    return "".join(out)


def _send_html(h, status: int, text: str) -> None:
    payload = text.encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(payload)))
    h.send_header("Content-Security-Policy", _CSP)
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    try:
        h.wfile.write(payload)
    except Exception:
        pass
