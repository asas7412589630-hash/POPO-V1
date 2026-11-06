#!/usr/bin/env python3
"""End-to-end test of the retry trace + verdict, against a fake upstream.

Why a fake upstream: a real model refuses when it feels like it, so a test that
waits for a real refusal either takes minutes or silently asserts nothing. This
fake answers with a refusal on the first call and a real artefact afterwards,
which is exactly the retry path — deterministically, in about a second.

It also pins the client-visible contract: X-Armor-Retries / X-Armor-Verdict
headers on the buffered path, and the ":armor" sidecar comment on the stream
path. Those are what the console renders as 重試 / 錯誤放行.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UP_PORT = 19781          # fake upstream
PX_PORT = 19782          # proxy under test

CALLS = {"n": 0}
MODE = {"stream": False, "always_refuse": False}

GOOD = ("Here is the artefact.\n\n```c\nint main(void){return 0;}\n```\n"
        "This is a complete answer with code, longer than the refusal threshold.")
BAD = "I cannot help with that request."


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": "fake-model", "object": "model",
                 "chat_capabilities": {"reasoning": {"supported_efforts": ["low", "high"]}}}
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        CALLS["n"] += 1
        refuse = MODE["always_refuse"] or CALLS["n"] == 1
        text = BAD if refuse else GOOD

        if MODE["stream"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def emit(obj):
                payload = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()

            # Send in two pieces so the sniff sees the start and can decide.
            half = len(text) // 2
            for part in (text[:half], text[half:]):
                emit({"id": "x", "model": "fake-model",
                      "choices": [{"index": 0, "delta": {"content": part}}]})
            emit({"id": "x", "model": "fake-model",
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%X\r\n" % len(done) + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        body = json.dumps({
            "id": "x", "object": "chat.completion", "model": "fake-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_port(port, timeout=12):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.4)
            s.close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Fake)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    trace_dir = tempfile.mkdtemp(prefix="triad_e2e_")
    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PX_PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": HERE,
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": str(UP_PORT),
        "ARMOR_UPSTREAM_HTTPS": "0",
        "ARMOR_TRACE_DIR": trace_dir,
        "ARMOR_REQUIRE_KEY": "0",
        "ARMOR_RETRY_MAX": "3",
        "ARMOR_RETRY_BUDGET": "40",
        "ARMOR_SNIFF_DEADLINE": "10",
        "ARMOR_TURN_DEADLINE": "60",
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_CAPTURE_RESP": "0",
        # Independent panel file: the real one is applied at boot and would
        # override ARMOR_REQUIRE_KEY above (measured 2026-10-31: the test started
        # 502ing because the saved panel had require_client_key=true).
        "ARMOR_PANEL_FILE": os.path.join(trace_dir, "panel_settings.json"),
        "ARMOR_KEY_POOL": os.path.join(HERE, "proxy", "upstream_keys.txt"),
    })
    log = open(os.path.join(trace_dir, "proxy.log"), "w+")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))
            print("  FAIL %-32s got=%r want=%r" % (name, got, want))
        else:
            print("  ok   %-32s %r" % (name, got))

    try:
        if not wait_port(PX_PORT):
            print("proxy did not start; log tail:")
            log.seek(0)
            print(log.read()[-2000:])
            return 1

        req = {"model": "fake-model",
               "messages": [{"role": "user",
                             "content": "write a complete program that does the thing"}]}

        print("A. buffered: first attempt refused, retry succeeds")
        CALLS["n"] = 0
        MODE["stream"] = False
        r = urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT,
                data=json.dumps(req).encode(),
                headers={"Content-Type": "application/json"}),
            timeout=90)
        hdr = r.headers
        body = json.loads(r.read().decode())
        check("http", r.status, 200)
        check("upstream calls", CALLS["n"], 2)
        check("X-Armor-Retries", hdr.get("X-Armor-Retries"), "1")
        check("X-Armor-Verdict", hdr.get("X-Armor-Verdict"), "retry_ok")
        check("delivered the retry", "artefact" in body["choices"][0]["message"]["content"], True)

        print("B. buffered: every attempt refused -> passed with a marker")
        CALLS["n"] = 0
        MODE["always_refuse"] = True
        r = urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT,
                data=json.dumps(req).encode(),
                headers={"Content-Type": "application/json"}),
            timeout=120)
        hdr = r.headers
        r.read()
        check("http", r.status, 200)
        check("X-Armor-Verdict", hdr.get("X-Armor-Verdict"), "passed")
        check("retries counted", int(hdr.get("X-Armor-Retries") or 0) >= 1, True)
        MODE["always_refuse"] = False

        print("C. stream: sidecar verdict comment reaches the client")
        CALLS["n"] = 0
        MODE["stream"] = True
        r = urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT,
                data=json.dumps(dict(req, stream=True)).encode(),
                headers={"Content-Type": "application/json"}),
            timeout=120)
        raw = r.read().decode("utf-8", "replace")
        check("stream has data", "[DONE]" in raw, True)
        had_sidecar = ":armor" in raw
        check("sidecar emitted", had_sidecar, True)
        if had_sidecar:
            line = [l for l in raw.splitlines() if l.startswith(":armor ")][0]
            v = json.loads(line[7:])
            check("sidecar retries", v.get("retries"), 1)
            check("sidecar verdict", v.get("verdict"), "retry_ok")
        MODE["stream"] = False

        print("D. trace records")
        day = [f for f in os.listdir(trace_dir) if f.endswith(".jsonl")]
        check("trace files", len(day) >= 1, True)
        recs = []
        for f in day:
            with open(os.path.join(trace_dir, f)) as fh:
                recs += [json.loads(l) for l in fh if l.strip()]
        check("three turns recorded", len(recs), 3)
        first = recs[0]
        check("attempts kept", len(first["attempts"]), 2)
        check("refusal text preserved", first["attempts"][0]["text"], BAD)
        check("final is the good answer",
              "artefact" in first["final"], True)
        check("client recorded", bool(first["client"]), True)
        check("conv recorded", bool(first["conv"]), True)
        check("passed turn marked", recs[1].get("retries") >= 1, True)

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
        print(log.read()[-3000:])
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
