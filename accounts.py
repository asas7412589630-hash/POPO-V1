#!/usr/bin/env python3
"""accounts — 帳戶層：身分、金鑰歸屬、可分享的回看連結、用量計量。

這不是「另一個 keyring」。分工刻意分開：

  keyring.py    證明「這個 token 是有效的」           (proxy 呼叫，每請求)
  accounts.py   說明「這個 token 是誰的、能不能給連結、用掉多少」 (console/分享用)

一個帳戶的 API 金鑰就是一把 keyring 金鑰 —— 建立帳戶金鑰時呼叫
KEYRING.create()，所以代理的驗證路徑（_client_key_ok / 鏡像檔 / 停用記錄優先）
一個字都不用改。這裡只記錄 kid -> uid 的歸屬。

三個刻意的設計決定：

 1. **金鑰留本地，不上傳**。所有狀態都在 proxy/*.json，沒有任何 outbound
    同步。分享連結的內容也由本機 trace 檔渲染，所以「給別人看對話」不會把
    對話送到第三方。

 2. **回看連結是「伺服器端持有的 view 記錄」，不是簽章 URL**。
    簽章 URL 無法撤銷；記錄可以。`delete` 一筆 view，連結立刻失效，
    且知道長度、知道過期時間、可以數被點了幾次。

 3. **密碼只存 PBKDF2-HMAC-SHA256(salt, iterations)**。明文密碼除了登入那一
    瞬間之外不存在於任何地方；連 owner 建立帳戶時看到的也是一次性回傳值。

檔案：proxy/accounts.json（0600），原子寫入（tmp + replace）。
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ACCT_JSON = os.environ.get("ARMOR_ACCOUNTS_JSON",
                           os.path.join(BASE, "proxy", "accounts.json"))

# 90k 次在手機上是 ~60ms（實測），足以讓離線爆破不划算，又不至於讓登入卡住。
# 可用 ARMOR_PBKDF2_ITERS 調；舊記錄保存自己的 iterations，改這個不會讓人登不進去。
PBKDF2_ITERS = int(os.environ.get("ARMOR_PBKDF2_ITERS", "90000"))
SESSION_TTL = int(os.environ.get("ARMOR_SESSION_TTL", str(14 * 86400)))
MAX_FAIL = int(os.environ.get("ARMOR_LOGIN_MAX_FAIL", "8"))
FAIL_WINDOW = int(os.environ.get("ARMOR_LOGIN_FAIL_WINDOW", "600"))

_lock = threading.RLock()
_cache = {"stamp": None, "data": None}
_fails = {}


# ---------------------------------------------------------------------------
# 儲存
# ---------------------------------------------------------------------------
def _blank() -> dict:
    return {"version": 1, "users": {}, "sessions": {}, "tokens": {},
            "views": {}, "usage": {}, "settings": {}}


def _load() -> dict:
    try:
        with open(ACCT_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return _blank()
    except Exception:
        # 壞檔不能鎖死所有人，也不能靜默放行：回空 + 標記，呼叫端看得到。
        d = _blank()
        d["_corrupt"] = True
        return d
    if not isinstance(d, dict):
        return _blank()
    for k in ("users", "sessions", "tokens", "views", "usage", "settings"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(ACCT_JSON) or ".", exist_ok=True)
    tmp = ACCT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCT_JSON)
    with _lock:
        try:
            st = os.stat(ACCT_JSON)
            _cache["stamp"] = (st.st_mtime, st.st_size)
        except OSError:
            _cache["stamp"] = None
        _cache["data"] = d


def _data() -> dict:
    """mtime+size 快取，和 keyring 同一個模式：改檔立刻生效，每請求只 stat 一次。"""
    try:
        st = os.stat(ACCT_JSON)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    with _lock:
        if stamp is not None and _cache["stamp"] == stamp and _cache["data"] is not None:
            return _cache["data"]
    d = _load()
    with _lock:
        _cache["stamp"] = stamp
        _cache["data"] = d
    return d


def _mut(fn):
    """讀-改-寫。所有寫入都走這裡，避免兩個請求各寫一半。"""
    with _lock:
        d = _load()
        out = fn(d)
        _save(d)
        return out


# ---------------------------------------------------------------------------
# 密碼
# ---------------------------------------------------------------------------
def _hash_pw(password: str, salt_hex: str, iters: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               bytes.fromhex(salt_hex), int(iters)).hex()


def _new_uid() -> str:
    return "u_" + secrets.token_hex(6)


def _public_user(u: dict) -> dict:
    """絕不外流的欄位：salt / hash / iterations。"""
    return {
        "id": u.get("id", ""),
        "name": u.get("name", ""),
        "role": u.get("role", "user"),
        "enabled": bool(u.get("enabled", True)),
        "created": u.get("created", 0),
        "last_login": u.get("last_login", 0),
        "pw_changed": u.get("pw_changed", 0),
        "limits": u.get("limits") or {},
    }


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
SETTING_DEFAULTS = {
    # 金鑰必須屬於某個帳戶才放行。預設關閉的理由：舊的手貼金鑰會突然全滅，
    # 而那是操作者自己的裝置。開啟後所有金鑰都得從帳戶產生。
    "require_account": False,
    # 新帳戶的金鑰數上限（0 = 不限）。
    "max_keys_per_user": 0,
}


def get_setting(key: str, default=None):
    d = _data()
    s = d.get("settings") or {}
    if key in s:
        return s[key]
    if default is not None:
        return default
    return SETTING_DEFAULTS.get(key)


def set_setting(key: str, value) -> bool:
    if key not in SETTING_DEFAULTS:
        return False
    _mut(lambda d: d.setdefault("settings", {}).__setitem__(key, value))
    return True


def all_settings() -> dict:
    d = _data()
    out = dict(SETTING_DEFAULTS)
    out.update(d.get("settings") or {})
    return out


def count_users() -> int:
    return len(_data().get("users") or {})


def ensure_owner(name: str = "owner", password: str = "") -> dict:
    """第一次啟動：建 owner，回傳含一次性 tok。已有人就回 None。

    存在的理由：沒有 owner 就沒有人能建立帳戶，而「先讓任何人建立帳戶」等於
    公開註冊。所以只有 store 全空時才允許無憑證建立第一個（bootstrap），
    之後一律走 session + admin 身分。
    """
    def fn(d):
        if d.get("users"):
            return None
        uid = _new_uid()
        pw = password or secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        u = {
            "id": uid, "name": (name or "owner")[:40], "role": "owner",
            "salt": salt, "hash": _hash_pw(pw, salt, PBKDF2_ITERS),
            "iterations": PBKDF2_ITERS, "created": time.time(),
            "last_login": 0, "enabled": True, "pw_changed": time.time(),
            "limits": {},
        }
        d["users"][uid] = u
        d.setdefault("_bootstrap", {})["at"] = time.time()
        out = _public_user(u)
        out["password"] = pw
        return out
    return _mut(fn)


def create_user(name: str, password: str = "", role: str = "user",
                limits: dict = None) -> dict:
    pw = password or secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    uid = _new_uid()
    u = {
        "id": uid, "name": (name or uid)[:40],
        "role": ("owner" if role == "owner" else "user"),
        "salt": salt, "hash": _hash_pw(pw, salt, PBKDF2_ITERS),
        "iterations": PBKDF2_ITERS, "created": time.time(),
        "last_login": 0, "enabled": True, "pw_changed": time.time(),
        "limits": limits or {},
    }
    _mut(lambda d: d["users"].__setitem__(uid, u))
    out = _public_user(u)
    out["password"] = pw
    return out


def set_password(uid: str, password: str) -> bool:
    def fn(d):
        u = d["users"].get(uid)
        if not u:
            return False
        salt = secrets.token_hex(16)
        u["salt"] = salt
        u["hash"] = _hash_pw(password, salt, PBKDF2_ITERS)
        u["iterations"] = PBKDF2_ITERS
        u["pw_changed"] = time.time()
        # 換密碼 = 撤銷所有工作階段（本機被撿走的手機不該繼續有 session）。
        for sid, s in list(d["sessions"].items()):
            if s.get("uid") == uid:
                d["sessions"].pop(sid, None)
        return True
    return _mut(fn)


def set_enabled(uid: str, enabled: bool) -> bool:
    def fn(d):
        u = d["users"].get(uid)
        if not u:
            return False
        u["enabled"] = bool(enabled)
        if not enabled:
            for sid, s in list(d["sessions"].items()):
                if s.get("uid") == uid:
                    d["sessions"].pop(sid, None)
            for kid, t in list(d["tokens"].items()):
                if t.get("uid") == uid:
                    t["enabled"] = False
                    _keyring_set_enabled(kid, False)
        return True
    return _mut(fn)


def delete_user(uid: str) -> bool:
    def fn(d):
        u = d["users"].pop(uid, None)
        if not u:
            return False
        for sid, s in list(d["sessions"].items()):
            if s.get("uid") == uid:
                d["sessions"].pop(sid, None)
        for kid, t in list(d["tokens"].items()):
            if t.get("uid") == uid:
                d["tokens"].pop(kid, None)
                _keyring_delete(kid)
        for vid, v in list(d["views"].items()):
            if v.get("uid") == uid:
                d["views"].pop(vid, None)
        d["usage"].pop(uid, None)
        return True
    return _mut(fn)


def _fail_key(ip: str) -> str:
    return (ip or "-")[:64]


def _fail_blocked(ip: str) -> float:
    now = time.time()
    with _lock:
        rec = _fails.get(_fail_key(ip))
        if not rec:
            return 0
        rec[:] = [t for t in rec if now - t < FAIL_WINDOW]
        if len(rec) < MAX_FAIL:
            return 0
        return max(0.0, FAIL_WINDOW - (now - rec[0]))


def _fail_note(ip: str) -> None:
    now = time.time()
    with _lock:
        rec = _fails.setdefault(_fail_key(ip), [])
        rec.append(now)
        if len(rec) > MAX_FAIL * 2:
            del rec[:-MAX_FAIL]


def find_by_name(name: str):
    n = (name or "").strip().lower()
    for u in (_data().get("users") or {}).values():
        if u.get("id", "").lower() == n or (u.get("name", "").lower() == n):
            return u
    return None


def login(name: str, password: str, ip: str = "", ua: str = "") -> dict:
    """回 {ok, sid, uid, user} 或 {ok:False, error, retry_after}。

    「使用者不存在」和「密碼錯」回同一個錯誤，且不存在的帳號也做一次 PBKDF2，
    否則回應時間本身就是一個帳號列舉介面。
    """
    left = _fail_blocked(ip)
    if left > 0:
        return {"ok": False, "error": "too many attempts",
                "retry_after": int(left) + 1}
    u = find_by_name(name)
    iters = int((u or {}).get("iterations") or PBKDF2_ITERS)
    salt = (u or {}).get("salt") or "00" * 16
    got = _hash_pw(password, salt, iters)
    ok = bool(u) and bool(u.get("enabled", True)) and hmac.compare_digest(
        got, (u or {}).get("hash") or "")
    if not ok:
        _fail_note(ip)
        return {"ok": False, "error": "帳號或密碼錯誤"}
    with _lock:
        _fails.pop(_fail_key(ip), None)
    return new_session(u["id"], ip=ip, ua=ua)


# ---------------------------------------------------------------------------
# 工作階段
# ---------------------------------------------------------------------------
def _sid_hash(sid: str) -> str:
    return hashlib.sha256((sid or "").encode("utf-8")).hexdigest()


def new_session(uid: str, ip: str = "", ua: str = "", ttl: int = 0) -> dict:
    sid = secrets.token_urlsafe(32)
    ttl = int(ttl or SESSION_TTL)
    now = time.time()

    def fn(d):
        d["sessions"][_sid_hash(sid)] = {
            "uid": uid, "created": now, "last": now,
            "exp": now + ttl, "ip": (ip or "")[:64],
            "ua": (ua or "")[:180],
        }
        u = d["users"].get(uid)
        if u:
            u["last_login"] = now
        return _public_user(u) if u else {}
    user = _mut(fn)
    return {"ok": True, "sid": sid, "uid": uid, "ttl": ttl, "user": user}


def resolve(sid: str) -> dict:
    """回 session 記錄（含 uid），過期/撤銷/帳戶停用一律回 {}。"""
    if not sid:
        return {}
    h = _sid_hash(sid)
    d = _data()
    s = (d.get("sessions") or {}).get(h)
    if not s:
        return {}
    now = time.time()
    if s.get("exp") and now > float(s["exp"]):
        return {}
    u = (d.get("users") or {}).get(s.get("uid"))
    if not u or not u.get("enabled", True):
        return {}
    # last 只要偶爾更新：每請求寫檔會把儲存層變成熱點，而 last 只用於顯示。
    if now - float(s.get("last") or 0) > 300:
        def fn(dd):
            e = dd["sessions"].get(h)
            if e:
                e["last"] = now
        try:
            _mut(fn)
        except Exception:
            pass
    out = dict(s)
    out["user"] = u
    return out


def revoke(sid: str) -> bool:
    h = _sid_hash(sid)
    return bool(_mut(lambda d: bool(d["sessions"].pop(h, None))))


def revoke_all(uid: str) -> int:
    def fn(d):
        n = 0
        for h, s in list(d["sessions"].items()):
            if s.get("uid") == uid:
                d["sessions"].pop(h, None)
                n += 1
        return n
    return _mut(fn)


def list_sessions(uid: str) -> list:
    now = time.time()
    rows = []
    for h, s in ((_data().get("sessions") or {}).items()):
        if s.get("uid") != uid:
            continue
        if s.get("exp") and now > float(s["exp"]):
            continue
        rows.append({"sid8": h[:8], "created": s.get("created", 0),
                     "last": s.get("last", 0), "exp": s.get("exp", 0),
                     "ip": s.get("ip", ""), "ua": s.get("ua", "")})
    rows.sort(key=lambda r: -float(r.get("last") or 0))
    return rows


# ---------------------------------------------------------------------------
# 金鑰（歸屬層；真憑證在 keyring）
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring as K
        return K
    except Exception:
        return None


def _keyring_set_enabled(kid: str, enabled: bool) -> None:
    K = _keyring()
    if K is not None:
        try:
            K.set_enabled(kid, enabled)
        except Exception:
            pass


def _keyring_delete(kid: str) -> None:
    K = _keyring()
    if K is not None:
        try:
            K.delete(kid)
        except Exception:
            pass


def mint_token(uid: str, label: str = "") -> dict:
    """替帳戶建一把代理金鑰。真的金鑰由 keyring 產生，這裡只記歸屬。"""
    K = _keyring()
    if K is None:
        return {"ok": False, "error": "keyring 模組不可用，無法建立金鑰"}
    u = (_data().get("users") or {}).get(uid)
    if not u:
        return {"ok": False, "error": "帳戶不存在"}
    cap = int(get_setting("max_keys_per_user") or 0)
    if cap > 0 and len(list_tokens(uid)) >= cap:
        return {"ok": False, "error": "金鑰數已達上限（%d）" % cap}
    lbl = (label or "").strip()[:40] or (u.get("name") or "user")
    rec = K.create(label="%s · %s" % (u.get("name") or uid, lbl))
    kid = rec.get("id") or ""
    entry = {"id": kid, "uid": uid, "label": lbl,
             "prefix": rec.get("prefix", ""), "enabled": True,
             "created": time.time(), "last_used": 0, "hits": 0}
    _mut(lambda d: d["tokens"].__setitem__(kid, entry))
    out = dict(entry)
    out["ok"] = True
    out["token"] = rec.get("token")  # 只在此刻存在，之後永遠拿不回來
    return out


def revoke_token(uid: str, kid: str, hard: bool = False) -> bool:
    def fn(d):
        t = d["tokens"].get(kid)
        if not t or (uid and t.get("uid") != uid):
            return False
        if hard:
            d["tokens"].pop(kid, None)
            _keyring_delete(kid)
        else:
            t["enabled"] = False
            _keyring_set_enabled(kid, False)
        return True
    return _mut(fn)


def list_tokens(uid: str = "") -> list:
    """帳戶自己的金鑰清單。刻意不含 token 本身，只給 prefix。"""
    K = _keyring()
    live = {}
    if K is not None:
        try:
            for e in K.list_keys():
                live[e.get("id")] = e
        except Exception:
            live = {}
    rows = []
    for kid, t in ((_data().get("tokens") or {}).items()):
        if uid and t.get("uid") != uid:
            continue
        k = live.get(kid) or {}
        rows.append({
            "id": kid, "uid": t.get("uid", ""), "label": t.get("label", ""),
            "prefix": k.get("prefix") or t.get("prefix", ""),
            "enabled": bool(k.get("enabled", t.get("enabled", True))),
            "created": t.get("created", 0),
            "last_used": k.get("last_used") or t.get("last_used", 0),
            "hits": k.get("hits") or t.get("hits", 0),
        })
    rows.sort(key=lambda r: -float(r.get("created") or 0))
    return rows


def uid_for_token(raw: str) -> str:
    """代理已驗過金鑰；這裡只回答「是誰的」。查不到回 ''（例如手動貼進
    client_keys.txt 的舊金鑰）。"""
    K = _keyring()
    if K is None or not raw:
        return ""
    try:
        rec = K.verify(raw)
    except Exception:
        rec = None
    if not rec:
        return ""
    t = (_data().get("tokens") or {}).get(rec.get("id") or "")
    return (t or {}).get("uid", "")


# ---------------------------------------------------------------------------
# 回看連結（伺服器端記錄，可撤銷）
# ---------------------------------------------------------------------------
VIEW_TTL = int(os.environ.get("ARMOR_VIEW_TTL", str(7 * 86400)))
VIEW_KINDS = ("chat", "think")   # chat = 最終對話；think = 含被丟棄的重試與思考


def new_view(uid: str, kind: str, conv: str, ttl: int = 0,
             note: str = "") -> dict:
    kind = kind if kind in VIEW_KINDS else "chat"
    ttl = int(ttl or VIEW_TTL)
    if ttl < 60:
        ttl = 60
    vid = "v" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")
    now = time.time()
    rec = {"vid": vid, "uid": uid, "kind": kind, "conv": (conv or "")[:200],
           "created": now, "exp": now + ttl, "hits": 0,
           "last": 0, "note": (note or "")[:80], "enabled": True}
    _mut(lambda d: d["views"].__setitem__(vid, rec))
    out = dict(rec)
    out["ok"] = True
    return out


def resolve_view(vid: str) -> dict:
    vid = (vid or "").strip()
    if not vid.startswith("v") or len(vid) > 40:
        return {}
    d = _data()
    v = (d.get("views") or {}).get(vid)
    if not v or not v.get("enabled", True):
        return {}
    if v.get("exp") and time.time() > float(v["exp"]):
        return {}
    u = (d.get("users") or {}).get(v.get("uid"))
    if not u or not u.get("enabled", True):
        return {}
    def fn(dd):
        e = dd["views"].get(vid)
        if e:
            e["hits"] = int(e.get("hits") or 0) + 1
            e["last"] = time.time()
    try:
        _mut(fn)
    except Exception:
        pass
    # Read the counter back instead of reusing the pre-increment copy: the page
    # shows "N 次點閱" and the operator checks it right after opening the link,
    # so a stale count reads as "nobody opened it".
    cur = (_data().get("views") or {}).get(vid) or v
    out = dict(cur)
    out["user"] = _public_user(u)
    return out


def revoke_view(uid: str, vid: str) -> bool:
    def fn(d):
        v = d["views"].get(vid)
        if not v or (uid and v.get("uid") != uid):
            return False
        d["views"].pop(vid, None)
        return True
    return _mut(fn)


def list_views(uid: str = "") -> list:
    rows = []
    for v in ((_data().get("views") or {}).values()):
        if uid and v.get("uid") != uid:
            continue
        rows.append(dict(v))
    rows.sort(key=lambda r: -float(r.get("created") or 0))
    return rows


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------
_usage_pending = {}
_usage_flush_at = [0.0]
USAGE_FLUSH_S = float(os.environ.get("ARMOR_USAGE_FLUSH_S", "5"))


def _usage_merge(d: dict, uid: str, r: dict) -> None:
    u = d["usage"].setdefault(uid, {})
    day = time.strftime("%Y-%m-%d")
    slot = u.setdefault(day, {"turns": 0, "bytes_in": 0, "chars_out": 0,
                              "tokens_in": 0, "tokens_out": 0})
    for k, v in r.items():
        slot[k] = int(slot.get(k) or 0) + int(v or 0)
    if len(u) > 130:
        for k in sorted(u)[:-120]:
            u.pop(k, None)


_flusher_started = [False]


def start_flusher() -> None:
    """背景每 5 秒把待寫的計量落盤。

    沒有這個執行緒時，最後一輪的用量要等到「下一次 bump 且已過節流窗口」才會
    落盤 —— 也就是說，一輪對話結束後就把 app 收起來，那一輪的數字永遠不會出現。
    計量表少算最後一筆，比計量表慢 5 秒差得多。
    """
    if _flusher_started[0]:
        return
    _flusher_started[0] = True

    def loop():
        while True:
            time.sleep(max(1.0, USAGE_FLUSH_S))
            try:
                flush_usage(force=True)
            except Exception:
                pass

    t = threading.Thread(target=loop, name="usage-flusher", daemon=True)
    t.start()


def flush_usage(force: bool = False) -> int:
    """把累積的計量寫一次檔。呼叫端不必持鎖。"""
    now = time.time()
    with _lock:
        if not force and now < _usage_flush_at[0]:
            return 0
        if not _usage_pending:
            _usage_flush_at[0] = now + USAGE_FLUSH_S
            return 0
        batch = dict(_usage_pending)
        _usage_pending.clear()
        _usage_flush_at[0] = now + USAGE_FLUSH_S
    try:
        with _lock:
            d = _load()
            for uid, r in batch.items():
                _usage_merge(d, uid, r)
            _save(d)
    except Exception:
        with _lock:
            for uid, r in batch.items():
                cur = _usage_pending.setdefault(uid, {})
                for k, v in r.items():
                    cur[k] = cur.get(k, 0) + v
        return 0
    return len(batch)


def bump_usage(uid: str, turns: int = 0, bytes_in: int = 0,
               chars_out: int = 0, tokens_in: int = 0,
               tokens_out: int = 0) -> None:
    """每日 rollup，先記記憶體再一次落盤。

    每一輪都 fsync 一次帳本會把儲存層變成熱點（這裡的 fsync 是本機 0600 檔，
    一次 ~2ms，而一輪對話裡 usage 碎片可以來好幾個）。所以累積 + 5 秒節流；
    `usage()` 讀檔前會先 flush，看到的永遠是最新數字。
    """
    if not uid:
        return
    with _lock:
        cur = _usage_pending.setdefault(uid, {})
        cur["turns"] = cur.get("turns", 0) + int(turns)
        cur["bytes_in"] = cur.get("bytes_in", 0) + int(bytes_in)
        cur["chars_out"] = cur.get("chars_out", 0) + int(chars_out)
        cur["tokens_in"] = cur.get("tokens_in", 0) + int(tokens_in)
        cur["tokens_out"] = cur.get("tokens_out", 0) + int(tokens_out)
        due = time.time() >= _usage_flush_at[0] or sum(
            v.get("turns", 0) for v in _usage_pending.values()) >= 25
    if due:
        flush_usage(force=True)


def usage(uid: str, days: int = 14) -> list:
    flush_usage(force=True)
    u = (_data().get("usage") or {}).get(uid) or {}
    rows = []
    for day in sorted(u)[-max(1, int(days)):]:
        r = dict(u[day] or {})
        r["day"] = day
        rows.append(r)
    return rows


def usage_today(uid: str) -> dict:
    flush_usage(force=True)
    u = (_data().get("usage") or {}).get(uid) or {}
    return dict(u.get(time.strftime("%Y-%m-%d")) or {})


def attach_token(uid: str, kid: str, label: str = "", prefix: str = "") -> bool:
    """把一把「已經存在」的 keyring 金鑰掛到帳戶名下。

    /keys（控制台的金鑰頁）走的是舊路徑：它直接呼叫 KEYRING.create()，不經過
    帳戶層。沒有這個函式，那些金鑰就是「沒有主人」的 —— 而 require_account
    一旦開啟，它們就會突然失效，使用者卻剛在畫面上看到「建立成功」。
    兩條建立路徑都掛上歸屬，金鑰的可見性才一致。
    """
    if not uid or not kid:
        return False
    u = (_data().get("users") or {}).get(uid)
    if not u:
        return False
    entry = {"id": kid, "uid": uid,
             "label": (label or "").strip()[:40] or (u.get("name") or ""),
             "prefix": prefix, "enabled": True,
             "created": time.time(), "last_used": 0, "hits": 0}
    _mut(lambda d: d["tokens"].__setitem__(kid, entry))
    return True


def list_users() -> list:
    rows = [_public_user(u) for u in ((_data().get("users") or {}).values())]
    rows.sort(key=lambda r: (r.get("role") != "owner", r.get("created") or 0))
    return rows


def get_user(uid: str) -> dict:
    u = (_data().get("users") or {}).get(uid)
    return _public_user(u) if u else {}


def stats() -> dict:
    d = _data()
    return {"users": len(d.get("users") or {}),
            "sessions": len(d.get("sessions") or {}),
            "tokens": len(d.get("tokens") or {}),
            "views": len(d.get("views") or {}),
            "corrupt": bool(d.get("_corrupt"))}
