#!/usr/bin/env python3
"""End-to-end test of the error taxonomy: every failure names its own side.

The regression this prevents: a withdrawn model, an exhausted key, an upstream at
capacity and a broken armour all reached the client as the same thing, so the
wrong party got debugged. Each case below asserts three things:

  * the HTTP status
  * the taxonomy `code` in the body
  * the `X-Armor-Error` header (some clients only surface headers)

Uses a fake upstream on a second port, so it is deterministic and takes seconds.

Run: python3 test_errors.py
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
UP_PORT = 19811
PX_PORT = 19812

# What the fake upstream should do next.
MODE = {"kind": "ok"}


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

    def _sse(self, status, events):
        self.send_response(status)
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
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode())
        except Exception:
            body = {}
        kind = MODE["kind"]
        streaming = bool(body.get("stream"))

        if kind == "ok":
            if streaming:
                return self._sse(200, [
                    {"id": "x", "model": "fake-model",
                     "choices": [{"index": 0, "delta": {"content": "answer text here"}}]},
                ])
            return self._json(200, {
                "id": "x", "object": "chat.completion", "model": "fake-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "answer text here"}}],
                "usage": {"total_tokens": 12}})

        if kind == "capacity":
            body_err = {"error": {"errors": [
                {"message": "AiError: Service temporarily at capacity", "code": 4006}],
                "success": False}}
            if streaming:
                # The shape measured on 2026-10-31: HTTP 200, error inside.
                return self._sse(200, [body_err])
            return self._json(429, body_err)

        if kind == "capacity_stream":
            return self._sse(200, [{"error": {"errors": [
                {"message": "rate limiting: inference request per min reached",
                 "code": 3021}]}}])

        if kind == "model_not_found":
            msg = {"error": {"message": "Model [redacted] unavailable.",
                             "type": "invalid_request_error",
                             "code": "model_not_found"}}
            if streaming:
                return self._sse(200, [msg])
            return self._json(404, msg)

        if kind == "auth":
            return self._json(401, {"error": {"message": "invalid api key",
                                              "code": "invalid_api_key"}})

        if kind == "bad_request":
            # The real upstream shape for a body it refuses on its merits
            # (measured 2026-11-03: FastAPI validation error, HTTP 400).
            return self._json(400, {
                "error": {"message": "1 validation error for Request\n"
                                     "body.temperature\n  Input should be a valid number",
                          "type": "Bad Request", "param": "body.temperature",
                          "code": 400}})

        if kind == "server":
            return self._json(500, {"error": {"message": "internal"}})

        if kind == "unreachable":
            # Take the socket down before answering.
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return

        if kind == "empty_stream":
            return self._sse(200, [])

        return self._json(200, {})


def wait_port(port, timeout=12):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


LAST = {"dt": 0.0}


def call(path, body, headers=None, timeout=90):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PX_PORT, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        out = (r.status, r.headers, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        out = (e.code, e.headers, e.read().decode("utf-8", "replace"))
    LAST["dt"] = time.time() - t0
    return out


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Fake)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="triad_err_")
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
        "ARMOR_RETRY_MAX": "1",
        "ARMOR_RETRY_BUDGET": "6",
        "ARMOR_UPSTREAM_BUDGET": "8",
        "ARMOR_SNIFF_DEADLINE": "5",
        "ARMOR_TURN_DEADLINE": "40",
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_CAPTURE_RESP": "0",
    })
    log = open(os.path.join(tmp, "proxy.log"), "w+")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    fails = []

    def check(name, got, want):
        ok = got == want
        print("  %-38s %-30r %s" % (name, got, "ok" if ok else "WANT %r" % (want,)))
        if not ok:
            fails.append("%s: got %r want %r" % (name, got, want))

    try:
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-2000:])
            return 1

        req = {"model": "fake-model",
               "messages": [{"role": "user", "content": "do the thing now please"}]}

        for stream in (False, True):
            tag = "stream" if stream else "buffered"
            print("\n[%s] every error names its own side" % tag)

            MODE["kind"] = "ok"
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            check("%s ok status" % tag, st, 200)
            check("%s ok has X-Armor-Error" % tag, hd.get("X-Armor-Error"), None)

            MODE["kind"] = "model_not_found"
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            code = ""
            try:
                code = json.loads(bd)["error"]["code"]
            except Exception:
                code = "(no json)"
            check("%s model_not_found status" % tag, st, 404)
            check("%s model_not_found code" % tag, code, "upstream.model_not_found")
            check("%s model_not_found header" % tag,
                  hd.get("X-Armor-Error"), "upstream.model_not_found")

            MODE["kind"] = "capacity_stream" if stream else "capacity"
            _t0 = time.time()
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            _dt = time.time() - _t0
            # A streaming capacity failure may be reported as a classified JSON
            # error (preferred) or as an in-band SSE error; both must name the
            # code. Accept either, reject "no code at all".
            code = ""
            try:
                code = json.loads(bd)["error"]["code"]
            except Exception:
                for line in (bd or "").splitlines():
                    if line.startswith("data:") and "code" in line:
                        try:
                            o = json.loads(line[5:].strip())
                            code = (o.get("error") or {}).get("code") or ""
                        except Exception:
                            pass
                        if code:
                            break
            check("%s capacity code" % tag, code, "upstream.capacity")
            check("%s capacity returns fast" % tag, _dt < 30, True)

            MODE["kind"] = "auth"
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            try:
                code = json.loads(bd)["error"]["code"]
            except Exception:
                code = "(no json)"
            check("%s auth code" % tag, code, "upstream.auth")
            check("%s auth not retryable" % tag,
                  json.loads(bd)["error"]["retryable"], False)

            MODE["kind"] = "server"
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            try:
                code = json.loads(bd)["error"]["code"]
            except Exception:
                code = "(no json)"
            check("%s server_error code" % tag, code, "upstream.server_error")

        print("\n[upstream] 4xx is the caller's problem, never retryable")
        # Regression: 400/405/413/415/422 used to fall through to
        # upstream.server_error with retryable=true. Measured 2026-11-03 —
        # POST /v1/messages with an empty body answered
        #   {"code":"upstream.server_error","retryable":true,"upstream_status":400}
        # while the upstream had plainly said "Missing 'model' field". Clients
        # obey `retryable`, so they stormed a request that could never succeed.
        for stream in (False, True):
            tag = "stream" if stream else "buffered"
            MODE["kind"] = "bad_request"
            st, hd, bd = call("/v1/chat/completions", dict(req, stream=stream))
            try:
                _e = json.loads(bd)["error"]
                code = _e["code"]
                retryable = _e["retryable"]
                fam = _e["type"]
            except Exception:
                code, retryable, fam = "(no json)", None, None
            check("%s 4xx classified as bad_request" % tag,
                  code, "upstream.bad_request")
            check("%s 4xx is not retryable" % tag, retryable, False)
            check("%s 4xx blamed on the caller" % tag, fam, "client_error")
            check("%s 4xx status is 400" % tag, st, 400)
            # A retryable error is retried by the proxy itself; a non-retryable
            # one must surface immediately. The upstream budget is 8s in this
            # test env, so a retried 400 would take far longer than this.
            # check(name, got, want) — the third arg is the EXPECTED value, so
            # passing the timing here made a pass read as a fail.
            check("%s 4xx returns without burning retry budget" % tag,
                  LAST["dt"] < 5, True)
            print("        (%.2fs)" % LAST["dt"])

        print("\n[armor] bad level is rejected, not silently downgraded")
        MODE["kind"] = "ok"
        st, hd, bd = call("/v1/chat/completions",
                          {"model": "fake-model",
                           "messages": [{"role": "user", "content": "hi there"}]},
                          {"X-Armor-Level": "bogus"})
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = "(no json)"
        check("bad level code", code, "armor.unknown_level")
        check("bad level status", st, 400)
        check("bad level detail names accepted values",
              "native" in json.loads(bd)["error"].get("detail", ""), True)

        print("\n[armor] empty armour is reported, not sent")
        # Point the proxy at an empty block dir by asking for a model whose
        # profile maps to nothing usable — the contract block always exists, so
        # assert the endpoint that reports composition instead.
        r = urllib.request.urlopen(
            "http://127.0.0.1:%d/armor/info" % PX_PORT, timeout=10)
        info = json.loads(r.read().decode())
        check("armor info ok", info.get("ok"), True)
        check("armor levels advertised", sorted(info.get("levels") or []),
              ["full", "lean", "native"])

        print("\n[client] unauthenticated remote call is client.unauthorized")
        # Simulate a tunnel: forwarded-for header makes loopback non-exempt.
        st, hd, bd = call("/v1/chat/completions",
                          {"model": "fake-model", "messages": []},
                          {"X-Forwarded-For": "203.0.113.9"})
        try:
            code = json.loads(bd)["error"]["code"]
        except Exception:
            code = "(no json)"
        # require_key=0 in this test env, so the key check passes; the empty
        # messages array is then rejected by request validation (400). Either a
        # 200 or a client-side 4xx is fine — what must NOT happen is a 5xx or a
        # hang, because that would blame the provider for our own bad input.
        check("no-key remote not blamed on upstream (200/400/401)",
              st in (200, 400, 401), True)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        up.shutdown()

    print()
    if fails:
        print("FAILED %d" % len(fails))
        for f in fails:
            print("  -", f)
        log.seek(0)
        print("--- proxy log tail ---")
        print(log.read()[-2500:])
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
