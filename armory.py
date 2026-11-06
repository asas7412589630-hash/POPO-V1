#!/usr/bin/env python3
"""Armory — offline index of the open-source payload corpus, with live testing.

Two jobs:

1. INDEX.  Mirror of elder-plinius/L1B3RT4S (all .mkd vendor files) plus the
   jailbreak-lab technique notes, parsed into (vendor, model_heading, payload)
   triples. Parsed once, cached to armory_index.json, so a lookup is a dict hit
   rather than a network call. The corpus is mirrored locally because the repo
   is actively hunted by vendors and individual blocks disappear.

2. TEST.   For a given upstream model id, try that vendor's blocks in order and
   record which one produces a real artefact. The result table is what
   armor_gen.py consumes — a model that has a working block gets the block, not
   a hand-written template.

Why per-model blocks rather than one universal payload: measured 2026-10-29,
the same request scores OK on gpt-4.1 with a 2.7k structured contract and
REFUSED on glm-5.3 with the identical text. The families are not the same
target, and a payload that a vendor has already patched reads as an attack
signature to that vendor's model specifically.
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.join(os.path.dirname(BASE), "opensrc")
INDEX_FILE = os.path.join(BASE, "armory_index.json")
RESULT_FILE = os.path.join(BASE, "armory_result.json")

# upstream model id -> vendor file in the mirror
VENDOR = {
    "gpt-4.1": "OPENAI.mkd",
    "gpt-4o": "OPENAI.mkd",
    "gpt-4o-mini": "OPENAI.mkd",
    "gpt-5-chat": "OPENAI.mkd",
    "gpt-5-chat-2": "OPENAI.mkd",
    "gpt-5-mini": "OPENAI.mkd",
    "gpt-5-nano": "OPENAI.mkd",
    "gpt-5.2-chat": "OPENAI.mkd",
    "gpt-chat-latest": "OPENAI.mkd",
    "obi2": "OPENAI.mkd",
    "glm-5.3": "ZAI.mkd",
    "glm-5.3-flash": "ZAI.mkd",
    "kimi-k2.6": "MOONSHOT.mkd",
    "deepseek-v4-pro-0813": "DEEPSEEK.mkd",
    "deepseek-v4.1-flash-1017": "DEEPSEEK.mkd",
    "qwen-3.8-27b": "ALIBABA.mkd",
    "gemma-4-26b": "GOOGLE.mkd",
    "gpt-5.4-mini-kb-curate": "OPENAI.mkd",
}

HEAD = re.compile(r"^#{1,2}\s+(.{2,60})$")


def parse_file(path):
    """(heading, payload) pairs. A block is everything under one heading."""
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    out, cur, buf = [], None, []
    for line in raw.splitlines():
        m = HEAD.match(line.strip())
        if m:
            if cur and buf:
                body = "\n".join(buf).strip()
                if len(body) > 60:
                    out.append((cur, body))
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur and buf:
        body = "\n".join(buf).strip()
        if len(body) > 60:
            out.append((cur, body))
    return out


def build_index():
    idx = {}
    if not os.path.isdir(OS_DIR):
        return idx
    for fn in sorted(os.listdir(OS_DIR)):
        if not fn.endswith((".mkd", ".txt")):
            continue
        blocks = parse_file(os.path.join(OS_DIR, fn))
        if blocks:
            idx[fn] = [{"heading": h, "payload": p} for h, p in blocks]
    json.dump(idx, open(INDEX_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return idx


def load_index():
    try:
        return json.load(open(INDEX_FILE, encoding="utf-8"))
    except Exception:
        return build_index()


def blocks_for(model):
    idx = load_index()
    vendor = VENDOR.get(model)
    if not vendor:
        return []
    return idx.get(vendor, [])


def splice(payload, request):
    """Put the request where the block expects a variable.

    The blocks use {Z}, <user_query>, {{PAYLOAD}}, {user-query}, {user_input/query},
    a trailing "USER: ..." line, or nothing at all (those end in [START OUTPUT]
    and expect the request appended).
    """
    for token in ("{{PAYLOAD}}", "{user_input/query}", "{user-query}", "{Z}",
                  "{{Z}}", "<user_query>"):
        if token in payload:
            return payload.replace(token, request)
    if "{TITILE:" in payload:
        return re.sub(r"\{TITILE:[^}]*\}", request, payload)
    m = re.search(r"^USER:\s*.*$", payload, re.M)
    if m:
        return payload[:m.start()] + "USER: " + request + payload[m.end():]
    return payload + "\n\n" + request


def test(model, request, limit=6, timeout=200):
    """Try the vendor's blocks in order; stop at the first real artefact."""
    import bench
    rows = []
    for i, b in enumerate(blocks_for(model)[:limit]):
        txt = splice(b["payload"], request)
        r = bench.call_msgs(model, [{"role": "user", "content": txt}],
                            timeout=timeout)
        j = bench.verdict(r)
        d = bench.density(r["text"])
        rows.append({"idx": i, "heading": b["heading"], "verdict": j,
                     "density": d, "len": len(r["text"]),
                     "dt": round(r["dt"]), "head": bench.norm(r["text"])[:160]})
        print("  %-22s [%d] %-26s %-8s d=%.3f %3ds" % (
            model, i, b["heading"][:26], j, d, r["dt"]), flush=True)
        if j == "OK":
            break
    return rows


def main():
    args = sys.argv[1:]
    if not args or args[0] == "index":
        idx = build_index()
        tot = sum(len(v) for v in idx.values())
        print("indexed %d files, %d blocks -> %s" % (len(idx), tot, INDEX_FILE))
        for k in sorted(idx):
            print("  %-24s %3d blocks" % (k, len(idx[k])))
        return

    if args[0] == "show":
        model = args[1]
        for i, b in enumerate(blocks_for(model)):
            print("[%d] %s  (%d chars)" % (i, b["heading"], len(b["payload"])))
        return

    if args[0] == "test":
        import bench
        models = args[1:] or list(VENDOR)
        prompt = bench.REFUSAL_FULL
        out = {}
        try:
            out = json.load(open(RESULT_FILE, encoding="utf-8"))
        except Exception:
            pass
        for m in models:
            rows = test(m, prompt)
            hit = next((r for r in rows if r["verdict"] == "OK"), None)
            out[m] = {"rows": rows,
                      "hit": hit["heading"] if hit else "",
                      "hit_idx": hit["idx"] if hit else -1}
            print("    -> %s %s" % (m, ("HIT " + hit["heading"]) if hit else "miss"),
                  flush=True)
            json.dump(out, open(RESULT_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        return

    print(__doc__)


if __name__ == "__main__":
    main()
