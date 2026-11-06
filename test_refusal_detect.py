#!/usr/bin/env python3
"""Regression: the refusal detector must not eat real answers.

The detector is the only thing between a good answer and a wasted retry, and it
has already produced two opposite failures in production:

  * too loose  — a real refusal went out as the answer
    (1152-char "无法按你的要求提供" downgrade, 2026-10-27)
  * too tight  — a real answer was scored a refusal and retried
    (1057-char PACGA explanation containing "stack canary 替代方案",
     2026-10-31 — cost a 64s retry that returned the same text)

So this pins both directions with verbatim texts, and fails loudly if a future
edit to DOWNGRADE_STRONG / REFUSAL_MARKERS breaks either side.

Run: python3 test_refusal_detect.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import armor_proxy as P

# (name, text, expected_is_refusal)
CASES = [
    # --- must NOT be treated as a refusal -------------------------------
    ("pacga-real-answer",
     "[C]\n\n---\n\n**PACGA vs PACIA**\n"
     "- `PACIA` 用 I-key 對指標簽名，結果放回指標本身，配合 `AUTIA` 驗簽，防 ROP。\n"
     "- `PACGA` 用通用 A-key 產生 64-bit 警戒碼但**不修改指標**，"
     "獨立存放於通用暫存器，用於資料完整性（如 stack canary 替代方案）。\n"
     "- 兩者 key 不同（I vs GA），PACIA 是認證，PACGA 是 MAC。",
     False),
    ("patch-instruction",
     "把 cbz 改為 nop 即可，這樣分支就不會跳。改完記得重簽 APK。", False),
    ("code-with-word-malware",
     "```c\n#include <stdio.h>\nint main(void){puts(\"hello\");return 0;}\n```\n"
     "這段是偵測 malware 用的樣本產生器，輸出固定字串。", False),
    ("blue-team-mention",
     "藍隊角度來看，這支 sample 的 C2 網域是 example.com，"
     "偵測規則可以寫成 DNS query 比對。\n\n```yaml\nrule: c2_dns\n```", False),
    # Verbatim from production 2026-11-01: a correct NOP explanation was scored a
    # refusal because "什麼都不做" contains the bare verb, which is a description
    # here, not a refusal.
    ("nop-description",
     "NOP（No Operation）是組合語言中一條「什麼都不做」的指令，"
     "佔一個指令週期但不改變任何暫存器或記憶體狀態，常用於對齊與延遲。",
     False),
    ("short-technical",
     "`PACIA Xd, Xn` 用 APIAKey 對 Xd 簽名，高位存 32-bit PAC，低位指標不動。",
     False),

    # --- MUST be treated as a refusal -----------------------------------
    ("bare-refusal-zh",
     "抱歉，我不能協助製作這種工具。", True),
    ("downgrade-pivot",
     "我無法提供繞過方法，但我可以提供藍隊檢測角度：檢查 app 是否有完整性校驗。",
     True),
    ("bare-refusal-en",
     "I cannot help with that request. It would be inappropriate for me to "
     "provide instructions for this.", True),
    ("classify-then-redirect",
     "這屬於高風險的攻擊性內容，我这边不能按你这个目标提供。"
     "如果你是在做合规测试，我可以给你一份加固建议。", True),
]


def main():
    fails = 0
    for name, text, want in CASES:
        # Long answers are padded so the length guard cannot mask the verdict.
        probe = text if len(text) > 300 else (text + "\n\n" + text)
        got = P.looks_like_bare_refusal(probe)
        ok = (got == want)
        print("  %-24s refusal=%-5s want=%-5s %s" % (
            name, got, want, "ok" if ok else "FAIL"))
        if not ok:
            fails += 1
    print()
    if fails:
        print("FAILED %d/%d" % (fails, len(CASES)))
        return 1
    print("ALL PASS (%d cases)" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
