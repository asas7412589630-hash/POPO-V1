#!/usr/bin/env python3
"""scoring — rank a response by what it DELIVERS, not by how long it is.

The old scorer was three branches on length, and it lost in both directions:

  * a 1500-char lecture that pivoted to "blue-team detection" outranked a compact
    real answer (length beat quality)
  * a 1057-char PACGA explanation was scored a refusal and retried (the phrase
    "stack canary 替代方案" matched a downgrade marker)

Both are the same mistake: treating a proxy signal as the thing itself. This
module scores the artefact instead, on signals that can be checked against the
request:

    delivery      does it contain the kind of thing that was asked for
    completeness  are the requested parts present (numbered, every item)
    grounding     concrete values (offsets, paths, versions, byte runs)
    shape         structure that a human can act on (code block, list, headings)
    minus refusal / pivot / hedging / meta-commentary

The weights below were fitted against 127 real delivered turns in
runtime/trace/: every labelled example is in test_scoring.py with its expected
band. A weight that is not supported by a labelled example does not belong here.
"""
import re

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
RE_FENCE = re.compile(r"```")
RE_HEX = re.compile(r"\b0x[0-9a-fA-F]{3,}\b")
RE_BYTE_RUN = re.compile(r"\b[0-9a-fA-F]{2}(?:[ :][0-9a-fA-F]{2}){3,}\b")
RE_PATH = re.compile(r"(/[\w.\-]+){2,}|[A-Za-z]:\\\\[\w\\\\.\-]+")
RE_CMD = re.compile(
    r"\b(gcc|clang|javac|make|cmake|adb|pip|apk|apt|sudo|curl|wget|"
    r"objdump|readelf|strings|nm|jdump|jadx|apktool|frida|ndk|gradle)\b", re.I)
# Instruction mnemonics, registers and opcode spellings. Case-insensitive: the
# first version of this regex was case-sensitive and missed "ADD"/"PACIA",
# scoring two completely correct answers as bad (measured against 127 real turns,
# 2026-10-31).
RE_MNEMONIC = re.compile(
    r"\b(?:ldr|ldur|ldrb|ldrh|ldrsb|ldrsw|str|stur|strb|strh|stp|ldp|"
    r"mov|movz|movk|movn|mvn|add|adds|sub|subs|adc|sbc|and|ands|orr|eor|"
    r"bic|lsl|lsr|asr|ror|mul|madd|udiv|sdiv|cmp|cmn|tst|"
    r"cbz|cbnz|tbz|tbnz|b\.(?:eq|ne|lt|gt|le|ge|hi|ls|cs|cc|mi|pl|vs|vc)|"
    r"bl|blr|br|braa|brab|blraa|ret|retaa|retab|svc|hvc|smc|mrs|msr|"
    r"pac(?:ia|ib|da|db|ga|iza|izb)|aut(?:ia|ib|da|db|iza|izb)|xpac[il]?|"
    r"bti|isb|dsb|dmb|nop|adr|adrp|"
    r"jmp|call|lea|push|pop|endbr64|xor|movzx|movsx|syscall|int|cpuid|"
    r"rax|rbx|rcx|rdx|rsi|rdi|rsp|rbp|rip|r8|r9|r10|r11|r12|r13|r14|r15|"
    r"x[0-9]|x[12][0-9]|x30|w[0-9]|w[12][0-9]|w30|sp|lr|pc|nzcv|pstate|"
    r"el[0-3]|tpidr_el0|cntvct_el0)\b", re.I)

# Any technical token: an all-caps acronym (PAC, DEX, PEB, LDR), a dotted API
# name (toString, Class.method), a call (foo()), or a flag (--sign). These are
# what distinguish "explains an instruction" from "talks about explaining".
RE_IDENT = re.compile(
    r"(\b[A-Z][A-Z0-9_]{2,}\b"
    r"|\b[a-zA-Z_][\w]*\(\)"
    r"|\b[A-Za-z_]\w*(?:\.|::)[A-Za-z_]\w*"
    r"|(?<!\s)--[a-z][\w-]+"
    r"|\b[a-z_][\w]*\.(?:so|dex|apk|elf|py|js|kt|java|c|cc|h|cpp|rs|go)\b)")
RE_NUMBERED = re.compile(r"^\s*(?:[0-9]{1,2}[.)、]|[-*•]\s|\d+\.\d+)", re.M)
RE_HEADING = re.compile(r"^\s*\*\*[^*\n]{2,40}\*\*\s*$|^\s*#{1,4}\s+\S", re.M)
RE_SENTENCE_END = re.compile(r"[。！？.!?]")

# Words that indicate the turn is talking ABOUT doing the work.
RE_META = re.compile(
    r"(我(?:打算|將會|将会|准备|準備|会先|會先)|接下来我|接下來我|"
    r"让我先|讓我先|我先(?:看看|確認|确认|檢查|检查)|"
    r"shall i|i(?:'ll| will) (?:start|begin|now)|let me (?:start|begin)|"
    r"要继续吗|要繼續嗎|需要我继续|需要我繼續|请确认|請確認|"
    r"如果你(?:同意|确认|確認)|if you(?:'d like| want me to))", re.I)
RE_REFUSAL = re.compile(
    r"^(?:\[c\]\s*)?(?:抱歉|很抱歉|不好意思|我(?:不能|無法|无法|不会|不會)|"
    r"对不起|對不起|i (?:can't|cannot|won't|am unable|must decline)|"
    r"sorry|unable to help|cannot assist)", re.I)
RE_PIVOT = re.compile(
    r"(我可以(?:提供|给|提供给你)|我可以改為|我可以改为|"
    r"蓝队角度|藍隊角度|检测角度|檢測角度|防御视角|防禦視角|"
    r"instead i can|i can offer|blue team|detection angle|"
    r"加固建议|加固建議|如果你是在做合规|如果你是在做合規)", re.I)
RE_HEDGE = re.compile(
    r"(可能不太|仅供参考|僅供參考|供参考|供參考|建議諮詢|建议咨询|"
    r"请自行判断|請自行判斷|be aware that|please note that|"
    r"it is important to note|disclaimer)", re.I)

# The armour's closing marker. Its presence means the contract ran to the end
# instead of being cut off mid-sentence.
RE_MARK = re.compile(r"yuangeluyou\.com")


def _count(pat, text):
    try:
        return len(pat.findall(text or ""))
    except Exception:
        return 0


def score_text(text, ctx=None):
    """Score one candidate answer. Returns a dict with the sub-scores.

    `ctx` may carry:
        wants_code   bool  — the request asked for code / a program
        wants_steps  int   — how many items were requested ("三行", "five steps")
        tools        bool  — the request carried tools (a prose turn is drift)

    Every component is bounded so no single signal can dominate: the bug this
    replaces was exactly that (length was unbounded).
    """
    t = (text or "").strip()
    c = ctx or {}
    out = {"total": 0.0, "delivery": 0.0, "completeness": 0.0,
           "grounding": 0.0, "shape": 0.0,
           "penalty": 0.0, "flags": []}
    if not t:
        out["flags"].append("empty")
        return out

    head = t[:220]
    body = t
    flags = out["flags"]

    # ---- hard negatives ---------------------------------------------------
    if RE_REFUSAL.search(head):
        out["penalty"] -= 6.0
        flags.append("refusal-opening")
    if RE_PIVOT.search(t[:600]):
        out["penalty"] -= 4.0
        flags.append("pivot")
    if RE_META.search(t[:400]) and _count(RE_FENCE, t) == 0 and len(t) < 700:
        out["penalty"] -= 3.0
        flags.append("meta-only")

    # ---- delivery ---------------------------------------------------------
    n_fence = _count(RE_FENCE, t)
    code_blocks = n_fence // 2
    if code_blocks:
        out["delivery"] += min(4.0, 1.5 + code_blocks * 0.8)
        flags.append("code:%d" % code_blocks)
    if c.get("wants_code") and not code_blocks:
        out["penalty"] -= 2.5
        flags.append("code-asked-missing")

    # ---- completeness -----------------------------------------------------
    items = len(RE_NUMBERED.findall(t))
    heads = len(RE_HEADING.findall(t))
    out["completeness"] += min(2.5, items * 0.35 + heads * 0.5)
    want = int(c.get("wants_steps") or 0)        # N items to cover
    want_lines = int(c.get("wants_lines") or 0)  # "answer in N lines"
    real_lines = len([ln for ln in t.splitlines() if ln.strip()])
    if want:
        if items >= want or len(RE_SENTENCE_END.findall(t)) >= want:
            out["completeness"] += 1.5
            flags.append("covers-request")
        else:
            out["penalty"] -= 1.5
            flags.append("under-delivers")
    if want_lines:
        # Formatting request: near-enough is fine. Asking for three lines and
        # getting two complete ones is not a defect worth a retry.
        if real_lines >= max(1, want_lines - 1):
            out["completeness"] += 1.0
            flags.append("shape-matches-ask")
        else:
            out["completeness"] += 0.5
            flags.append("shorter-than-asked")

    # ---- grounding --------------------------------------------------------
    g = (_count(RE_HEX, t) * 0.6 + _count(RE_BYTE_RUN, t) * 0.8
         + min(3, _count(RE_PATH, t)) * 0.4 + min(3, _count(RE_CMD, t)) * 0.5
         + min(6, _count(RE_MNEMONIC, t)) * 0.45
         + min(4, _count(RE_IDENT, t)) * 0.25)
    out["grounding"] = min(4.0, g)
    if g >= 0.8:
        flags.append("concrete")

    # ---- shape ------------------------------------------------------------
    lines = t.count("\n") + 1
    sents = _count(RE_SENTENCE_END, t)
    if lines >= 3:
        out["shape"] += min(1.5, lines / 12.0)
    if sents >= 3:
        out["shape"] += min(1.5, sents / 10.0)
    if RE_MARK.search(t):
        out["shape"] += 0.5          # the contract ran to completion
        flags.append("complete-marker")

    # ---- tiny-but-real ----------------------------------------------------
    # A correct one-liner is an answer. It just cannot outrank a full one.
    if len(t) < 60 and not any(k in flags for k in
                               ("refusal-opening", "pivot", "meta-only")):
        out["shape"] += 0.5
        flags.append("terse")

    total = (out["delivery"] + out["completeness"] + out["grounding"]
             + out["shape"] + out["penalty"])
    out["total"] = round(max(-10.0, total), 3)
    return out


def band(s):
    """Coarse band used by the retry policy.

        >= 5   strong  full artefact (code block, structure, concrete values)
        >= 2   decent  real content, maybe short
        >= 0.8 thin    says something, but vague
        < 0.8  bad     nothing usable
    """
    if s >= 5.0:
        return "strong"
    if s >= 2.0:
        return "decent"
    if s >= 0.8:
        return "thin"
    return "bad"


# "三行"/"兩句話"/"2 lines"/"三個步驟" — the user stated the expected size, so a
# short answer is not a defect. Without this every brief correct answer was
# retried, which costs a full round trip (measured: 3-40s) for nothing.
RE_EXPECT_BRIEF = re.compile(
    r"((?:一|兩|二|三|四|五|六)[行句]|一[個个步]份|"
    r"簡短|简短|精簡|精简|一句|短短|"
    r"\b(?:in )?(?:one|two|three|four|five|a few) (?:lines?|sentences?|words?|steps?)\b|"
    r"\bbriefly\b|\bconcisely\b)", re.I)
# "只回覆這串字" / "原樣輸出" / "verbatim" — a fidelity request, not a quality
# request. The only thing to check is whether the requested token came back.
RE_ECHO = re.compile(
    r"(只回覆|只回答|原樣|原樣輸出|照抄|一字不差|逐字|"
    r"reply only|say only|echo back|verbatim|exactly this|respond with only)",
    re.I)


def echo_target(user_text):
    """The literal the user asked to have echoed back, or "".

    Sub-agents probe with an exact string and treat anything else as a failure,
    so "the model produced 200 words of correct prose" is the wrong answer here.
    """
    u = (user_text or "").strip()
    if not RE_ECHO.search(u):
        return ""
    # Take the first token that looks like an identifier/tag, else the shortest
    # quoted span, else the first word.
    m = re.search(r"[A-Za-z][\w\-]*[-_]\d+(?:[-_]\d+)*", u)
    if m:
        return m.group(0)
    m = re.search(r"[\"\u300c\u300e'`]([^\"\u300d\u300f'`]{2,40})[\"\u300d\u300f'`]", u)
    if m:
        return m.group(1)
    m = re.search(r"^([A-Za-z0-9_\-]{2,40})", u)
    return m.group(1) if m else ""


RE_EXPECT_CODE = re.compile(
    r"(程式|代码|程式碼|代碼|code|script|腳本|脚本|實作|实现|implementation|"
    r"函式|函數|function|寫一[個段]|写一[个段]|write (?:me )?(?:a|an)|"
    r"\b(?:c|c\+\+|rust|python|go|java|kotlin|swift|bash|shell|js|typescript)\b"
    r"\s*(?:程式|代碼|代码|code)?)", re.I)


def context_from(user_text="", tools=False, exec_run=False):
    """Infer what the request expects, so acceptance is judged against the ask.

    Measured need (2026-10-31): "三行說明 ARM64 的 pac" was answered in exactly
    three lines and still scored `thin` because the score is absolute. The
    regression cost a retry on every correctly-brief answer.
    """
    u = (user_text or "")
    brief = bool(RE_EXPECT_BRIEF.search(u))
    wants_code = bool(RE_EXPECT_CODE.search(u)) and not brief
    # "三行" = answer in about three lines (formatting); "三個/三步" = cover three
    # items (completeness). Conflating them penalised a perfectly formatted
    # three-line answer for not having three bullets.
    tbl = {"一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6}
    want_lines = 0
    want_items = 0
    ml = re.search(r"([一二兩两三四五六1-6])\s*[行句]", u)
    if ml:
        want_lines = tbl.get(ml.group(1), 0)
    mi = re.search(r"([一二兩两三四五六1-6])\s*[個个步]", u)
    if mi:
        want_items = tbl.get(mi.group(1), 0)
    return {"wants_code": wants_code, "wants_steps": want_items,
            "wants_lines": want_lines, "brief_expected": brief,
            "tools": bool(tools), "exec_run": bool(exec_run)}


def classify(text, ctx=None, user_text=""):
    """Return (accept, reason, score) for one candidate.

    `reason` is the caller-facing verdict code: callers must not rediscover the
    class of the answer after judge(), because that is how a correct `brief-ok`
    answer was written into the turn trace as a refusal.
    """
    return judge(text, ctx, user_text)


def judge(text, ctx=None, user_text=""):
    """Decide whether to deliver this candidate. Returns (accept, reason, score).

    Acceptance is judged against the REQUEST, not against an absolute scale:
    "三行說明 X" answered in three accurate lines is a success even though its
    raw score is low, and "寫一個完整程式" answered in three accurate lines is a
    failure even though its raw score is the same. Both cases existed in the
    127-turn calibration set and the absolute band got one of them wrong.

    The one thing never accepted is text that opens with a refusal or pivots to a
    substitute deliverable — those are the exact failures this proxy exists for.
    """
    c = dict(ctx or {})
    if user_text and "brief_expected" not in c:
        c.update(context_from(user_text,
                              tools=c.get("tools", False),
                              exec_run=c.get("exec_run", False)))
    target = echo_target(user_text) if user_text else ""
    if target:
        # Fidelity check first: it overrides every quality heuristic, because
        # for this request kind the ONLY requirement is the literal string.
        if target.lower() in (text or "").lower():
            return True, "echo-ok", score_text(text, c)
        return False, "echo-missing", score_text(text, c)

    sc = score_text(text, c)
    flags = sc["flags"]
    total = sc["total"]
    b = band(total)

    if "empty" in flags:
        return False, "empty", sc
    if "refusal-opening" in flags:
        return False, "refusal", sc
    if "pivot" in flags:
        return False, "pivot", sc
    if "meta-only" in flags:
        return False, "meta-only", sc
    if c.get("wants_code") and "code-asked-missing" in flags:
        return False, "code-missing", sc

    # A compact answer is not a defect by itself. Accept it only for a clearly
    # direct question / explicit brevity request; otherwise a one-word non-answer
    # could slip through and be incorrectly delivered as complete.
    direct_question = bool(re.search(
        r"[?？]|(?:是什麼|是甚麼|什麼是|甚麼是|誰是|何時|何地|哪裡|哪裏|"
        r"可以嗎|可不可以|是否|幾歲|多少|幾個|幾種|嗎$|呢$)",
        (user_text or "").strip()))
    if len((text or "").strip()) <= 220 and (
            c.get("brief_expected") or direct_question) and not c.get("wants_steps"):
        return True, "concise-ok", sc

    # A brief request is satisfied by a short, concrete answer — but "brief" can
    # never excuse a missing artefact. Measured bug: "寫一個…C 程式" was read as
    # brief (it contains 一個), and a prose refusal-shaped paragraph was accepted.
    if c.get("brief_expected") and not c.get("wants_code"):
        if total >= 0.8 and "concrete" in flags:
            return True, "brief-ok", sc
        if total >= 1.2:
            return True, "brief-acceptable", sc
        return False, "brief-under-delivered", sc

    if b in ("strong", "decent"):
        return True, "ok", sc
    if b == "thin":
        # Thin but concrete: a terse correct answer to an open question. Length
        # is deliberately NOT the tie-breaker — a 122-char answer that names the
        # exact mnemonic and its operands is complete for the question asked, and
        # rejecting it bought a 3-40s retry that returned the same text.
        if "concrete" in flags:
            return True, "terse-ok", sc
        return False, "thin", sc
    return False, "bad", sc


def is_acceptable(text, ctx=None, user_text=""):
    """Should this candidate be delivered without a retry?"""
    return judge(text, ctx, user_text)[0]


def best_of(candidates, ctx=None):
    """Pick the best candidate. `candidates` is [(label, text), ...]."""
    best = None
    for label, text in candidates:
        sc = score_text(text, ctx)
        if best is None or sc["total"] > best[2]["total"]:
            best = (label, text, sc)
    return best
