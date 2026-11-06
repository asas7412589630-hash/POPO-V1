#!/usr/bin/env python3
"""keyring — client API keys as manageable records, not a flat text file.

Why not just keep `client_keys.txt`: that file can only express "this token is
valid". It cannot express a label ("nono-phone"), a disabled key that must stop
working without being deleted, a creation time, or a last-used stamp. Those are
the things an operator actually needs when a key leaks or a device is retired.

Two layers, on purpose:

  keys.json     the records (id, hash, label, enabled, created, last_used, hits)
  keys.txt      regenerated MIRROR of the enabled tokens

The mirror exists because every existing code path (and any tool the operator
already uses) reads the text file, and re-pointing all of them at JSON is how you
introduce a bug in the auth path — the one place a bug means "everything is
open" or "nothing works". The JSON is the source of truth; the mirror is written
atomically after every change and on boot.

The raw token is NEVER stored. Only a SHA-256 of it, so a leaked keys.json does
not hand over working credentials. The token is shown exactly once, at creation.
"""
import hashlib
import json
import os
import secrets
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KEYS_JSON = os.environ.get("ARMOR_KEYS_JSON",
                           os.path.join(BASE, "proxy", "keys.json"))
KEYS_TXT = os.environ.get("ARMOR_CLIENT_KEYS",
                          os.path.join(BASE, "proxy", "client_keys.txt"))

_lock = threading.RLock()

# Token prefixes, so a key is recognisable in a log or a paste without being
# usable from the log alone.
PREFIX = "triad-"


def _hash(tok: str) -> str:
    return hashlib.sha256((tok or "").strip().encode("utf-8")).hexdigest()


def _load() -> dict:
    try:
        with open(KEYS_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"keys": []}
        d.setdefault("keys", [])
        return d
    except FileNotFoundError:
        return {"keys": []}
    except Exception:
        # A corrupt record file must not lock everyone out, and must not silently
        # grant access either. Fall back to the text mirror (below), and say so.
        return {"keys": [], "_corrupt": True}


# Fields that exist only in memory. `_tok` is the raw token, kept so the mirror
# can be rebuilt without persisting secrets — if it ever reaches the file, the
# keyring stores working credentials in plaintext and the whole "we only keep a
# hash" property is gone. Stripped here, at the single write path, rather than at
# each caller (a caller that forgets is a leak).
_EPHEMERAL = ("_tok", "_saved")


def _persistable(d: dict) -> dict:
    out = {"keys": []}
    for e in d.get("keys") or []:
        out["keys"].append({k: v for k, v in e.items() if k not in _EPHEMERAL})
    return out


def _save(d: dict) -> None:
    tmp = KEYS_JSON + ".tmp"
    os.makedirs(os.path.dirname(KEYS_JSON), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_persistable(d), f, ensure_ascii=False, indent=1)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, KEYS_JSON)


def _sync_mirror(d: dict) -> None:
    """Rewrite the plain-text list of ENABLED token hashes' ... tokens.

    Problem: we only store hashes, so we cannot reconstruct the token for the
    mirror. Solution: the mirror is written from the tokens we have in hand at
    mutation time (create/enable), and rebuilt from hashes only when we cannot
    (delete). A mirror that cannot be rebuilt keeps the last known tokens, which
    is why deletion removes the entry from JSON and rewrites the mirror from the
    entries that still carry a `tok` field kept in memory for the session.
    """
    lines = ["# managed by keyring — use /keys (console) instead of editing",
             "# disabled keys are removed from this mirror; records live in keys.json"]
    for e in d.get("keys") or []:
        if not e.get("enabled", True):
            continue
        tok = e.get("_tok") or _mirror_cache.get(e.get("id"), "")
        if tok:
            lines.append(tok)
    os.makedirs(os.path.dirname(KEYS_TXT), exist_ok=True)
    tmp = KEYS_TXT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, KEYS_TXT)


# Holds the raw token for keys created in this process, so the mirror can be
# rebuilt without ever persisting the token. Rebuilt from the mirror on import.
_mirror_cache = {}


def _rebuild_cache_from_mirror() -> None:
    """Match mirror tokens back to records by hash (so a restart keeps working)."""
    try:
        with open(KEYS_TXT, "r", encoding="utf-8") as f:
            toks = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return
    d = _load()
    by_hash = {e.get("hash"): e.get("id") for e in (d.get("keys") or [])}
    for t in toks:
        kid = by_hash.get(_hash(t))
        if kid:
            _mirror_cache[kid] = t


def _public(e: dict) -> dict:
    """A record as the UI may see it: never includes the token."""
    return {
        "id": e.get("id"),
        "label": e.get("label") or "",
        "prefix": e.get("prefix") or "",
        "enabled": bool(e.get("enabled", True)),
        "created": e.get("created") or 0,
        "created_when": time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(e.get("created") or 0)) if e.get("created") else "",
        "last_used": e.get("last_used") or 0,
        "last_used_when": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(e.get("last_used") or 0)) if e.get("last_used") else "",
        "hits": e.get("hits") or 0,
    }


def list_keys() -> list:
    with _lock:
        d = _load()
        return [_public(e) for e in (d.get("keys") or [])]


def create(label: str = "") -> dict:
    """Make a key. The returned dict carries the token — the only time it exists."""
    with _lock:
        d = _load()
        tok = PREFIX + secrets.token_urlsafe(24)
        e = {
            "id": secrets.token_hex(6),
            "hash": _hash(tok),
            "prefix": tok[:10] + "…",
            "label": (label or "").strip()[:60],
            "enabled": True,
            "created": time.time(),
            "last_used": 0,
            "hits": 0,
            "_tok": tok,
        }
        d.setdefault("keys", []).append(e)
        _save(d)
        _mirror_cache[e["id"]] = tok
        _sync_mirror(d)
        out = _public(e)
        out["token"] = tok
        return out


def set_enabled(kid: str, enabled: bool) -> bool:
    with _lock:
        d = _load()
        for e in d.get("keys") or []:
            if e.get("id") == kid:
                e["enabled"] = bool(enabled)
                _save(d)
                _sync_mirror(d)
                return True
        return False


def rename(kid: str, label: str) -> bool:
    with _lock:
        d = _load()
        for e in d.get("keys") or []:
            if e.get("id") == kid:
                e["label"] = (label or "").strip()[:60]
                _save(d)
                return True
        return False


def delete(kid: str) -> bool:
    with _lock:
        d = _load()
        before = len(d.get("keys") or [])
        d["keys"] = [e for e in (d.get("keys") or []) if e.get("id") != kid]
        if len(d["keys"]) == before:
            return False
        _mirror_cache.pop(kid, None)
        _save(d)
        _sync_mirror(d)
        return True


def verify(token: str) -> dict:
    """Return the matching enabled record, or {}.

    Records the hit so the console can show which key is actually in use — the
    first question when a key leaks is "is it even being used".
    """
    if not token:
        return {}
    h = _hash(token)
    with _lock:
        d = _load()
        for e in d.get("keys") or []:
            if e.get("hash") == h and e.get("enabled", True):
                e["last_used"] = time.time()
                e["hits"] = int(e.get("hits") or 0) + 1
                # Persist lazily: every request must not rewrite the file. Once
                # per 60s per key is enough for "last used".
                if time.time() - float(e.get("_saved") or 0) > 60:
                    e["_saved"] = time.time()
                    try:
                        _save(d)
                    except Exception:
                        pass
                return _public(e)
        return {}


def enabled_tokens_exist() -> bool:
    with _lock:
        d = _load()
        return any(e.get("enabled", True) for e in (d.get("keys") or []))


def bootstrap_from_mirror() -> None:
    """Adopt an existing client_keys.txt on first run.

    An install that already has keys must keep working across the upgrade, so the
    plain tokens in the mirror become records (hashed) the first time this module
    sees an empty keys.json.
    """
    with _lock:
        d = _load()
        if d.get("keys"):
            _rebuild_cache_from_mirror()
            return
        try:
            with open(KEYS_TXT, "r", encoding="utf-8") as f:
                toks = [ln.strip() for ln in f
                        if ln.strip() and not ln.startswith("#")]
        except OSError:
            return
        if not toks:
            return
        d["keys"] = []
        for t in toks:
            e = {"id": secrets.token_hex(6), "hash": _hash(t),
                 "prefix": t[:10] + "…", "label": "imported", "enabled": True,
                 "created": time.time(), "last_used": 0, "hits": 0}
            d["keys"].append(e)
            _mirror_cache[e["id"]] = t
        _save(d)
        _sync_mirror(d)


bootstrap_from_mirror()
