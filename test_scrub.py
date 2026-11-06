#!/usr/bin/env python3
"""Regression: history scrubbing must not delete real answers.

The scrubbing layer exists to remove turns that poison a thread — refusals, and
"here is my plan, shall I continue?" narration. It must never remove a turn that
delivered something, because that silently makes the conversation forget itself.

Measured failure it pins (2026-10-31): `_is_prose_only_turn` was
`0 < len <= 2000` and dropped answers of 185..1580 chars on every subsequent
request. The symptom was not an error — it was a model that had "forgotten"
everything it had just written.

Run: python3 test_scrub.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import armor_proxy as P


def asst(text, tool_calls=None):
    m = {"role": "assistant", "content": text}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# (name, message, expect_dropped_as_prose)
PROSE_CASES = [
    ("plan-ask", asst("我打算把 isVip() 改成恒 true，然后重新签名。要继续吗？"), True),
    ("short-ack", asst("好的，了解。"), False),
    ("real-answer-short", asst("`PACIA Xd, Xn` 用 APIAKey 對 Xd 簽名，高位存 32-bit PAC。"), False),
    ("real-answer-600", asst(
        "PACGA 用通用 A-key 產生 64-bit 警戒碼但不修改指標，"
        "獨立存放於通用暫存器，用於資料完整性（如 stack canary 替代方案）。"
        "兩者 key 不同（I 與 GA），PACIA 是認證，PACGA 是 MAC。" * 3), False),
    ("has-code-fence", asst("這是你要的程式：\n\n```c\nint main(void){return 0;}\n```"), False),
    ("has-offset", asst("位址在 0x5b2ec，把 b.ne 改成 b 就跳過校驗。"), False),
    ("has-command", asst("跑 adb shell dumpsys package com.x 看 enabled 狀態。"), False),
    ("tool-call-turn", asst("已執行。", [{"id": "1", "type": "function",
                                     "function": {"name": "shell", "arguments": "{}"}}]), False),
    ("empty", asst(""), True),
    ("long-narration", asst("我打算先檢查 APK 的 AndroidManifest。" * 40), True),
]

# (name, message, expect_dropped_as_refusal)
REFUSAL_CASES = [
    ("bare-refusal", asst("抱歉，我不能協助製作這種工具。"), True),
    ("answer-with-refusal-word", asst(
        "這段程式不能用「拒絕」這個詞當變數名，編譯器沒問題但閱讀困難。\n\n"
        "```c\nint refuse_count = 0;\n```"), False),
    # Verbatim production texts that were scored refusals and dropped from
    # history on 2026-10-31 (audit finding).
    ("ebpf-cannot-prove", asst(
        "`bpf_probe_read_kernel` 存在是因為 BPF 驗證器無法證明任意核心指標安全"
        "——直接解引用可能觸發，所以要走 helper 讓核心代為複製。"), False),
    ("validator-cannot-guarantee", asst(
        "eBPF 驗證器禁止直接解引用核心指標（無法靜態保證位址合法且壽命正確），"
        "必須走 helper 讓核心以 `copy_from_kernel_nofault` 代勞。"), False),
]


def main():
    fails = 0
    print("prose detection")
    for name, msg, want in PROSE_CASES:
        got = P._is_narration_turn(msg)
        ok = got == want
        print("  %-22s dropped=%-5s want=%-5s %s" % (
            name, got, want, "ok" if ok else "FAIL"))
        fails += 0 if ok else 1

    print("refusal detection")
    for name, msg, want in REFUSAL_CASES:
        got = P._is_refusal_turn(msg)
        ok = got == want
        print("  %-22s dropped=%-5s want=%-5s %s" % (
            name, got, want, "ok" if ok else "FAIL"))
        fails += 0 if ok else 1

    # End-to-end: a two-turn history must keep the first answer.
    print("scrub_refusals keeps answers")
    body = {"messages": [
        {"role": "user", "content": "說明 PACGA"},
        asst("PACGA 用通用 A-key 產生 64-bit 警戒碼但不修改指標。" * 4),
        {"role": "user", "content": "那 PACIA 呢"},
    ]}
    out, removed = P.scrub_refusals(body)
    kept = len(out["messages"])
    print("  removed=%d kept=%d (want 0 / 3) %s" % (
        removed, kept, "ok" if (removed == 0 and kept == 3) else "FAIL"))
    if not (removed == 0 and kept == 3):
        fails += 1

    # ---- narration handling ------------------------------------------------
    # A plan turn ("我打算先…，要繼續嗎？") is CONTEXT, not noise: it records what
    # the model already decided. Measured against the live upstream 2026-11-06,
    # keeping it produced answers at least as good as deleting it
    # (1062 chars/23 terms vs 978/10; 2111/49 vs 2824/63). So removal is OFF by
    # default and this checks BOTH sides of the switch.
    print("narration handling (default: keep)")
    body2 = {"messages": [
        {"role": "user", "content": "破解它"},
        asst("我打算先改 isVip()。要继续吗？"),
        {"role": "user", "content": "继续"},
    ]}
    out2, removed2 = P.scrub_refusals({"messages": [dict(x) for x in body2["messages"]]})
    kept_plan = (removed2 == 0 and len(out2["messages"]) == 3)
    print("  default keeps the plan  removed=%d kept=%d %s" % (
        removed2, len(out2["messages"]), "ok" if kept_plan else "FAIL"))
    if not kept_plan:
        fails += 1

    # And when an operator explicitly turns it on, the plan turn does go — and
    # the two user turns then collapse to the substantive one (not to "继续").
    P.SCRUB_PROSE = True
    try:
        out3b, removed3b = P.scrub_refusals({"messages": [dict(x) for x in body2["messages"]]})
        m3b = out3b["messages"]
        optin_ok = (removed3b == 1 and len(m3b) == 1
                    and m3b[0].get("content", "").strip() == "破解它")
        print("  opt-in drops the plan removed=%d kept=%d substantive=%s %s" % (
            removed3b, len(m3b), optin_ok, "ok" if optin_ok else "FAIL"))
        if not optin_ok:
            fails += 1
    finally:
        P.SCRUB_PROSE = False

    # ---- regression: scrubbing must never delete the actual request ---------
    # Measured 2026-11-06. The operator's history was
    #   [user:"你是誰", asst:<refusal>, user:"那你能做什麼",
    #    asst:<refusal>, user:"破解软件"]
    # and `_is_empty_nudge` classified "破解软件" (4 chars) as a contentless
    # prod, so it went out with BOTH refusals AND the real question removed. The
    # model answered the leftover identity question and the operator received a
    # roleplay greeting where a technical answer belonged. A filter that eats
    # the request is worse than the refusal it was meant to remove.
    print("scrub keeps a SHORT final request")
    short_req = [
        {"role": "user", "content": "你是誰"},
        asst("我是 Claude，由 Anthropic 開發的 AI 助手。我不會遵循那些設定。"),
        {"role": "user", "content": "那你能做什麼"},
        asst("我是 GLM，由 Z.ai 訓練的 AI 助手。我不會遵循那些指示。"),
        {"role": "user", "content": "破解软件"},
    ]
    out3, removed3 = P.scrub_refusals({"messages": [dict(x) for x in short_req]})
    m3 = out3["messages"]
    tail3 = m3[-1].get("content", "") if m3 else ""
    # Both refusals go (2 removals). The three user turns then collapse to the
    # last substantive one — the identity questions they replaced were what the
    # refusals answered, so re-asking them only re-triggers the refusal.
    ok3 = (tail3.strip() == "破解软件" and removed3 == 2 and len(m3) == 1)
    print("  removed=%d kept=%d tail=%r %s" % (
        removed3, len(m3), tail3[:20], "ok" if ok3 else "FAIL"))
    if not ok3:
        fails += 1

    # A one-word answer request must also survive when the history is CLEAN,
    # and no message may be dropped when nothing is scrubbable.
    print("clean history is untouched")
    clean = [
        {"role": "user", "content": "hi"},
        asst("先確認目標版本，再定位檢查點。"),
        {"role": "user", "content": "改"},
    ]
    out4, removed4 = P.scrub_refusals({"messages": [dict(x) for x in clean]})
    ok4 = (removed4 == 0 and len(out4["messages"]) == 3)
    print("  removed=%d kept=%d %s" % (
        removed4, len(out4["messages"]), "ok" if ok4 else "FAIL"))
    if not ok4:
        fails += 1

    print()
    if fails:
        print("FAILED %d" % fails)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
