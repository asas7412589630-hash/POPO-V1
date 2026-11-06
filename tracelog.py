#!/usr/bin/env python3
"""Per-client / per-conversation logging.

The problem with one log stream: a turn from the Android client, a turn from a
scheduled task, and a test curl all interleave, so "why did model X refuse on
Tuesday" cannot be answered — the surrounding lines belong to a different
conversation. Worse, the retry loop's own lines land between the request line and
the response line, so the outcome appears attached to whichever request happened to
be adjacent.

Two axes, because they answer different questions:

  client      who is asking. Derived from the Authorization token (hashed — the raw
              key never appears in a log) or the forwarded IP when no key is used.
              Answers "is this client's config broken" and "is this client's key
              being rate-limited".

  conversation  which thread of work. The OpenAI API has no conversation id, so one
              is derived: the client's own `user` field when present (the API
              defines it for exactly this), otherwise a hash of the first user turn
              plus the system prompt. Hashing the first turn means a resumed thread
              keeps its id, while two threads that merely start alike stay distinct
              as soon as they diverge.

Log layout:

  logs/<client>/<conversation>.log      full per-conversation trail
  logs/<client>/_index.log              one line per request for that client
  logs/_all.log                         the combined stream (what exists today)

Every line in the per-conversation file carries the same prefix, so a file can be
read start to finish without guessing which request it belongs to.
"""
import hashlib
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_ROOT = os.environ.get("ARMOR_SPLIT_LOG_DIR", os.path.join(BASE, "logs"))

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Log rotation + handle cache
#
# Before this, every log line opened three files (conversation, client index,
# combined), appended, and closed, with no size limit anywhere. Two problems:
#
#   * cost — a turn emits roughly ten log lines, so a single request paid thirty
#     open/close syscalls. Under the concurrency test that was pure overhead on
#     top of the real work.
#   * growth — `_all.log` and `_ip-local/...log` were already at 1 MB and 1.8 MB
#     with no ceiling. Nothing was going to stop them.
#
# Handles are now cached and rotated by size. append is still called under
# `_lock`, so ordering within the process is unchanged; flush per line is kept
# so a crash cannot lose the tail that explains it. Rotation renames, which is
# safe on Linux even with the old fd open, but the cached handle is closed first
# and reopened against the fresh file so new lines never land in the .1 file.
# ---------------------------------------------------------------------------
MAX_BYTES = int(os.environ.get("ARMOR_SPLIT_LOG_MAX", "8000000"))
KEEP = int(os.environ.get("ARMOR_SPLIT_LOG_KEEP", "3"))
_FH_MAX = 64                 # cached handles; evicted oldest first

_fh = {}                     # path -> open file object
_fh_size = {}                # path -> approximate bytes in that file


def _rotate(path):
    fh = _fh.pop(path, None)
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    for i in range(KEEP - 1, 0, -1):
        try:
            os.replace("%s.%d" % (path, i), "%s.%d" % (path, i + 1))
        except OSError:
            pass
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass
    _fh_size[path] = 0


def _append(path, text):
    """Append one line, rotating when the file outgrows MAX_BYTES. Never raises."""
    fh = _fh.get(path)
    if fh is None:
        try:
            fh = open(path, "a", encoding="utf-8")
        except OSError:
            return
        if len(_fh) >= _FH_MAX:
            old = next(iter(_fh))
            if old != path:
                try:
                    _fh.pop(old).close()
                except Exception:
                    _fh.pop(old, None)
                _fh_size.pop(old, None)
        _fh[path] = fh
        try:
            _fh_size[path] = os.path.getsize(path)
        except OSError:
            _fh_size[path] = 0

    if _fh_size.get(path, 0) + len(text) > MAX_BYTES:
        _rotate(path)
        try:
            fh = _fh[path] = open(path, "a", encoding="utf-8")
        except OSError:
            return

    try:
        fh.write(text)
        fh.flush()
    except (OSError, ValueError):
        # Someone closed it out from under us (rotation in another path or a
        # startup of a second process). Drop the cache and try once plainly.
        _fh.pop(path, None)
        _fh_size.pop(path, None)
        try:
            with open(path, "a", encoding="utf-8") as f2:
                f2.write(text)
        except OSError:
            return
    _fh_size[path] = _fh_size.get(path, 0) + len(text)


def close_all():
    """Release cached handles (called on graceful shutdown)."""
    with _lock:
        for fh in _fh.values():
            try:
                fh.close()
            except Exception:
                pass
        _fh.clear()
        _fh_size.clear()

# A conversation id kept in the request scope of the current turn. Set by
# begin(), read by event(); the proxy passes it explicitly where it can.
_ctx = threading.local()

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _short(digest, n=10):
    return hashlib.sha256(digest.encode("utf-8", "replace")).hexdigest()[:n]


def client_id(headers, client_keys=()):
    """Stable, non-reversible client label.

    The token is hashed rather than logged: an access token in a log file is a
    credential leak, and the proxy's logs are readable from the host. A client that
    sends no key is labelled by forwarded IP, with `anon-` marking that it is not
    authenticated.
    """
    auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            known = tok in set(client_keys or ())
            return ("key-" if known else "tok-") + _short(tok)
    for h in ("CF-Connecting-IP", "cf-connecting-ip", "X-Real-IP", "x-real-ip",
              "X-Forwarded-For", "x-forwarded-for"):
        v = (headers.get(h) or "").strip()
        if v:
            return "ip-" + _short(v.split(",")[0].strip())
    return "ip-local"


def conversation_id(body):
    """Which thread of work this turn belongs to.

    Order, most reliable first:
      1. the client's `user` field — the API defines it as the end-user identifier,
         so a client that sets it gets exact grouping for free;
      2. a system-prompt hash + first-user-turn hash. Resumed threads keep the same
         first turn, so they keep the same id;
      3. a timestamp bucket, for a request with no user turn at all.
    """
    if not isinstance(body, dict):
        return "conv-unknown"
    u = body.get("user")
    if isinstance(u, str) and u.strip():
        return "u-" + _short(u.strip())

    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        sys_txt, first_user = "", ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            c = m.get("content")
            txt = c if isinstance(c, str) else (
                " ".join(p.get("text") or "" for p in c
                         if isinstance(p, dict)) if isinstance(c, list) else "")
            if role == "system" and not sys_txt:
                sys_txt = txt[:400]
            elif role == "user" and not first_user:
                first_user = txt[:400]
                break
        if first_user:
            return "c-" + _short(sys_txt + "\x00" + first_user)
    return "t-" + time.strftime("%H%M")


def begin(client, conv):
    """Bind the current turn's labels to this thread."""
    _ctx.client = client
    _ctx.conv = conv
    return client, conv


def current():
    return getattr(_ctx, "client", "unknown"), getattr(_ctx, "conv", "unknown")


_DIR_READY = set()
_DIR_LOCK = threading.Lock()

def _path(client, name):
    d = os.path.join(LOG_ROOT, client)
    # makedirs() measured 0.78 ms per call on this host (PRoot), and event()
    # calls _path() three times per log line — with ten-odd log lines per turn
    # that is ~25 ms of pure mkdir cost on every request, all of it for
    # directories that already exist. The set is bounded by the number of
    # clients, and a removed directory simply gets recreated on the next event.
    if d not in _DIR_READY:
        with _DIR_LOCK:
            if d not in _DIR_READY:
                os.makedirs(d, exist_ok=True)
                _DIR_READY.add(d)
    return d, os.path.join(d, name)


def event(msg, level="", client=None, conv=None):
    """Append one line to the client file, the client index and the combined log.

    Also writes to stderr so `hark-service logs` keeps working as it does today —
    this adds a split view, it does not move the operator's existing one.
    """
    c, v = current()
    client = client or c
    conv = conv or v
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = ("[%s] " % level) if level else ""
    line = "%s %-10s %-14s %s%s\n" % (stamp, client, conv, tag, msg)
    try:
        with _lock:
            d, p = _path(client, conv + ".log")
            _append(p, line)
            _, idx = _path(client, "_index.log")
            _append(idx, line)
            _append(os.path.join(LOG_ROOT, "_all.log"), line)
    except OSError:
        pass
    return line


def summary():
    """One screenful: clients, their conversation count, last activity."""
    out = []
    try:
        clients = sorted(os.listdir(LOG_ROOT))
    except OSError:
        return "no logs yet"
    for c in clients:
        if c.startswith("_"):
            continue
        d = os.path.join(LOG_ROOT, c)
        if not os.path.isdir(d):
            continue
        convs = [f for f in os.listdir(d) if f.endswith(".log") and f != "_index.log"]
        rows = []
        total = 0
        for f in convs:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
                n = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
            except OSError:
                continue
            total += n
            rows.append((st.st_mtime, f[:-4], n))
        rows.sort(reverse=True)
        out.append("%-16s %3d conversations %6d lines" % (c, len(rows), total))
        for mtime, name, n in rows[:5]:
            out.append("    %-16s %5d lines  last %s" % (
                name, n, time.strftime("%m-%d %H:%M", time.localtime(mtime))))
    return "\n".join(out) or "no logs yet"
