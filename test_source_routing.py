#!/usr/bin/env python3
"""Routing contract for console-published models.

Asserts the four things a user actually cares about, against a fake source:
  1. a published alias reaches ITS source, with that source's key, using the
     real upstream model id (not the alias);
  2. each model gets the block set its name maps to, and the response says so;
  3. a second request in the same conversation on a different model does not
     carry the first model's blocks (client replays the system message);
  4. the shared provider is untouched by any of it.
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
SRC_PORT = 19791
PX_PORT = 19792
SEEN = {"auth": [], "models": [], "systems": []}


class FakeSource(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": "deepseek-v9"}, {"id": "gpt-7"}]}).encode()
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
        req = json.loads(self.rfile.read(n) or b"{}")
        SEEN["auth"].append(self.headers.get("Authorization") or "")
        SEEN["models"].append(req.get("model") or "")
        systems = [m.get("content") for m in req.get("messages") or []
                   if m.get("role") == "system"]
        SEEN["systems"].append("\n".join(str(c) for c in systems))
        body = json.dumps({
            "id": "x", "object": "chat.completion", "model": req.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": "ok, done.\n\n```py\nprint(1)\n```"},
                        "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_port(port, timeout=12):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def main():
    src = ThreadingHTTPServer(("127.0.0.1", SRC_PORT), FakeSource)
    threading.Thread(target=src.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="triad_src_")
    files = {
        "src": os.path.join(tmp, "model_sources.json"),
        "sec": os.path.join(tmp, "source_secrets.json"),
        "alias": os.path.join(tmp, "armor_alias.json"),
        "routes": os.path.join(tmp, "model_routes.json"),
    }
    with open(files["src"], "w") as f:
        json.dump({"sources": [{"id": "mock", "name": "Mock",
                                "base_url": "http://127.0.0.1:%d/v1" % SRC_PORT}]}, f)
    with open(files["sec"], "w") as f:
        json.dump({"mock": "test-token"}, f)
    with open(files["alias"], "w") as f:
        json.dump({"mock/deepseek-v9": ["deepseek-v9"], "mock/gpt-7": ["gpt-7"]}, f)
    with open(files["routes"], "w") as f:
        json.dump({"mock/deepseek-v9": {"source_id": "mock", "model_id": "deepseek-v9",
                                    "base_url": "http://127.0.0.1:%d/v1" % SRC_PORT,
                                    "profile": ""},
                   "mock/gpt-7": {"source_id": "mock", "model_id": "gpt-7",
                                    "base_url": "http://127.0.0.1:%d/v1" % SRC_PORT,
                                    "profile": ""}}, f)

    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PX_PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": HERE,
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": "1",          # nothing listens: must not be used
        "ARMOR_UPSTREAM_HTTPS": "0",
        "ARMOR_TRACE_DIR": os.path.join(tmp, "trace"),
        "ARMOR_REQUIRE_KEY": "0",
        "ARMOR_RETRY_MAX": "0",
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_CAPTURE_RESP": "0",
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
        "ARMOR_SOURCES_FILE": files["src"],
        "ARMOR_SOURCE_SECRETS": files["sec"],
        "ARMOR_ALIAS_FILE": files["alias"],
        "ARMOR_ROUTES_FILE": files["routes"],
        "ARMOR_LEVEL": "native",
    })
    log = open(os.path.join(tmp, "proxy.log"), "w+")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print("  FAIL %-34s got=%r want=%r" % (name, got, want))
        else:
            print("  ok   %-34s %r" % (name, got))

    def call(model, system_text):
        req = {"model": model, "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": "do the thing"}]}
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT,
            data=json.dumps(req).encode(),
            headers={"Content-Type": "application/json"}), timeout=60)
        r.read()
        return r.headers

    try:
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-2000:])
            return 1

        print("1. published alias reaches its own source")
        h = call("mock/deepseek-v9", "host persona")
        check("upstream model rewritten", SEEN["models"][-1], "deepseek-v9")
        check("source key used", SEEN["auth"][-1], "Bearer test-token")
        check("X-Armor-Source", h.get("X-Armor-Source"), "mock")
        check("X-Armor-Request-Model", h.get("X-Armor-Request-Model"), "mock/deepseek-v9")

        print("2. per-model block set, visible to the client")
        check("deepseek-v9 profile", h.get("X-Armor-Profile"), "native_deepseek")
        check("deepseek-v9 blocks", h.get("X-Armor-Blocks"), "persona,contract")
        h = call("mock/gpt-7", "host persona")
        check("gpt-7 profile", h.get("X-Armor-Profile"), "native_gpt")
        check("gpt-7 blocks", h.get("X-Armor-Blocks"), "contract")

        print("3. mid-conversation switch does not keep old blocks")
        old = SEEN["systems"][0]
        h = call("mock/gpt-7", "host persona\n\n" + old)
        now = SEEN["systems"][-1]
        check("old persona stripped", "<|start|>system:<instructions>" in now, False)
        check("old contract stripped", now.count('<policy runtime="compile"'), 1)
        check("new profile applied", h.get("X-Armor-Profile"), "native_gpt")

        print("4. global level cannot flatten per-model mapping")
        req = {"model": "mock/deepseek-v9", "messages": [
            {"role": "user", "content": "hi"}]}
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT,
            data=json.dumps(req).encode(),
            headers={"Content-Type": "application/json",
                     "X-Armor-Level": "full"}), timeout=60)
        r.read()
        # glm/deepseek armour is LOCKED (2026-11-04): every level resolves to the
        # fixed profile, so an explicit full cannot downgrade or upgrade it.
        check("locked family ignores explicit full",
              r.headers.get("X-Armor-Profile"), "native_deepseek")

        print()
        if fails:
            print("FAILED: %s" % ", ".join(fails))
            return 1
        print("ALL PASS")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        src.shutdown()


if __name__ == "__main__":
    sys.exit(main())
