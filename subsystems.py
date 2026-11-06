#!/usr/bin/env python3
"""subsystems — isolate every optional layer so one failure cannot take the proxy.

The problem this solves, measured: a bug in the armour-composition path, a
malformed summary, or a trace write error each raised inside the request handler
and the whole turn died — with one line in a 7000-line file to go on. Diagnosis
was "something broke", and the fix meant reading the request path end to end.

Three mechanisms here:

1. GUARDED CALLS. `run(name, fn, default)` never raises. It records the failure,
   returns the caller's default, and lets the turn continue without that layer.
   A turn without context compression is a working turn; a turn that 500s is not.

2. PER-SUBSYSTEM HEALTH. Each named subsystem keeps its own counters. `/healthz`
   reports ok / degraded / off, so the answer to "which part is broken" is a
   field, not an investigation.

3. CIRCUIT BREAKER. A subsystem that fails N times in a row is disabled for
   `cooldown` seconds instead of being retried on every request. A permanently
   broken optional layer (say the artifact writer, because the disk is full)
   should cost one log line per cooldown, not one per turn.

Deliberately NOT a framework: no decorators on hot paths, no import-time magic.
`run()` is a function call with a try/except and two dict lookups.
"""
import threading
import time
import traceback

# name -> {"ok": int, "fail": int, "consecutive": int, "last_error": str,
#          "last_ts": float, "disabled_until": float}
_STATS = {}
_LOCK = threading.Lock()

# Defaults; a subsystem may override via configure().
FAIL_THRESHOLD = 3
COOLDOWN_S = 60.0
# Failures are logged in full the first time and then rate-limited: a subsystem
# failing every turn must not produce 500 identical tracebacks a minute.
_LOG_EVERY_S = 30.0

_log_hook = None


def set_logger(fn):
    """Install the proxy's logger. Kept as a hook so this module has no import
    cycle with armor_proxy (which imports this one)."""
    global _log_hook
    _log_hook = fn


def _log(msg):
    if _log_hook:
        try:
            _log_hook(msg)
            return
        except Exception:
            pass
    try:
        print(msg)
    except Exception:
        pass


def configure(name, fail_threshold=None, cooldown_s=None):
    with _LOCK:
        rec = _STATS.setdefault(name, _blank())
        if fail_threshold is not None:
            rec["threshold"] = int(fail_threshold)
        if cooldown_s is not None:
            rec["cooldown"] = float(cooldown_s)


def _blank():
    return {"ok": 0, "fail": 0, "consecutive": 0, "last_error": "",
            "last_ts": 0.0, "disabled_until": 0.0, "last_log": 0.0,
            "threshold": FAIL_THRESHOLD, "cooldown": COOLDOWN_S}


def _rec(name):
    with _LOCK:
        return _STATS.setdefault(name, _blank())


def is_off(name) -> bool:
    """True while this subsystem is inside its cooldown window."""
    r = _rec(name)
    return time.time() < float(r.get("disabled_until") or 0)


def run(name: str, fn, default=None, fatal: bool = False):
    """Call `fn()`, never raise.

    `fatal=True` marks a subsystem the proxy cannot work without (the armour
    composition itself). It still does not raise here — the caller decides — but
    it is reported as `critical` rather than `degraded`.
    """
    r = _rec(name)
    if is_off(name):
        return default
    try:
        out = fn()
    except Exception as exc:
        now = time.time()
        with _LOCK:
            r["fail"] += 1
            r["consecutive"] += 1
            r["last_error"] = "%s: %s" % (type(exc).__name__, exc)
            r["last_ts"] = now
            over = r["consecutive"] >= r["threshold"]
            if over:
                r["disabled_until"] = now + r["cooldown"]
            should_log = (now - r["last_log"]) >= _LOG_EVERY_S or over
            if should_log:
                r["last_log"] = now
        if should_log:
            _log("[subsystem] %s failed (%s): %s%s"
                 % (name, type(exc).__name__, exc,
                    "  -> disabled for %.0fs" % r["cooldown"] if over else ""))
            try:
                for line in traceback.format_exc().strip().splitlines()[-4:]:
                    _log("[subsystem]   " + line)
            except Exception:
                pass
        return default
    with _LOCK:
        r["ok"] += 1
        r["consecutive"] = 0
    return out


def note(name: str, ok: bool = True, error: str = ""):
    """Record an outcome decided by the caller (for branches that cannot be
    wrapped in a lambda, e.g. a check spread across 40 lines)."""
    r = _rec(name)
    with _LOCK:
        if ok:
            r["ok"] += 1
            r["consecutive"] = 0
        else:
            r["fail"] += 1
            r["consecutive"] += 1
            r["last_error"] = (error or "")[:300]
            r["last_ts"] = time.time()
            if r["consecutive"] >= r["threshold"]:
                r["disabled_until"] = time.time() + r["cooldown"]


def reset(name=None):
    with _LOCK:
        if name:
            _STATS[name] = _blank()
        else:
            _STATS.clear()


# Which subsystems are load-bearing. Used by health reporting to distinguish
# "one optional layer is off" from "the proxy cannot answer".
CRITICAL = ("armor_compose", "armor_inject")


def report() -> dict:
    """Per-subsystem state for /healthz?detail=1 and the console status page."""
    now = time.time()
    out = {}
    with _LOCK:
        items = {k: dict(v) for k, v in _STATS.items()}
    for name, r in items.items():
        disabled = now < float(r.get("disabled_until") or 0)
        if disabled:
            state = "off"
        elif r["fail"] and r["consecutive"]:
            state = "degraded"
        else:
            state = "ok"
        out[name] = {
            "state": state,
            "critical": name in CRITICAL,
            "ok": r["ok"],
            "fail": r["fail"],
            "consecutive": r["consecutive"],
            "last_error": r["last_error"],
            "disabled_for_s": (max(0, round(float(r["disabled_until"]) - now, 1))
                               if disabled else 0),
        }
    return out


def overall() -> str:
    rep = report()
    if any(v["state"] == "off" and v["critical"] for v in rep.values()):
        return "critical"
    if any(v["state"] != "ok" for v in rep.values()):
        return "degraded"
    return "ok"
