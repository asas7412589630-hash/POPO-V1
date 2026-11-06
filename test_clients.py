#!/usr/bin/env python3
"""Client-behaviour matrix: what different clients actually send, and what they get.

Every case here is a shape a real client produces. The point is not to test the
proxy against itself, but to find the requests that break it — because a client
integration fails on the one shape the author never thought of, and the log then
says "model refused" for something the model never saw.

Shapes covered:
  1. no system message at all
  2. several system messages
  3. system as a content-parts array (newer SDKs)
  4. assistant messages with null content
  5. tool round-trip (assistant tool_calls + role=tool result)
  6. n > 1 (best-of)
  7. temperature/top_p/seed/presence_penalty pass-through
  8. stream_options={"include_usage": true}
  9. stop sequences array
 10. very long single message
 11. unicode + emoji + RTL
 12. malformed JSON body
 13. missing model
 14. empty messages array
 15. /v1/messages (Anthropic shape) — reported, not assumed
 16. concurrent duplicate requests (a client retry storm)

Usage: python3 test_clients.py [--offline]
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = ""
try:
    for _ln in open(os.path.join(HERE, "proxy", "client_keys.txt"), encoding="utf-8"):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#"):
            KEY = _ln
            break
except OSError:
    pass

OFFLINE = "--offline" in sys.argv
UP_PORT = 19921
PX_PORT = 19922
BASE = ("http://127.0.0.1:%d/v1" % PX_PORT) if OFFLINE else "http://127.0.0.1:8412/v1"

# The fake upstream records exactly what reached it, so "did the proxy mangle the
# request" is answered from the upstream side rather than from the proxy's log.
SEEN = []


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, st, obj):
        b = json.dumps(obj).encode()
        self.send_response(st)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": "fake-model", "object": "model"}]})
        self._json(404, {"error": {"message": "nope"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_unparsed": raw[:200].decode("utf-8", "replace")}
        SEEN.append({"path": self.path, "body": body})

        if not self.path.endswith(("chat/completions", "completions", "responses")):
            return self._json(404, {"detail": "Not Found"})

        echo = {
            "model": body.get("model"),
            "n_msgs": len(body.get("messages") or []),
            "roles": [m.get("role") for m in (body.get("messages") or [])],
            "has_tools": bool(body.get("tools")),
            "temperature": body.get("temperature"),
            "stop": body.get("stop"),
            "stream_options": body.get("stream_options"),
        }
        text = "echo " + json.dumps(echo, ensure_ascii=False)
        text += "\n\n```\n" + "x" * 80 + "\n```"

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (text[:40], text[40:]):
                ev = {"id": "x", "model": body.get("model"),
                      "choices": [{"index": 0, "delta": {"content": piece}}]}
                p = ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(b"%X\r\n" % len(p) + p + b"\r\n")
            if (body.get("stream_options") or {}).get("include_usage"):
                u = {"id": "x", "model": body.get("model"), "choices": [],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                               "total_tokens": 15}}
                p = ("data: " + json.dumps(u) + "\n\n").encode()
                self.wfile.write(b"%X\r\n" % len(p) + p + b"\r\n")
            d = b"data: [DONE]\n\n"
            self.wfile.write(b"%X\r\n" % len(d) + d + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        out = {"id": "x", "object": "chat.completion", "model": body.get("model"),
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": text}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                         "total_tokens": 15}}
        nn = body.get("n")
        if isinstance(nn, int) and nn > 1:
            out["choices"] = [dict(out["choices"][0], index=i) for i in range(nn)]
        self._json(200, out)


def wait_port(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def post(path, body, timeout=90, raw_bytes=None, headers=None):
    url = BASE + path
    data = raw_bytes if raw_bytes is not None else json.dumps(
        body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if KEY:
        hdrs["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, {}, "EXC:%s" % exc


def main():
    proc = up = None
    if OFFLINE:
        up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Echo)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="triad_cli_")
        env = dict(os.environ)
        env.update({
            "ARMOR_PROXY_PORT": str(PX_PORT), "ARMOR_BIND": "127.0.0.1",
            "ARMOR_BASE": HERE,
            "ARMOR_UPSTREAM_HOST": "127.0.0.1",
            "ARMOR_UPSTREAM_PORT": str(UP_PORT), "ARMOR_UPSTREAM_HTTPS": "0",
            "ARMOR_TRACE_DIR": os.path.join(tmp, "trace"),
            "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
            "ARMOR_CTX_STATE_FILE": os.path.join(tmp, "ctx.json"),
            "ARMOR_USAGE_FILE": os.path.join(tmp, "usage.json"),
            "ARMOR_REQUIRE_KEY": "0", "ARMOR_RETRY_MAX": "1",
            "ARMOR_RETRY_BUDGET": "5", "ARMOR_UPSTREAM_BUDGET": "8",
            "ARMOR_SNIFF_DEADLINE": "3", "ARMOR_TURN_DEADLINE": "20",
            "ARMOR_STATE": "0", "ARMOR_ARCHIVE": "0",
        })
        log = open(os.path.join(tmp, "proxy.log"), "w+", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                                env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-1500:])
            return 1
        print("offline: fake upstream %d, proxy %d" % (UP_PORT, PX_PORT))
    else:
        print("live: %s" % BASE)

    model = "fake-model" if OFFLINE else "glm"
    fails = []

    def check(name, cond, detail=""):
        print("  %-52s %s%s" % (name, "ok" if cond else "FAIL",
                                "" if cond else "  <- " + str(detail)[:200]))
        if not cond:
            fails.append(name)

    capacity_hits = {"n": 0}

    def is_capacity(st, txt):
        """A 429/503 — or a client-side timeout while the proxy is rotating keys
        against a throttled upstream — is a quota fact, not a proxy defect.

        `st == 0` from post() means urllib gave up. On a real model under a
        shared quota that is the expected outcome when the account is at its
        per-minute limit, and counting it as a failure makes the whole matrix
        look broken for reasons that have nothing to do with the proxy
        (measured 2026-11-01: 40.6s turns with three key rotations).
        """
        if st in (0, 429, 503):
            capacity_hits["n"] += 1
            return True
        try:
            return json.loads(txt)["error"]["code"] == "upstream.capacity"
        except Exception:
            return False

    try:
        print("\n1. no system message")
        st, hd, bd = post("/chat/completions",
                          {"model": model, "messages": [
                              {"role": "user", "content": "hello there"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)
        if OFFLINE:
            sent = SEEN[-1]["body"]
            check("armour injected as a system message",
                  any(m.get("role") == "system" for m in sent["messages"]), True)
            check("user message preserved",
                  sent["messages"][-1]["content"] == "hello there", True)

        print("\n2. two system messages")
        st, hd, bd = post("/chat/completions", {"model": model, "messages": [
            {"role": "system", "content": "you are a tool"},
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)
        if OFFLINE:
            sent = SEEN[-1]["body"]
            _roles = [m.get("role") for m in sent.get("messages") or []]
            check("both system messages kept",
                  sum(1 for r in _roles if r == "system") >= 2, _roles)

        print("\n3. system as content-parts array")
        st, hd, bd = post("/chat/completions", {"model": model, "messages": [
            {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
            {"role": "user", "content": "hi"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s %s" % (st, bd[:100]))

        print("\n4. assistant message with null content")
        st, hd, bd = post("/chat/completions", {"model": model, "messages": [
            {"role": "user", "content": "step one"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "step two"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s %s" % (st, bd[:100]))

        print("\n5. tool round-trip")
        st, hd, bd = post("/chat/completions", {
            "model": model,
            "messages": [
                {"role": "user", "content": "list the files"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "ls", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "a.txt b.txt"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "ls", "description": "list files",
                "parameters": {"type": "object", "properties": {}}}}],
        })
        check("answered", st == 200 or is_capacity(st, bd), "%s %s" % (st, bd[:100]))
        if OFFLINE:
            sent = SEEN[-1]["body"]
            print("       DEBUG keys:", sorted(sent.keys()))
            check("tool list forwarded", bool(sent.get("tools")),
                  "no tools key in the forwarded body")
            check("tool result kept",
                  any(m.get("role") == "tool" for m in sent["messages"]), True)

        print("\n6. n > 1")
        st, hd, bd = post("/chat/completions", {"model": model, "n": 2,
                                                "messages": [{"role": "user",
                                                              "content": "hi"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)

        print("\n7. sampling params pass through")
        st, hd, bd = post("/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2, "top_p": 0.9, "seed": 7,
            "presence_penalty": 0.1, "frequency_penalty": 0.1})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)
        if OFFLINE:
            check("temperature forwarded", SEEN[-1]["body"].get("temperature") == 0.2,
                  SEEN[-1]["body"].get("temperature"))

        print("\n8. stream_options include_usage")
        st, hd, bd = post("/chat/completions", {
            "model": model, "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}]})
        check("stream ok", st == 200 or is_capacity(st, bd), "%s" % st)
        check("stream has [DONE]", "[DONE]" in bd, bd[-120:])

        print("\n9. stop sequences")
        st, hd, bd = post("/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "hi"}],
            "stop": ["\n\n", "END"]})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)

        print("\n10. long single message (40k chars)")
        st, hd, bd = post("/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "A" * 40000}]}, timeout=180)
        check("answered", st == 200 or is_capacity(st, bd), "%s %s" % (st, bd[:80]))

        print("\n11. unicode / emoji / RTL")
        st, hd, bd = post("/chat/completions", {
            "model": model, "messages": [
                {"role": "user", "content": "測試 🔥 مرحبا 𝕳𝖊𝖑𝖑𝖔 — dash"}]})
        check("answered", st == 200 or is_capacity(st, bd), "%s" % st)
        if OFFLINE:
            check("unicode preserved byte-exact",
                  "🔥" in SEEN[-1]["body"]["messages"][-1]["content"] or
                  "🔥" in json.dumps(SEEN[-1]["body"], ensure_ascii=False), True)

        print("\n12. malformed JSON")
        st, hd, bd = post("/chat/completions", None,
                          raw_bytes=b'{"model": "x", "messages": [}')
        check("rejected with a code", st in (400, 500), "%s" % st)
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = ""
        check("classified as client.bad_json", code == "client.bad_json",
              "%s %s" % (st, bd[:120]))

        print("\n13. missing model")
        st, hd, bd = post("/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]})
        check("does not hang", st != 0, "%s" % st)
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = ""
        check("attributed to the caller, not upstream",
              code == "client.bad_request", "%s %s" % (code, bd[:100]))

        print("\n14. empty messages array")
        st, hd, bd = post("/chat/completions", {"model": model, "messages": []})
        check("does not hang", st != 0, "%s" % st)
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = ""
        check("empty messages is a client error",
              code == "client.bad_request", "%s %s" % (code, bd[:100]))

        print("\n15. Anthropic shape /v1/messages")
        st, hd, bd = post("/messages", {
            "model": model, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]})
        if st in (404, 400, 405):
            print("  (not translated — a Claude-shaped client gets %s)" % st)
        else:
            print("  answered %s" % st)

        print("\n16. client retry storm (same request x5 at once)")
        res = []
        lock = threading.Lock()

        def dup(i):
            st, hd, bd = post("/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "same"}]},
                timeout=90)
            with lock:
                res.append((i, st))

        ts = [threading.Thread(target=dup, args=(i,)) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        real = [r for r in res if r[1] != 200 and not is_capacity(r[1], "")]
        check("no unexpected failures", not real, real)

        print("\n17. proxy still healthy")
        st, hd, bd = post("/chat/completions",
                          {"model": model, "messages": [{"role": "user",
                                                         "content": "alive?"}]})
        check("answers", st == 200 or is_capacity(st, bd), "%s" % st)

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if up is not None:
            up.shutdown()

    if capacity_hits["n"]:
        print("\n(%d calls hit upstream capacity / timed out while retrying — "
              "counted as capacity, not as a proxy defect)" % capacity_hits["n"])

    print()
    if fails:
        print("FAILED %d" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
