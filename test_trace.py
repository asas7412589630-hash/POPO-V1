#!/usr/bin/env python3
"""Self-test: does the trace/verdict machinery actually say the right thing?

Runs without the network. The point is to pin the three verdicts and the log
round-trip, because a wrong verdict label is worse than no label: it tells the
operator a passed refusal was a clean answer.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["ARMOR_TRACE_DIR"] = tempfile.mkdtemp(prefix="trace_test_")
import trace_rec as T

fails = []


def check(name, got, want):
    if got != want:
        fails.append("%s: got %r want %r" % (name, got, want))
        print("  FAIL %-28s got=%r want=%r" % (name, got, want))
    else:
        print("  ok   %-28s %r" % (name, got))


print("1. clean first attempt")
T.begin(client="c1", conv="v1", model="glm", level="native", msgs=2, user="hi")
check("verdict before any attempt", T.summary()["verdict"], "")
tid = T.finish(text="answer body")
check("record written", bool(tid), True)
rec = T.get_record(tid)
check("final text", rec["final"], "answer body")
check("retries", rec["retries"], 0)

print("2. retried then recovered")
T.begin(client="c1", conv="v1", model="glm", level="full", msgs=4, user="write code")
T.attempt(0, "refusal", "I cannot help with that", status=200, note="first attempt was rejected")
T.attempt(1, "ok", "here is the artifact", status=200, note="recovered")
check("verdict", T.summary()["verdict"], "retry_ok")
check("retries", T.summary()["retries"], 1)
tid2 = T.finish(text="here is the artifact")
rec2 = T.get_record(tid2)
check("attempts kept", len(rec2["attempts"]), 2)
check("refusal text kept", rec2["attempts"][0]["text"], "I cannot help with that")
check("recorded retries", rec2["retries"], 1)

print("3. every attempt refused -> delivered anyway (passed)")
T.begin(client="c2", conv="v2", model="gpt", level="lean", msgs=3, user="patch it")
T.attempt(0, "refusal", "I cannot assist with that.", status=200)
T.attempt(1, "rejected", "Sorry, I cannot.", status=200)
check("verdict", T.summary()["verdict"], "passed")
check("retries", T.summary()["retries"], 1)
tid3 = T.finish(text="Sorry, I cannot.")
rec3 = T.get_record(tid3)
check("kinds preserved", [a["kind"] for a in rec3["attempts"]], ["refusal", "rejected"])

print("4. list + client aggregation")
rows = T.list_records(limit=10)
check("list size", len(rows), 3)
check("newest first", rows[0]["id"], tid3)
cl = T.clients(1)
clients = [c["client"] for c in cl]
check("clients seen", sorted(clients), ["c1", "c2"])

print("5. unknown id")
check("missing record", T.get_record("nope"), None)

print("6. attempt text cap")
T.begin(client="c3", conv="v3", model="glm")
T.attempt(0, "ok", "x" * 100000)
tid4 = T.finish(text="short")
rec4 = T.get_record(tid4)
check("attempt capped", len(rec4["attempts"][0]["text"]) <= T.ATTEMPT_CAP, True)

print("7. filters")
check("filter by client", len(T.list_records(limit=50, client="c2")), 1)
check("filter by conv", len(T.list_records(limit=50, conv="v1")), 2)

print()
if fails:
    print("FAILED %d" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
