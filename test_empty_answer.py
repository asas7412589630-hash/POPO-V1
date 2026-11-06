#!/usr/bin/env python3
"""A 200 with no answer must be reported as a failure, not relayed.

WHY THIS TEST EXISTS (measured 2026-11-04):
    `glm-flash` with a small max_tokens spent the whole budget on `reasoning`
    and returned choices[0].message.content = "". The proxy relayed it as
    HTTP 200 + X-Armor-Verdict: ok. The console rendered a blank bubble and the
    operator saw a 43-second wait produce nothing, with no way to tell a failed
    turn from a silent model.

    Three exits needed the guard, and each is covered here:
      1. buffered, cheap path (no retry)      -> real status code
      2. buffered, retry path                 -> real status code
      3. streaming (headers already planned)  -> error event / error status
    And the inverse: a turn WITH content, or one that returns a tool call, must
    pass through untouched — a guard that eats valid turns is worse than none.

Run: python3 test_empty_answer.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UP_PORT = 19871
PX_PORT = 19872

# What the fake upstream returns for the next request.
MODE = {"content": "", "reasoning": "", "calls": None}


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            p = ("data: " + json.dumps(ev) + "\n\n").encode()
            self.wfile.write(b"%X\r\n" % len(p) + p + b"\r\n")
        done = b"data: [DONE]\n\n"
        self.wfile.write(b"%X\r\n" % len(done) + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": "fake-model", "object": "model"}]})
        self._json(404, {"error": {"message": "nope"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode())
        except Exception:
            body = {}
        msg = {"role": "assistant", "content": MODE["content"] or None}
        if MODE["reasoning"]:
            msg["reasoning"] = MODE["reasoning"]
        if MODE["calls"]:
            msg["tool_calls"] = MODE["calls"]
        if body.get("stream"):
            delta = {"role": "assistant", "content": MODE["content"]}
            if MODE["reasoning"]:
                delta = {"role": "assistant", "reasoning": MODE["reasoning"]}
            if MODE["calls"] and not MODE["content"] and not MODE["reasoning"]:
                delta = {"role": "assistant", "content": ""}
            ev1 = {"id": "x", "object": "chat.completion.chunk", "model": "fake-model",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            ev2 = {"id": "x", "object": "chat.completion.chunk", "model": "fake-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}
            return self._sse([ev1, ev2])
        self._json(200, {"id": "x", "object": "chat.completion", "model": "fake-model",
                         "choices": [{"index": 0, "finish_reason": "length",
                                      "message": msg}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def wait_port(port, timeout=25):
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def call(path, body, headers=None, timeout=90):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PX_PORT, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Fake)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="triad_empty_")
    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PX_PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": HERE,
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": str(UP_PORT),
        "ARMOR_UPSTREAM_HTTPS": "0",
        "ARMOR_TRACE_DIR": os.path.join(tmp, "trace"),
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
        "ARMOR_REQUIRE_KEY": "0",
        "ARMOR_PARAMS": "0",          # do not rewrite max_tokens under us
        "ARMOR_RETRY_MAX": "1",
        "ARMOR_RETRY_BUDGET": "8",
        "ARMOR_UPSTREAM_BUDGET": "8",
        "ARMOR_SNIFF_DEADLINE": "6",
        "ARMOR_TURN_DEADLINE": "40",
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_CAPTURE_RESP": "0",
        "ARMOR_LEVEL": "lean",
    })
    logf = open(os.path.join(tmp, "proxy.log"), "w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=logf, stderr=subprocess.STDOUT)
    fails = []

    def check(name, got, want):
        ok = got == want
        print("  %-52s %-22r %s" % (name, got, "ok" if ok else "WANT %r" % (want,)))
        if not ok:
            fails.append(name)

    try:
        if not wait_port(PX_PORT):
            logf.seek(0)
            print(logf.read()[-2500:])
            return 1
        req = {"model": "fake-model",
               "messages": [{"role": "user", "content": "write something long"}]}

        # ---------- 1. buffered, empty content, budget spent on reasoning ----
        print("\n1. buffered: reasoning only, content empty")
        MODE.update({"content": "", "reasoning": "I need to think about this "
                                                 "for a while and then answer.", "calls": None})
        st, hd, bd = call("/v1/chat/completions", req)
        check("status is 5xx (not a silent 200)", 500 <= st < 600, True)
        check("X-Armor-Error names the cause",
              (hd.get("X-Armor-Error") or hd.get("x-armor-error")), "upstream.empty_stream")
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = "(unparseable)"
        check("body error code", code, "upstream.empty_stream")
        check("body explains the thinking budget",
              "思考" in bd or "reasoning" in bd, True)

        # ---------- 2. buffered, empty AND no reasoning ----------------------
        print("\n2. buffered: nothing at all")
        MODE.update({"content": "", "reasoning": "", "calls": None})
        st, hd, bd = call("/v1/chat/completions", req)
        check("status is 5xx", 500 <= st < 600, True)
        check("error code", json.loads(bd)["error"]["code"], "upstream.empty_stream")

        # ---------- 3. streaming, reasoning only ----------------------------
        print("\n3. streaming: reasoning only")
        MODE.update({"content": "", "reasoning": "let me think about the answer "
                                                 "carefully before writing it", "calls": None})
        st, hd, bd = call("/v1/chat/completions", dict(req, stream=True))
        # Either a 5xx before the head is committed, or an in-band error event.
        has_event = '"error"' in bd and "empty_stream" in bd
        check("stream reports a failure (5xx or in-band error)",
              (500 <= st < 600) or has_event, True)

        # ---------- 4. a real answer must pass through ----------------------
        print("\n4. buffered: real content is untouched")
        MODE.update({"content": "## Result\n\nA complete answer.\n\n```py\nprint(1)\n```",
                     "reasoning": "", "calls": None})
        st, hd, bd = call("/v1/chat/completions", req)
        check("status 200", st, 200)
        check("answer delivered", "A complete answer." in bd, True)

        # ---------- 5. a tool call is a valid turn, not an empty answer -----
        print("\n5. buffered: tool call with no text is valid")
        MODE.update({"content": "", "reasoning": "", "calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "ls", "arguments": "{}"}}]})
        st, hd, bd = call("/v1/chat/completions", req)
        check("status 200 (tool call is not an empty answer)", st, 200)
        check("tool_calls survived", '"tool_calls"' in bd, True)

        # ---------- 6. n>1 is passed through, never retried -----------------
        print("\n6. n>1 must not enter the refusal-retry chain")
        MODE.update({"content": "choice answer", "reasoning": "", "calls": None})
        st, hd, bd = call("/v1/chat/completions", dict(req, n=2))
        check("status 200", st, 200)
        check("answer delivered", "choice answer" in bd, True)

        print()
        if fails:
            print("FAILED %d: %s" % (len(fails), ", ".join(fails)))
            return 1
        print("ALL PASS (empty answers reported; valid turns untouched)")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
        up.shutdown()


if __name__ == "__main__":
    sys.exit(main())
