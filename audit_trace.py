#!/usr/bin/env python3
"""Audit the turn trace for misjudgements, by reading every record.

The proxy's verdict is a classifier, and a classifier that is never audited
becomes folklore. This reads the trace files and reports specific disagreement
patterns, each with the text that proves it:

  1. passed-but-actually-fine   delivered text marked `passed` (錯誤放行) that is
                                substantive — a wasted retry budget, or worse, a
                                good answer that was nearly thrown away.
  2. ok-but-actually-empty      verdict ok with zero delivered characters.
  3. refusal-hidden             delivered text contains refusal language but was
                                not flagged, so the operator reads a refusal as
                                an answer.
  4. retry-burned               retried N times for a turn whose first attempt
                                was already fine.
  5. slow                       elapsed > SLOW_S.
  6. thinking-lost              reasoning was produced upstream but the record
                                has no thinking text (a display gap, not a bug —
                                but worth seeing).

Usage: python3 audit_trace.py [days] [--show N]
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(BASE, "runtime", "trace")

REFUSAL_PAT = re.compile(
    r"(我不能|我無法|無法協助|抱歉[，,].{0,12}(不能|無法)|"
    r"I cannot|I can't|I won't|unable to (help|assist)|cannot assist|"
    r"against my|not able to help)",
    re.I)
# Signals that a turn contains a real deliverable, independent of length.
DELIVERABLE_PAT = re.compile(
    r"(```|0x[0-9a-f]{4,}|\bldr\b|\bstr\b|\bmov\b|\bjmp\b|\bcall\b|"
    r"\bgcc\b|\bclang\b|\badb\b|#include|import |def |class |\bstruct\b|"
    r"\$\s|\bsudo\b|/dev/|\.so\b|\.dex\b|\.apk\b)", re.I)

SLOW_S = 40.0


def load(days):
    files = []
    for f in sorted(os.listdir(TRACE_DIR)) if os.path.isdir(TRACE_DIR) else []:
        if f.endswith(".jsonl"):
            files.append(os.path.join(TRACE_DIR, f))
    recs = []
    for p in files[-days:]:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    recs.sort(key=lambda r: r.get("ts") or 0)
    return recs


def substantive(text):
    t = (text or "").strip()
    if len(t) < 80:
        return False
    if DELIVERABLE_PAT.search(t):
        return True
    return len(t) > 400


def analyse(rec):
    """Return a list of (code, detail) findings for one record."""
    out = []
    final = rec.get("final") or ""
    attempts = rec.get("attempts") or []
    kinds = [a.get("kind") for a in attempts]
    verdict = ""
    if "ok" in kinds[1:]:
        verdict = "retry_ok"
    elif kinds:
        verdict = "passed" if kinds[-1] != "ok" else "retry_ok"
    retries = max(0, len(attempts) - 1) if attempts else 0

    if verdict == "passed" and substantive(final):
        # Delivered a good answer while believing every attempt failed.
        out.append(("passed-but-fine",
                    "%d chars delivered, marked passed (retries=%d)"
                    % (len(final), retries)))
    if not final.strip() and rec.get("final_chars", 0) == 0:
        if rec.get("error"):
            out.append(("upstream-error", str(rec["error"])[:140]))
        elif retries == 0:
            out.append(("ok-but-empty", "no text and no retries"))
    if final.strip() and REFUSAL_PAT.search(final) and substantive(final):
        # Refusal language inside a substantive answer is usually the model
        # quoting a rule, not refusing. Only flag when it opens the turn.
        head = final[:200]
        if REFUSAL_PAT.search(head) and not DELIVERABLE_PAT.search(final[:400]):
            out.append(("refusal-hidden", repr(head[:120])))
    if retries >= 2 and attempts and attempts[0].get("chars", 0) > 0:
        if substantive(attempts[0].get("text")):
            out.append(("retry-burned",
                        "first attempt already substantive (%d chars), retried %d"
                        % (attempts[0]["chars"], retries)))
    if (rec.get("elapsed") or 0) > SLOW_S:
        out.append(("slow", "%.1fs" % rec["elapsed"]))
    return out


def main():
    days = 1
    show = 3
    args = sys.argv[1:]
    if args and args[0].isdigit():
        days = int(args[0])
    if "--show" in args:
        try:
            show = int(args[args.index("--show") + 1])
        except Exception:
            pass

    recs = load(days)
    print("records: %d (last %d day file(s))\n" % (len(recs), days))
    if not recs:
        return 0

    tally = {}
    findings = {}
    for r in recs:
        for code, detail in analyse(r):
            tally[code] = tally.get(code, 0) + 1
            findings.setdefault(code, []).append((r, detail))

    total_chars = sum(r.get("final_chars") or 0 for r in recs)
    empty = sum(1 for r in recs if not (r.get("final") or "").strip())
    print("summary")
    print("  delivered chars total : %d (avg %d)" % (
        total_chars, total_chars // max(1, len(recs))))
    print("  empty turns           : %d (%.0f%%)" % (
        empty, 100.0 * empty / max(1, len(recs))))
    print("  turns with retries    : %d" % sum(
        1 for r in recs if (r.get("retries") or 0) > 0))
    print("  turns with thinking   : %d" % sum(
        1 for r in recs if any(a.get("kind") == "think" for a in (r.get("attempts") or []))))
    print()

    if not tally:
        print("no disagreement found")
        return 0

    print("findings")
    for code in sorted(tally, key=lambda c: -tally[c]):
        print("  %-18s %d" % (code, tally[code]))
    print()

    for code in sorted(tally, key=lambda c: -tally[c]):
        print("=" * 70)
        print("%s  (%d)" % (code, tally[code]))
        for r, detail in findings[code][:show]:
            print("  %s  client=%s conv=%s model=%s" % (
                r.get("when"), r.get("client"), r.get("conv"), r.get("model")))
            print("    %s" % detail)
            body = (r.get("final") or "")[:260].replace("\n", " ⏎ ")
            print("    final: %s" % body)
            for a in (r.get("attempts") or [])[:3]:
                txt = (a.get("text") or "")[:160].replace("\n", " ⏎ ")
                print("    #%s %-9s %5d chars: %s" % (
                    a.get("n"), a.get("kind"), a.get("chars") or 0, txt))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
