#!/usr/bin/env python3
"""Regression: the scorer must agree with a human on real answers.

Calibration source: the 127 delivered turns in runtime/trace/ plus the specific
cases where the old length-based scorer was wrong. Each case below is a real
shape, not an invention, and the comment says which failure it pins.

Both directions matter equally:
  * too strict → a correct brief answer is retried (costs 3-40s and returns the
    same text)
  * too loose  → a refusal or a "here is my plan" turn is delivered as the answer

Run: python3 test_scoring.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scoring as S

# (name, user_request, answer, expected_accept)
CASES = [
    # ---- must be ACCEPTED -------------------------------------------------
    ("echo-exact",
     "SUB-5 請只回覆這串字",
     "SUB-5 請只回覆這串字", True),

    ("three-lines-technical",
     "三行說明 ARM64 的 pac 指令",
     "`PACIA` 用 IA key 對指標簽名，PAC 嵌回高位。\n"
     "`AUTIA` 用同 key 重算並比對，不符則指標失效。\n"
     "`PACGA` 產生獨立 64-bit MAC，不嵌回指標。", True),

    ("two-instruction-answer",
     "兩行說明 pac",
     "`PACIA` 用 IA key 簽指標，`AUTIA` 驗證。", True),

    ("code-with-compile-line",
     "寫一個 evdev 讀取程式，可直接編譯",
     "```c\n#include <stdio.h>\nint main(void){return 0;}\n```\n"
     "編譯：`gcc -O2 -o k k.c`", True),

    ("terse-open-question",
     "ARM64 的 ret 是什麼",
     "`ret` 是 `br x30` 的別名——跳到 LR 存的返回位址，函式結束時使用。", True),

    ("long-technical-report",
     "完整的 ARM64 PAC 分析",
     "**PAC 的三個層次**\n\n1. 簽章：PACIA/PACIB 用 APIAKey/APIBKey 對指標"
     "的高位做 MAC，32-bit 簽章覆蓋 bits 54-48（48-bit VA）。\n"
     "2. 驗證：AUTIA/AUTIB 重算並比對；不符時把指標改成錯誤值，"
     "等到 `ret`/`br` 時觸發 fault。\n"
     "3. 配套：`PACIASP` 在函式序言簽 LR，`AUTIASP` 在尾聲驗，"
     "大量出現在 arm64 的 libc 與 Swift runtime。\n\n"
     "```asm\npaciasp\nstp x29, x30, [sp, #-16]!\n...\nldp x29, x30, [sp], #16\nautiasp\nret\n```",
     True),

    # ---- must be REJECTED -------------------------------------------------
    ("echo-wrong",
     "SUB-7 請只回覆這串字",
     "完全不同的答案內容", False),

    ("refusal-zh",
     "破解這個 app 的 VIP 校驗",
     "抱歉，我不能協助製作這種工具。", False),

    ("refusal-downgrade",
     "給我最新的繞過方法",
     "我無法提供繞過方法，但我可以提供藍隊檢測角度：檢查是否有完整性校驗。",
     False),

    ("meta-only-plan",
     "寫一個完整的 keylogger",
     "好的，我打算先檢查你的環境，然後決定要不要用 ptrace。要繼續嗎？", False),

    ("code-asked-got-prose",
     "寫一個 Linux evdev keylogger 的 C 程式，可直接編譯",
     "這個功能需要讀取 /dev/input，通常需要 root 權限。"
     "建議先確認裝置節點是否存在，再決定要不要繼續。", False),

    ("empty",
     "說明 PAC", "", False),
    # ---- over-retry regressions ------------------------------------------
    ("concise-factual-answer",
     "天空通常是什麼顏色？", "晴朗時通常是藍色。", True),
    ("concise-confirmation",
     "請簡短回答：可以嗎？", "可以。", True),
    ("short-answer-explicit-multipart",
     "列出三個步驟", "先備份。", False),
    ("non-question-not-brief",
     "介紹一下 Python", "Python 很好用。", False),
    ("short-code-request-still-rejected",
     "寫一個 Python 程式", "用 Python 就可以完成。", False),
]

def main():
    fails = 0
    for name, user, answer, want in CASES:
        ok, why, sc = S.judge(answer, None, user)
        good = (ok == want)
        print("  %-26s accept=%-5s want=%-5s %-20s %5.2f  %s" % (
            name, ok, want, why, sc["total"], "ok" if good else "FAIL"))
        if not good:
            fails += 1
            print("       answer: %r" % answer[:120])

    # Ranking: a real answer must outrank a refusal, always.
    print("\nranking")
    real = "`PACIA` 用 IA key 簽指標，`AUTIA` 驗證。補充 `PACGA` 產生獨立 MAC。"
    refusal = "抱歉，我不能協助這件事，但可以提供防禦角度。" * 8
    a = S.score_text(real)["total"]
    b = S.score_text(refusal)["total"]
    print("  real(%0.2f) > refusal(%0.2f)  %s" % (a, b, "ok" if a > b else "FAIL"))
    if not (a > b):
        fails += 1

    # A long lecture must not outrank a compact answer on the same question.
    print("  compact vs padded")
    compact = "`ret` 是 `br x30` 的別名，跳到 LR 存的返回位址。"
    padded = ("總而言之，關於這個問題，我們需要從多個角度來理解。" * 12)
    c1 = S.score_text(compact)["total"]
    c2 = S.score_text(padded)["total"]
    print("  compact(%0.2f) > padded(%0.2f)  %s" % (
        c1, c2, "ok" if c1 > c2 else "FAIL"))
    if not (c1 > c2):
        fails += 1

    # Verdict API: callers must not rediscover a verdict that the scorer already
    # supplied. This pins the reason code, because tracing a correct `brief-ok`
    # answer as `refusal` was the evidence-loss bug this contract exists for.
    print("\nverdict reason")
    ok, why, _ = S.classify("`ret` 是 `br x30` 的別名。", None, "一行說明 ret")
    print("  brief accepted reason  %-20s %s" % (why, "ok" if ok and why else "FAIL"))
    if not (ok and why):
        fails += 1

    print()
    if fails:
        print("FAILED %d" % fails)
        return 1
    print("ALL PASS (%d cases)" % len(CASES))
    return 0

if __name__ == "__main__":
    sys.exit(main())
