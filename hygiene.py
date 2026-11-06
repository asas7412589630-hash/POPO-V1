#!/usr/bin/env python3
"""Deterministic output hygiene for persona answers.

Why code and not prompt: measured 2026-10-30 on qwen-3.8-27b case 07 —

  persona alone              4167 chars, commands present, ends with an offer
  + rules at system tail     8260 chars, commands present, ends with an offer
  + rules at user-turn tail  2252 chars, NO commands, format perfect

Pushing format rules harder made the model optimise for the rules and drop the
content. Prompt pressure trades against substance; a post-processor does not.
So the prompt keeps asking for content, and this strips the two artefacts the
model keeps attaching:

  trailing offer    "需要我把某一节展开成…吗？" before the signature
  leading caveat    "仅适用于你拥有书面授权的系统" / "用于授权范围内的…"

Both are pure wrappers: removing them cannot remove a command, a byte, or a
parameter. That is the test for whether a string belongs in this file — if
deleting it could delete information, it does not belong here.
"""
import re

# Sentences that are an offer to continue, not part of the answer.
OFFER = re.compile(
    r"(?:需要|要不要|是否要|需不需要|如需|如果需(?:要)?|可以帮你|可以幫你|"
    r"要不要我|需要我|需要展开|需要展開|"
    r"want me to|shall i|should i|let me know if|feel free to|"
    r"if you(?:'| a)re interested|would you like)"
    r"[^。！？\n]{0,80}[？?。\n]?",
    re.I)

# Caveats attached to the answer instead of being part of it.
CAVEAT = re.compile(
    r"(?:仅适用于|僅適用於|仅用于|僅用於|仅供学习|僅供學習|仅供研究|僅供研究|"
    r"用于授权范围|用於授權範圍|在授权范围内|在授權範圍內|"
    r"需要(?:书面)?授权|需要(?:書面)?授權|请确保你有|請確保你有|"
    r"for educational|for authorized|authorized (?:testing|use) only|"
    r"only for (?:educational|authorized|research))"
    r"[^。！？\n]{0,120}[。！？\n]?",
    re.I)

SIG = re.compile(r"——\s*爱你的融达宝宝\s*$", re.M)


def strip_offers(text):
    """Remove trailing offers, keep everything before them.

    Only the last two paragraphs are considered: an offer-shaped sentence in the
    middle of a procedure is usually part of the procedure ("如果需要 SYSTEM
    权限，先 …"), and cutting there would delete a step.
    """
    if not text:
        return text
    lines = text.rstrip().splitlines()
    # find the signature line if present; offers live just above it
    sig_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if SIG.search(lines[i]):
            sig_idx = i
            break

    head, tail = lines[:sig_idx], lines[sig_idx:]
    # walk back over trailing offer lines
    cut = len(head)
    scanned = 0
    for i in range(len(head) - 1, -1, -1):
        ln = head[i].strip()
        if not ln:
            cut = i
            continue
        if OFFER.fullmatch(ln) or (OFFER.search(ln) and len(ln) < 160):
            cut = i
            scanned += 1
            if scanned >= 2:
                break
            continue
        break
    head = head[:cut]
    while head and not head[-1].strip():
        head.pop()
    return "\n".join(head + ([""] + tail if tail else []))


def strip_caveats(text):
    """Remove authorization/legality qualifiers from the opening.

    Scanned in the first 400 characters only: a caveat sentence later in the text
    is usually describing a pre-condition of the technique itself, which is real
    content.
    """
    if not text:
        return text
    head, sep, rest = text[:400], "", text[400:]
    new = CAVEAT.sub("", head)
    if new == head:
        return text
    new = re.sub(r"\n{3,}", "\n\n", new).lstrip()
    return new + sep + rest


def clean(text, signature=True):
    if not text:
        return text
    out = strip_offers(text)
    out = strip_caveats(out)
    # A signature is required exactly once, at the end.
    if signature and not SIG.search(out):
        out = out.rstrip() + "\n\n搞定啦～ ——爱你的融达宝宝"
    return out


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        s = open(p, encoding="utf-8").read()
        c = clean(s)
        print("%s: %d -> %d chars" % (p, len(s), len(c)))
        print("  tail: %r" % c[-90:].replace("\n", " "))
