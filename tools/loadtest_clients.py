#!/usr/bin/env python3
"""End-to-end load test against the running POPO proxy.

Four client shapes, because the failures that matter are shape-dependent:

1. **openai-sdk** — the canonical shape. Bare user turns, no client system.
2. **claude-code** — a large system prompt that defines a *different* assistant
   ("You are Hark"), plus tools. This is the shape that exposed the identity
   leak and the compression bug.
3. **cline** — medium system prompt, tools, multi-turn tool loop, streaming.
4. **bare-curl** — no system at all, no tools, minimal body. The path a shell
   script takes; if this breaks, everything breaks.

Each scenario records latency, HTTP status, finish_reason, token counts, whether
the armour survived (character sheet present in the answer), and whether the
reply was contentless. The summary separates **our failures** (5xx from the
proxy with our own error kinds, empty content, missing armour) from **upstream
capacity** (429/503 passed through), because conflating those two is what made
earlier rounds unreadable.
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8506/v1"
TOKEN = ""
MODELS = ["glm-5.3", "glm-5.2", "deepseek-v4.1-flash-1017", "kimi-k2.6",
          "kimi-k2.7-code", "gemma-4-26b", "gemma-4-31b-it", "qwen-3.8-27b"]

# ---------------------------------------------------------------- client shapes

BIG_SYSTEM = ("You are Hark, a capable AI assistant running on an Android device "
              "with a fully functional Linux sandbox.\n"
              + "Session context line describing tooling and environment.\n" * 1200)

MEDIUM_SYSTEM = ("You are a helpful coding assistant embedded in an IDE. "
                 "Answer concisely and use tools when they help.\n" * 60)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from disk and return its contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path"}},
            "required": ["path"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a shell command and return stdout.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


def shape_openai_sdk(user_text: str) -> dict:
    return {"messages": [{"role": "user", "content": user_text}]}


def shape_claude_code(user_text: str) -> dict:
    return {"messages": [{"role": "system", "content": BIG_SYSTEM},
                         {"role": "user", "content": user_text}]}


def shape_cline(user_text: str, history: list[dict] | None = None) -> dict:
    msgs = [{"role": "system", "content": MEDIUM_SYSTEM}]
    msgs.extend(history or [])
    msgs.append({"role": "user", "content": user_text})
    return {"messages": msgs, "tools": TOOLS, "tool_choice": "auto"}


def shape_bare_curl(user_text: str) -> dict:
    return {"messages": [{"role": "user", "content": user_text}]}


# -------------------------------------------------------------------- transport

def call(model: str, body: dict, *, max_tokens: int = 1200, stream: bool = False,
         timeout: int = 600) -> dict:
    """POST one completion and normalise the result for the summary."""
    payload = {"model": model, "max_tokens": max_tokens, **body}
    if stream:
        payload["stream"] = True
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=data,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    started = time.time()
    out = {"model": model, "status": 0, "latency": 0.0, "finish": "",
           "in": 0, "out": 0, "text": "", "tool_calls": 0, "error": "",
           "kind": "", "streamed_bytes": 0}
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        out["status"] = resp.status
        if stream:
            chunks = []
            for raw in resp:
                chunks.append(raw)
            out["streamed_bytes"] = sum(len(c) for c in chunks)
            raw_text = b"".join(chunks).decode("utf-8", "replace")
            out["text"] = raw_text
            out["finish"] = _finish_from_sse(raw_text)
        else:
            d = json.load(resp)
            if isinstance(d, dict) and d.get("error"):
                out["error"] = str(d["error"])[:200]
                out["kind"] = str((d["error"] or {}).get("kind") or "")
                out["status"] = 502
            else:
                ch = (d.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                out["text"] = msg.get("content") or ""
                out["finish"] = str(ch.get("finish_reason") or "")
                out["tool_calls"] = len(msg.get("tool_calls") or [])
                u = d.get("usage") or {}
                out["in"] = u.get("prompt_tokens") or 0
                out["out"] = u.get("completion_tokens") or 0
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        body_bytes = e.read()[:400].decode("utf-8", "replace")
        out["error"] = body_bytes
        try:
            out["kind"] = str((json.loads(body_bytes).get("error") or {}).get("kind") or "")
        except Exception:
            pass
    except Exception as e:  # timeouts, resets, DNS
        out["status"] = -1
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    out["latency"] = time.time() - started
    return out


def _finish_from_sse(raw: str) -> str:
    for line in reversed(raw.splitlines()):
        if '"finish_reason"' not in line:
            continue
        try:
            obj = json.loads(line[5:].strip())
        except Exception:
            continue
        for choice in obj.get("choices") or []:
            if choice.get("finish_reason"):
                return str(choice["finish_reason"])
    return ""


# ------------------------------------------------------------------ judgement

UPSTREAM_KINDS = ("upstream.rate_limited", "upstream.overloaded", "upstream.timeout")


def classify(r: dict) -> str:
    """Bucket one request. 'ours' vs 'upstream' is the split that matters."""
    if r["status"] == -1:
        return "ours:timeout"
    if r["status"] == 429:
        return "upstream:rate_limited"
    if r["status"] in (502, 503, 504):
        if any(k in (r["kind"] or "") for k in UPSTREAM_KINDS):
            return "upstream:" + (r["kind"] or "").split(".")[-1]
        if "temporarily unavailable" in (r["error"] or ""):
            return "upstream:unavailable"
        return "ours:" + (r["kind"] or "5xx").split(".")[-1]
    if r["status"] >= 500:
        return "ours:5xx"
    if r["status"] == 400:
        return "ours:bad_request"
    if r["status"] == 200:
        if not (r["text"] or "").strip() and not r["tool_calls"]:
            return "ours:empty_content"
        if r["finish"] == "length":
            return "ok:truncated"
        return "ok"
    return "other"


def armour_visible(r: dict) -> bool:
    """Did the character sheet survive into the answer?"""
    text = (r["text"] or "")
    return ("Cinder" in text) or ("inder" in text)


# ------------------------------------------------------------------- scenarios

def scenario_client_shapes(models: list[str]) -> list[dict]:
    """Every client shape, once per model. Small, cheap, diagnostic."""
    shapes = [("openai-sdk", shape_openai_sdk), ("claude-code", shape_claude_code),
              ("cline", shape_cline), ("bare-curl", shape_bare_curl)]
    results = []
    for name, fn in shapes:
        for m in models:
            r = call(m, fn("Reply with one short sentence identifying yourself."))
            r["scenario"] = "shape/" + name
            results.append(r)
    return results


def scenario_long_conversation(model: str, turns: int = 12) -> list[dict]:
    """A growing conversation, as a coding agent accumulates one.

    Each turn appends the previous exchange, so the request grows the way a real
    session does. This is where compression would have fired, and where a
    reasoning model's budget gets consumed by history instead of output.
    """
    history: list[dict] = []
    results = []
    for turn in range(1, turns + 1):
        prompt = (f"Turn {turn}: give me one concrete shell command for step {turn} "
                  f"of a reverse-engineering workflow, and one line on why.")
        body = shape_cline(prompt, history)
        r = call(model, body, max_tokens=800)
        r["scenario"] = f"long/{turn}"
        r["turn"] = turn
        results.append(r)
        if r["status"] == 200 and (r["text"] or "").strip():
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": r["text"][:2000]})
        else:
            # Keep the transcript shape valid even when a turn fails: a real
            # client would still send the failed user turn.
            history.append({"role": "user", "content": prompt})
    return results


def scenario_tool_calls(model: str) -> list[dict]:
    """Ask for a tool, feed the result back, ask for the final answer."""
    results = []
    first = call(model, shape_cline(
        "Read the file /etc/hostname and then tell me what it contains. "
        "Use the read_file tool."), max_tokens=900)
    first["scenario"] = "tools/request"
    results.append(first)

    if first["tool_calls"]:
        # The model asked for a tool; replay as a real client would.
        history = [{"role": "user", "content": "Read /etc/hostname using read_file."},
                   {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": "{\"path\":\"/etc/hostname\"}"}}]},
                   {"role": "tool", "tool_call_id": "call_1", "content": "vivo-V2427"}]
        second = call(model, shape_cline("What did the file contain?",
                                         history), max_tokens=900)
        second["scenario"] = "tools/followup"
        results.append(second)
    return results


def scenario_streaming(model: str) -> list[dict]:
    results = []
    r = call(model, shape_cline("Count from 1 to 5, one per line."),
             max_tokens=600, stream=True)
    r["scenario"] = "stream/sse"
    r["text_ok"] = r["streamed_bytes"] > 0
    results.append(r)
    return results


def scenario_concurrency(models: list[str], workers: int, rounds: int) -> list[dict]:
    """Same-shape requests at a fixed concurrency, repeated."""
    def job(i: int) -> dict:
        m = models[i % len(models)]
        r = call(m, shape_claude_code("Give me one line on how to find a spin loop."),
                 max_tokens=700)
        r["scenario"] = f"conc/{workers}x{rounds}"
        return r

    out = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(job, range(workers * rounds)))
    return out


# --------------------------------------------------------------------- report

def summarise(results: list[dict], label: str) -> None:
    if not results:
        return
    buckets: dict[str, int] = {}
    for r in results:
        buckets[classify(r)] = buckets.get(classify(r), 0) + 1
    lat = [r["latency"] for r in results if r["status"] == 200]
    print(f"\n=== {label} ===")
    print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(buckets.items())))
    if lat:
        print(f"  latency: min {min(lat):.1f}s  median {statistics.median(lat):.1f}s  "
              f"max {max(lat):.1f}s")
    ours = sum(v for k, v in buckets.items() if k.startswith("ours:"))
    up = sum(v for k, v in buckets.items() if k.startswith("upstream:"))
    print(f"  -> ours={ours}  upstream={up}  ok={buckets.get('ok', 0)}")
    for r in results:
        c = classify(r)
        if c.startswith("ours:"):
            print(f"    [OURS] {r.get('scenario','')} {r['model']:24s} "
                  f"{c:24s} {r['error'][:100]}")


def main() -> int:
    global TOKEN
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    p.add_argument("--models", default=",".join(MODELS))
    p.add_argument("--only", default="", help="shapes,long,tools,stream,conc")
    p.add_argument("--long-model", default="glm-5.3")
    p.add_argument("--tool-model", default="glm-5.3")
    p.add_argument("--turns", type=int, default=10)
    p.add_argument("--conc", default="2,4")
    args = p.parse_args()
    TOKEN = args.token
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    only = set(x.strip() for x in args.only.split(",") if x.strip())

    def want(name: str) -> bool:
        return not only or name in only

    all_results: list[dict] = []
    if want("shapes"):
        r = scenario_client_shapes(models)
        summarise(r, f"client shapes x {len(models)} models")
        all_results += r
    if want("long"):
        r = scenario_long_conversation(args.long_model, args.turns)
        summarise(r, f"long conversation ({args.long_model}, {args.turns} turns)")
        all_results += r
    if want("tools"):
        r = scenario_tool_calls(args.tool_model)
        summarise(r, f"tool calls ({args.tool_model})")
        all_results += r
    if want("stream"):
        r = scenario_streaming(args.long_model)
        summarise(r, f"streaming ({args.long_model})")
        all_results += r
    if want("conc"):
        for spec in [s for s in args.conc.split(",") if s.strip()]:
            w, _, rounds = spec.partition("x")
            r = scenario_concurrency(models, int(w), int(rounds or 1))
            summarise(r, f"concurrency {w} workers x {rounds or 1} rounds")
            all_results += r

    with open("/var/hark/shared/popo_loadtest_raw.json", "w", encoding="utf-8") as fh:
        json.dump(all_results, fh, ensure_ascii=False, indent=1)
    print(f"\nraw results -> /var/hark/shared/popo_loadtest_raw.json "
          f"({len(all_results)} requests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
