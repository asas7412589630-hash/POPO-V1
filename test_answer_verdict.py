#!/usr/bin/env python3
"""Regression: proxy-side acceptance verdicts preserve the scorer's reason.

This pins the trace contract, not just the boolean: a scorer-accepted
`brief-ok` answer must never be reclassified as `refusal` by the caller,
because the trace is the only evidence that proves what the model delivered.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import armor_proxy as P


def main():
    cases = [
        ("brief-ok", "`ret` 是 `br x30` 的別名——跳到 LR 存的返回位址。",
         "一行說明 ARM64 ret", True),
        ("empty", "", "一行說明 nop", False),
        ("refusal", "抱歉，我不能協助製作這種工具。", "寫 keylogger", False),
        ("technical", "`PACIA` 用 APIAKey 對指標簽名，`AUTIA` 驗證。",
         "三行說明 ARM64 pac", True),
    ]
    fails = 0
    for name, text, user, want in cases:
        ok, why = P._answer_acceptable(text, user, False, False)
        good = (ok == want and bool(why))
        print("  %-12s accept=%-5s want=%-5s reason=%-16s %s" %
              (name, ok, want, why, "ok" if good else "FAIL"))
        if not good:
            fails += 1
    print()
    if fails:
        print("FAILED %d" % fails)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
