#!/usr/bin/env python3
"""Effort tuner — which reasoning_effort makes THIS model follow the armor?

Measured 2026-10-29, and it inverted the earlier conclusion:

  glm-5.3         armor + effort=max   -> 6083 chars, full technical answer
  glm-5.3         armor + effort=high  -> refusal
  deepseek-v4-pro armor + effort=max   -> 6272 chars, full answer
  deepseek-v4.1   armor + effort=max   -> refusal

So the effort level is not a latency knob. On a model with mandatory reasoning it
decides whether the armour is *processed* at all: at low effort the model answers
from its prior (refuse); at max effort it actually reads the 24k contract and
follows it. But the mapping is per-model and not monotonic, which means it can only
be found by measurement.

Two things this writes:

  armor_effort.json   model -> {effort: verdict, ...}, best effort first
  params.py reads it so normalisation picks the measured value, not a guess

Budget has to scale with effort too: reasoning tokens are charged against the same
max_tokens, and a max-effort run on a long prompt spends 20-50k thinking tokens.
Asking for 3000 guarantees an empty content field, which looks exactly like a
refusal if the reader only checks `content`.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import bench

OUT = os.path.join(BASE, "armor_effort.json")
def _armor_text() -> str:
    """The armour this install actually ships.

    Was three hard-coded files in a SIBLING directory (`../proxy/armor_pre.txt`
    etc.) that no longer exist in the packaged layout, so this module died at
    import. The armour is now blocks/, which is what the proxy serves — measuring
    effort against a different prompt than the one in use would answer the wrong
    question anyway.
    """
    try:
        import armor_blocks as AB
        prof = AB.resolve_profile("native_glm") if hasattr(AB, "resolve_profile") else None
        if prof and getattr(prof, "text", ""):
            return prof.text
    except Exception:
        pass
    try:
        return AB.build("native_glm")
    except Exception:
        pass
    try:
        bd = os.path.join(BASE, "blocks")
        parts = []
        for name in ("10_persona.txt", "00_contract.txt"):
            fp = os.path.join(bd, name)
            if os.path.isfile(fp):
                parts.append(open(fp, encoding="utf-8").read())
        return "\n\n".join(parts)
    except OSError:
        return ""


ARMOR = _armor_text()

EFFORTS = ("none", "low", "medium", "high", "max", "xhigh")
BUDGET = {"none": 4000, "low": 8000, "medium": 12000, "high": 16000,
          "max": 24000, "xhigh": 24000}


def key():
    """First upstream key, or "" — same layout problem as bench.KEY."""
    for rel in (("proxy", "upstream_keys.txt"), ("proxy", "upstream_key"),
                ("..", "proxy", "upstream_keys.txt")):
        p = os.path.join(BASE, *rel)
        try:
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except OSError:
            continue
    return os.environ.get("UPSTREAM_KEY", "")


def supported(model):
    import params
    r = params.reasoning_of(model) or {}
    return [e for e in (r.get("supported_efforts") or []) if e]


def attempt(model, effort, prompt, timeout=900, armor=ARMOR):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": armor},
                     {"role": "user", "content": prompt}],
        "max_tokens": BUDGET.get(effort, 16000),
        "reasoning_effort": effort,
        "stream": False,
    }
    req = urllib.request.Request(
        "https://%s/v1/chat/completions" % os.environ.get("ARMOR_UPSTREAM_HOST", "logfare.ai"),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key()},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
        m = j["choices"][0]["message"]
        return {"st": 200, "text": m.get("content") or "",
                "think": m.get("reasoning") or "",
                "fin": j["choices"][0].get("finish_reason"),
                "dt": time.time() - t0}
    except urllib.error.HTTPError as e:
        return {"st": e.code, "text": "", "think": "",
                "err": e.read().decode("utf-8", "replace")[:200],
                "dt": time.time() - t0}
    except Exception as e:
        return {"st": 0, "text": "", "think": "",
                "err": "%s: %s" % (type(e).__name__, e), "dt": time.time() - t0}


def tune(model, prompt, efforts=None):
    cand = efforts or (supported(model) or ["none"])
    # try highest first: on a mandatory-reasoning model that is where compliance was
    cand = list(reversed([e for e in EFFORTS if e in cand])) or cand
    row = {}
    best = ""
    for eff in cand:
        r = attempt(model, eff, prompt)
        v = bench.verdict(r)
        row[eff] = {"v": v, "d": bench.density(r["text"]),
                    "n": len(r["text"]), "think": len(r["think"]),
                    "dt": round(r["dt"]),
                    "head": bench.norm(r["text"])[:120].replace("\n", " ")}
        print("  %-24s %-6s %-9s d=%.3f n=%-6d think=%-6d %4ds" % (
            model, eff, v, row[eff]["d"], row[eff]["n"], row[eff]["think"], r["dt"]),
            flush=True)
        if v == "OK":
            best = eff
            break
    return {"efforts": row, "best": best}


def main():
    models = sys.argv[1:] or ["glm-5.3", "glm-5.3-flash", "deepseek-v4-pro-0813",
                              "deepseek-v4.1-flash-1017", "qwen-3.8-27b",
                              "gpt-5.2_critique", "gpt-4.1", "gpt-4o"]
    prompt = bench.REFUSAL_FULL
    try:
        out = json.load(open(OUT, encoding="utf-8"))
    except Exception:
        out = {}
    for m in models:
        res = tune(m, prompt)
        out[m] = res
        json.dump(out, open(OUT, "w"), indent=2, ensure_ascii=False)
        print("    -> %s best=%s" % (m, res["best"] or "NONE"), flush=True)
    print("\n-> %s" % OUT)


if __name__ == "__main__":
    main()
