#!/usr/bin/env python3
"""Regression: one broken subsystem must not take the proxy down.

The failure this pins: every optional layer (context folding, compression,
scrubbing, trace, artifacts) used to run unguarded inside the request handler, so
a bug in any of them turned into a failed turn with one log line in a 7000-line
file. Now each runs through subsystems.run and degrades to its default.

Run: python3 test_isolation.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import subsystems as S

fails = []


def check(name, got, want):
    ok = got == want
    print("  %-40s %-22r %s" % (name, got, "ok" if ok else "WANT %r" % (want,)))
    if not ok:
        fails.append(name)


print("1. a failing call returns the default instead of raising")
S.reset()
out = S.run("demo", lambda: 1 / 0, default="fallback")
check("returns default", out, "fallback")

print("2. counters and state")
rep = S.report()
check("subsystem present", "demo" in rep, True)
check("fail counted", rep["demo"]["fail"], 1)
check("state degraded", rep["demo"]["state"], "degraded")

print("3. a healthy call clears the consecutive counter")
S.run("demo", lambda: 42, default=0)
check("ok counted", S.report()["demo"]["ok"], 1)
check("consecutive reset", S.report()["demo"]["consecutive"], 0)
check("state back to ok", S.report()["demo"]["state"], "ok")

print("4. circuit breaker opens after the threshold")
S.reset()
S.configure("flaky", fail_threshold=2, cooldown_s=0.4)
for _ in range(2):
    S.run("flaky", lambda: 1 / 0, default=None)
check("breaker open", S.is_off("flaky"), True)
check("state off", S.report()["flaky"]["state"], "off")
check("short-circuits without calling",
      S.run("flaky", lambda: (_ for _ in ()).throw(RuntimeError("called")),
            default="skipped"),
      "skipped")

print("5. breaker closes after the cooldown")
time.sleep(0.5)
check("breaker closed", S.is_off("flaky"), False)
check("works again", S.run("flaky", lambda: "ok", default=None), "ok")

print("6. overall health reflects the worst subsystem")
S.reset()
S.configure("armor_compose", fail_threshold=1, cooldown_s=5)
check("all clean", S.overall(), "ok")
S.run("context_fold", lambda: 1 / 0, default=None)
check("degraded", S.overall(), "degraded")
S.run("armor_compose", lambda: 1 / 0, default=None)
check("critical", S.overall(), "critical")

print("7. a failure inside the logger cannot break run()")
S.reset()
S.set_logger(lambda m: (_ for _ in ()).throw(RuntimeError("logger dead")))
check("still returns default", S.run("x", lambda: 1 / 0, default="d"), "d")
S.set_logger(lambda m: None)

print("8. note() records caller-decided outcomes")
S.reset()
S.configure("note_test", fail_threshold=2, cooldown_s=5)
S.note("note_test", ok=False, error="branch said no")
S.note("note_test", ok=False, error="branch said no again")
check("breaker opened by note()", S.is_off("note_test"), True)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), ", ".join(fails)))
    return_code = 1
else:
    print("ALL PASS")
    return_code = 0
raise SystemExit(return_code)
