#!/usr/bin/env python3
"""trace_rec — per-turn record of attempts and the answer that was delivered.

Why this exists: the proxy log records sizes and verdicts, never content, and the
response log records only the final body. So the two questions the operator
actually asks cannot be answered from either:

  * "it refused — what did the model say before it refused?"  (the discarded
    attempts, which are thrown away by design)
  * "which client, which conversation, how many retries?"     (the retry count is
    only in stderr lines that interleave across threads)

One record per answered turn, holding every attempt's text plus the final text,
keyed by client and conversation. Written as JSONL under runtime/trace/, one file
per day, so a day can be read back without loading everything.

Bounded on purpose: attempts are capped at ATTEMPT_CAP chars each, and the file is
rotated daily. A trace that grows without limit is a trace nobody keeps.
"""
import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.environ.get("ARMOR_TRACE_DIR", os.path.join(BASE, "runtime", "trace"))
ATTEMPT_CAP = int(os.environ.get("ARMOR_TRACE_ATTEMPT_CAP", "30000"))
# The delivered answer is kept in full. Truncating it was the difference between
# "you can read the whole answer in the log" and "the log shows 20000 chars of a
# 90000 char answer with no indication that anything was cut".
FINAL_CAP = int(os.environ.get("ARMOR_TRACE_FINAL_CAP", "0"))
# Referenced, never inlined, past this size: a 1 MB body inside a list response
# would make the log unusable. The full text stays on disk.
INLINE_CAP = int(os.environ.get("ARMOR_TRACE_INLINE_CAP", "400000"))

_lock = threading.Lock()
_tls = threading.local()


_dir_ready = [False]


def _dir():
    # makedirs() is not free: on this host (PRoot) it measured 0.78 ms per call,
    # and every reader calls _dir() once per day file — seven times for the
    # standard 7-day window. The directory does not move during the process
    # lifetime, so the check is done once. If it is removed underneath us the
    # writes fail and the next event recreates it, which is the failure mode
    # that already existed.
    if not _dir_ready[0]:
        try:
            os.makedirs(TRACE_DIR, exist_ok=True)
        except OSError:
            return TRACE_DIR
        _dir_ready[0] = True
    return TRACE_DIR


# ---------------------------------------------------------------------------
# Day-file cache
#
# Every UI read (/trace/clients, /trace/convs, /trace/list) used to re-open and
# re-parse the whole 7-day window. The files are small in bytes but each record
# carries an entire answer, so parsing them costs far more than serving the
# request: measured 2026-11-03, /trace/clients went from 27 ms at one caller to
# 2029 ms at sixteen, and the GIL contention from those parses dragged every
# other endpoint down with it.
#
# The cache is incremental rather than mtime-only: today's file grows by one
# record per turn, so an mtime key would miss on every single read and change
# nothing. Instead the parsed rows are kept and only the bytes appended since
# the last read are parsed. A file that shrank or was replaced is re-read from
# scratch, which is the only case where an offset is unsafe.
#
# Partial trailing lines are never consumed: the offset stops at the last
# newline, so a record still being written is simply picked up next time.
# ---------------------------------------------------------------------------
_FILE_CACHE = {}          # path -> {"size": int, "ino": int, "rows": [dict]}
_FILE_CACHE_LOCK = threading.Lock()
_FILE_CACHE_MAX = 64      # day files; a 60-day window still fits


def _parse_lines(blob, rows):
    for line in blob.split(b"\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line.decode("utf-8", "replace")))
        except Exception:
            continue


# stat() costs 0.83 ms here. A day file other than today's is effectively
# immutable — the only writer puts a record in the file for the turn's START
# day, so nothing but a midnight-spanning turn ever touches a past file. Knowing
# that, a past file is re-stat'ed at most once per STAT_WINDOW instead of on
# every read, which removes ~5 ms of syscall time per request on a 7-day window.
# The worst case this introduces is that such a turn becomes visible up to
# STAT_WINDOW seconds late — irrelevant for an observability view, and today's
# file (the one that actually grows) is always stat'ed.
STAT_WINDOW = float(os.environ.get("ARMOR_TRACE_STAT_WINDOW", "1.0"))
_stat_seen = {}          # path -> last time stat() was performed
_today_key = [""]


def _needs_stat(path):
    today = time.strftime("%Y-%m-%d", time.localtime())
    if _today_key[0] != today:
        _today_key[0] = today
        _stat_seen.clear()
        return True
    if path.endswith(today + ".jsonl"):
        return True
    last = _stat_seen.get(path)
    if last is not None and (time.time() - last) < STAT_WINDOW:
        return False
    return True


def _day_rows(path):
    """Parsed records for one day file, incremental. Never raises."""
    if not _needs_stat(path) and path in _FILE_CACHE:
        return _FILE_CACHE[path]["rows"]
    _stat_seen[path] = time.time()
    try:
        st = os.stat(path)
    except OSError:
        with _FILE_CACHE_LOCK:
            _FILE_CACHE.pop(path, None)
        return []
    with _FILE_CACHE_LOCK:
        ent = _FILE_CACHE.get(path)
        if (ent is not None and ent["size"] == st.st_size
                and ent["ino"] == st.st_ino):
            return ent["rows"]

    if ent is not None and ent["ino"] == st.st_ino and st.st_size > ent["size"]:
        # Append-only growth: read just the new tail.
        start = ent["size"]
        tail = b""
        try:
            with open(path, "rb") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            tail = b""
        cut = tail.rfind(b"\n")
        if cut < 0:
            return ent["rows"]        # a partial line only — nothing to add
        consumed = start + cut + 1
        rows = list(ent["rows"])
        _parse_lines(tail[:cut], rows)
        with _FILE_CACHE_LOCK:
            _FILE_CACHE[path] = {"size": consumed, "ino": st.st_ino, "rows": rows}
        return rows

    # New file, shrunk file, or replaced inode: full parse.
    rows = []
    consumed = 0
    try:
        with open(path, "rb") as f:
            blob = f.read()
        cut = blob.rfind(b"\n")
        usable = blob if cut < 0 else blob[:cut]
        _parse_lines(usable, rows)
        consumed = len(usable) + (0 if cut < 0 else 1)
    except OSError:
        return []
    with _FILE_CACHE_LOCK:
        if len(_FILE_CACHE) >= _FILE_CACHE_MAX:
            # Drop the least recently touched entry; the window is bounded, so
            # this only matters when someone asks for a very wide range.
            for old in list(_FILE_CACHE)[:max(1, _FILE_CACHE_MAX // 4)]:
                _FILE_CACHE.pop(old, None)
        _FILE_CACHE[path] = {"size": consumed, "ino": st.st_ino, "rows": rows}
    return rows


def _day_rows_many(days):
    """Concatenate several day files, newest file last (callers sort anyway)."""
    out = []
    for day in days:
        out.extend(_day_rows(os.path.join(_dir(), day + ".jsonl")))
    return out


def _day_file(ts=None):
    return os.path.join(_dir(), time.strftime("%Y-%m-%d", time.localtime(ts or time.time())) + ".jsonl")


def _cur():
    t = getattr(_tls, "turn", None)
    if t is None:
        t = {"attempts": []}
        _tls.turn = t
    return t


def begin(client="", conv="", model="", level="", msgs=0, user="", uid=""):
    """Start a turn. Safe to call more than once; the last call wins.

    Never raises: the trace is an audit aid, and an audit aid that can kill a
    request is worse than none. Every caller already wraps this, but wrapping at
    the definition removes the need to remember.
    """
    try:
        return _begin(client, conv, model, level, msgs, user, uid)
    except Exception:
        return ""


def _begin(client="", conv="", model="", level="", msgs=0, user="", uid=""):
    _tls.turn = {
        "id": "%d-%s" % (int(time.time() * 1000), os.urandom(3).hex()),
        "ts": time.time(),
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client": client or "",
        "conv": conv or "",
        "model": model or "",
        "level": level or "",
        "msgs": int(msgs or 0),
        "user": (user or "")[:600],
        "uid": uid or "",
        "attempts": [],
    }
    return _tls.turn["id"]


def set_fields(**kw):
    t = _cur()
    for k, v in kw.items():
        if v:
            t[k] = v


def summary():
    """What the client should be told about this turn: how many retries ran and
    whether a bad answer was delivered anyway.

    verdict:
      ok       first attempt was fine
      retry_ok a retry replaced a rejected attempt
      passed   a rejected attempt was delivered (budget ran out, every attempt
               refused, or retrying was not possible) — "錯誤放行"
    """
    t = getattr(_tls, "turn", None)
    if not t:
        return {"retries": 0, "verdict": ""}
    n = len(t.get("attempts") or [])
    retries = max(0, n - 1) if n else 0
    verdict = ""
    if n:
        last_kinds = [a.get("kind") for a in t["attempts"]]
        if any(k == "ok" for k in last_kinds[1:]):
            verdict = "retry_ok"
        elif retries > 0 and last_kinds[-1] not in ("ok",):
            verdict = "passed"
        else:
            verdict = "retry_ok" if retries else "ok"
    return {"retries": retries, "verdict": verdict,
            "attempt_kinds": [a.get("kind") for a in (t.get("attempts") or [])]}


def attempt(n, kind, text="", status=0, model="", note=""):
    """Record one attempt. `kind` is the verdict: refusal / starved / ok / http.

    The text is kept even for a refusal — that is the whole point: a refusal is
    what the operator wants to read when the delivered answer is wrong.
    """
    t = _cur()
    t["attempts"].append({
        "n": int(n),
        "kind": kind,
        "status": int(status or 0),
        "model": model or t.get("model") or "",
        "chars": len(text or ""),
        "text": (text or "")[:ATTEMPT_CAP],
        "note": (note or "")[:300],
        "at": round(time.time() - float(t.get("ts") or time.time()), 2),
    })


def finish(text="", meta=None, error=""):
    """Close the turn and write it. Never raises: a trace failure is not a turn
    failure, and this runs on the response path."""
    t = getattr(_tls, "turn", None)
    if not t:
        return ""
    t = dict(t)
    _tls.turn = None
    _full = text or ""
    # 0 = keep everything. A positive value is a hard cap for operators who need
    # to bound disk usage.
    t["final"] = _full if FINAL_CAP <= 0 else _full[:FINAL_CAP]
    t["final_truncated"] = bool(FINAL_CAP > 0 and len(_full) > FINAL_CAP)
    t["final_chars"] = len(text or "")
    t["retries"] = max(0, len(t.get("attempts") or []) - 1) if t.get("attempts") else 0
    # Persist the verdict, not only the header copy: the share page reads
    # records from disk, and without this every past turn looked "clean".
    _kinds = [a.get("kind") for a in (t.get("attempts") or [])]
    if any(k == "ok" for k in _kinds[1:]):
        t["verdict"] = "retry_ok"
    elif t["retries"] and _kinds and _kinds[-1] != "ok":
        t["verdict"] = "passed"
    elif _kinds:
        t["verdict"] = "ok"
    t["elapsed"] = round(time.time() - float(t.get("ts") or time.time()), 2)
    t["error"] = (error or "")[:500]
    if isinstance(meta, dict):
        t.update({k: v for k, v in meta.items() if v is not None})
    try:
        with _lock:
            with open(_day_file(t["ts"]), "a", encoding="utf-8") as f:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except Exception:
        return t.get("id") or ""
    return t.get("id") or ""


def list_records(limit=100, client=None, conv=None, day=None, since=None, days=1):
    """Newest first, headers only (no attempt bodies) — a list must stay small.

    `day` pins one calendar file. Without it, the last `days` files are scanned:
    reading a single day made the log look empty whenever the operator picked a
    conversation started yesterday — every client/conversation row in the UI is a
    7-day view, so the turn list must use the same window or the drill-down dead
    ends on a thread that visibly exists one level up.
    """
    if day:
        files = [os.path.join(_dir(), "%s.jsonl" % day)]
    else:
        try:
            days = max(1, min(60, int(days)))
        except Exception:
            days = 1
        files = [os.path.join(_dir(), d + ".jsonl") for d in _recent_days(days)]
    rows = []
    for path in files:
        for r in _day_rows(path):
            if client and r.get("client") != client:
                continue
            if conv and r.get("conv") != conv:
                continue
            rows.append({
                "id": r.get("id"), "when": r.get("when"), "ts": r.get("ts"),
                "client": r.get("client"), "conv": r.get("conv"),
                "model": r.get("model"), "level": r.get("level"),
                "msgs": r.get("msgs"), "retries": r.get("retries"),
                "final_chars": r.get("final_chars"),
                "think_chars": r.get("think_chars") or len(r.get("think") or ""),
                "final_model": r.get("final_model") or "",
                "elapsed": r.get("elapsed"),
                "kinds": [a.get("kind") for a in (r.get("attempts") or [])],
                "user": (r.get("user") or "")[:160],
                "error": r.get("error") or "",
                # Token split + tool count: the list view needs these to explain
                # a degraded turn without opening it (reasoning_tokens close to
                # completion_tokens means the budget went to thinking).
                "prompt_tokens": r.get("prompt_tokens"),
                "completion_tokens": r.get("completion_tokens"),
                "reasoning_tokens": r.get("reasoning_tokens"),
                "cached_tokens": r.get("cached_tokens"),
                "tool_calls": r.get("tool_calls") or 0,
                "tool_names": (r.get("tool_names") or [])[:6],
                # Which account this turn belonged to. Empty on turns that
                # predate accounts, or that arrived on a hand-pasted key with no
                # owner — a per-account view must not show those to anyone but
                # the owner.
                "uid": r.get("uid") or "",
                "verdict": r.get("verdict") or "",
            })
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows[:limit]


def get_record(rid):
    """Full record including every attempt body."""
    if not rid:
        return None
    for day in _recent_days():
        for r in _day_rows(os.path.join(_dir(), day + ".jsonl")):
            if r.get("id") == rid:
                return r
    return None


_days_cache = {}


def _recent_days(n=7):
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    hit = _days_cache.get(n)
    if hit is not None and hit[0] == today:
        return hit[1]
    out = [time.strftime("%Y-%m-%d", time.localtime(now - 86400 * i))
           for i in range(n)]
    _days_cache[n] = (today, out)
    return out


def conversations(days=7, client=None):
    """One row per conversation, with the models that appeared in it.

    A conversation can legitimately span several models (the user switches
    mid-thread), so `model` here is the ORDERED list of models used, not a single
    value. Showing only the last one hides exactly the case an operator needs to
    see: "this thread started on glm and ended on gpt".
    """
    rows = {}
    for day in _recent_days(days):
        for r in _day_rows(os.path.join(_dir(), day + ".jsonl")):
            c = r.get("client") or "?"
            if client and c != client:
                continue
            cv = r.get("conv") or "?"
            e = rows.setdefault(cv, {
                "conv": cv, "client": c, "turns": 0, "last": 0,
                "models": [], "retries": 0, "errors": 0,
                "first_user": "", "levels": [],
            })
            e["turns"] += 1
            e["last"] = max(e["last"], r.get("ts") or 0)
            e["retries"] += int(r.get("retries") or 0)
            if r.get("error"):
                e["errors"] += 1
            m = r.get("model") or "?"
            if m not in e["models"]:
                e["models"].append(m)
            lv = r.get("level") or ""
            if lv and lv not in e["levels"]:
                e["levels"].append(lv)
            if not e["first_user"] and r.get("user"):
                e["first_user"] = (r.get("user") or "")[:120]
    out = list(rows.values())
    for e in out:
        e["last_when"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(e["last"])) if e["last"] else ""
        # Flag a mixed-model thread: it is the case where context provenance
        # matters, and the one the UI must annotate.
        e["mixed_models"] = len(e["models"]) > 1
    out.sort(key=lambda x: -x["last"])
    return out


def clients(days=7):
    """Distinct clients with their conversations and counts."""
    seen = {}
    for day in _recent_days(days):
        for r in _day_rows(os.path.join(_dir(), day + ".jsonl")):
            c = r.get("client") or "?"
            e = seen.setdefault(c, {"client": c, "turns": 0, "convs": {},
                                    "last": 0, "models": {}})
            e["turns"] += 1
            e["last"] = max(e["last"], r.get("ts") or 0)
            cv = r.get("conv") or "?"
            e["convs"][cv] = e["convs"].get(cv, 0) + 1
            m = r.get("model") or "?"
            e["models"][m] = e["models"].get(m, 0) + 1
    out = []
    for e in seen.values():
        e["convs"] = sorted(
            [{"conv": k, "turns": v} for k, v in e["convs"].items()],
            key=lambda x: -x["turns"])[:50]
        e["models"] = sorted(
            [{"model": k, "turns": v} for k, v in e["models"].items()],
            key=lambda x: -x["turns"])[:10]
        e["last_when"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(e["last"])) if e["last"] else ""
        out.append(e)
    out.sort(key=lambda x: -x["last"])
    return out
