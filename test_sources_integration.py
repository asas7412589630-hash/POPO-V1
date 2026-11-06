#!/usr/bin/env python3
"""Integration test for multi source model routing.

test_model_sources.py only unit-tests the module. This drives the real HTTP
surface a console talks to, against a fake "external source" upstream, and
proves the promise the console makes actually holds end to end:

  * save a source, its key is stored but never returned again
  * discover its models
  * publish a subset -> namespaced aliases appear in /v1/models
  * a request for <source>/<model> is forwarded to THAT source, with THAT key,
    and the body keeps the bare model id (not the namespaced alias)
  * an unpublished alias is not routable

It also pins two integration bugs found while writing it:
  * base_url ending in /v1 must not become /v1/v1/... on forward
  * /sources must be answered locally, never forwarded upstream
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
SRC_PORT = 19791         # fake external source
PX_PORT = 19792          # proxy under test

SEEN = []                # what the source upstream received


class Source(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"object": "list", "data": [
                {"id": "alpha-1"}, {"id": "alpha-2"}, {"id": "beta-9"}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        model = body.get("model")
        SEEN.append({"path": self.path,
                     "auth": self.headers.get("Authorization") or "",
                     "model": model})
        # A real API answers 404 for a model it does not host. Without this the
        # fake answered 200 to anything, so "an unpublished alias is refused"
        # could only be tested against the LIVE internet — and logfare.ai takes
        # ~25 s to 404 an unknown model, which is longer than the client timeout.
        # The test was therefore flaky in a way that looked like a proxy bug.
        if model not in ("alpha-1", "alpha-2", "beta-9"):
            return self._json({"error": {"message": "model not found",
                                         "type": "invalid_request_error"}}, 404)
        return self._json({"id": "x", "object": "chat.completion", "model": body.get("model"),
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant",
                                                    "content": "## Result\n\n"
                                                    "This is a complete, substantive answer with a heading, "
                                                    "a paragraph of explanation and a fenced block, long "
                                                    "enough that the armour's thin-answer retry does not "
                                                    "fire and the call is observed exactly once.\n\n"
                                                    "```py\nprint('ok')\n```\n"}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def wait_port(port, timeout=12):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def call(method, path, body=None, key=""):
    url = "http://127.0.0.1:%d%s" % (PX_PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    # Admin WRITES require this header (CSRF gate): a cross-origin page cannot
    # send it without a preflight the proxy refuses. Reads do not need it.
    if method not in ("GET", "HEAD"):
        req.add_header("X-Armor-Admin", "1")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    up = ThreadingHTTPServer(("127.0.0.1", SRC_PORT), Source)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="triad_src_")
    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PX_PORT),
        "ARMOR_BIND": "127.0.0.1",
        # ARMOR_BASE must be the TEMP dir, not the checkout. The proxy derives
        # proxy/keys.json, proxy/source_secrets.json and the rest from BASE, so
        # pointing it at the repo meant every test run wrote a real key record
        # into the working tree — which then got committed, and made the
        # "no committed secrets" CI job fail on an otherwise clean push.
        # Only the code (armor_proxy.py) is read from HERE; all state goes to tmp.
        "ARMOR_BASE": tmp,
        # Code lives in HERE; only state is redirected. These are the three the
        # proxy reads that are NOT derived from BASE.
        "ARMOR_CONSOLE_DIR": os.path.join(HERE, "console"),
        "ARMOR_SOURCES_FILE": os.path.join(tmp, "sources.json"),
        "ARMOR_SOURCE_SECRETS": os.path.join(tmp, "secrets.json"),
        "ARMOR_ROUTES_FILE": os.path.join(tmp, "routes.json"),
        "ARMOR_ALIAS_FILE": os.path.join(tmp, "aliases.json"),
        "ARMOR_REQUIRE_KEY": "1",
        "ARMOR_CLIENT_KEYS": os.path.join(tmp, "client_keys.txt"),
        "ARMOR_KEYS_JSON": os.path.join(tmp, "keys.json"),
        "ARMOR_TRACE_DIR": tmp,
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
        "ARMOR_STATE": "0", "ARMOR_ARCHIVE": "0", "ARMOR_CAPTURE_RESP": "0",
        "ARMOR_MEDIA_DIR": os.path.join(tmp, "media"),
        # Hermetic: the shared provider IS the fake. An unpublished alias falls
        # through to it (by design) and must not reach the real internet.
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": str(SRC_PORT),
        "ARMOR_UPSTREAM_HTTPS": "0",
    })
    with open(env["ARMOR_CLIENT_KEYS"], "w", encoding="utf-8") as f:
        f.write("test-key-123\n")
    log = open(os.path.join(tmp, "proxy.log"), "w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    KEY = "test-key-123"
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print("  FAIL %-40s got=%r want=%r" % (name, got, want))
        else:
            print("  ok   %-40s %r" % (name, got))

    try:
        if not wait_port(PX_PORT):
            log.seek(0)
            print("proxy did not start:\n" + log.read()[-1500:])
            return 1

        print("A. /sources is answered locally (never forwarded upstream)")
        # Loopback callers are exempt from the key check by design, so this
        # asserts local handling, not auth: an unknown upstream path would 404.
        st, _ = call("GET", "/sources")
        check("sources answered locally", st, 200)

        print("B. register a source (key stored, never echoed)")
        st, r = call("POST", "/sources", {
            "id": "srcdemo", "name": "Demo", "base_url": "http://127.0.0.1:%d/v1" % SRC_PORT,
            "api_key": "sk-source-secret"}, key=KEY)
        check("save source ok", st, 200)
        check("secret not echoed", "sk-source-secret" in json.dumps(r), False)
        check("configured flag", r.get("source", {}).get("configured"), True)

        print("C. discover models from the source")
        st, r = call("GET", "/sources/srcdemo/models", key=KEY)
        ids = sorted(m["id"] for m in r.get("models", []))
        check("discovered", ids, ["alpha-1", "alpha-2", "beta-9"])

        print("D. publish a subset -> namespaced alias")
        st, r = call("POST", "/models/publish",
                     {"source_id": "srcdemo", "model_ids": ["alpha-1"],
                      "profiles": {"alpha-1": "native_gpt"}}, key=KEY)
        check("publish ok", st, 200)
        st, r = call("GET", "/sources/srcdemo/models", key=KEY)
        by_id = {m["id"]: m for m in r["models"]}
        check("alpha-1 enabled", by_id["alpha-1"]["enabled"], True)
        check("alpha-2 not enabled", by_id["alpha-2"]["enabled"], False)

        print("E. /v1/models lists the published alias")
        st, r = call("GET", "/v1/models", key=KEY)
        model_ids = {m["id"] for m in r.get("data", [])}
        check("alias in catalogue", "srcdemo/alpha-1" in model_ids, True)

        print("F. routed request goes to the source, bare model, source key")
        SEEN.clear()
        st, r = call("POST", "/v1/chat/completions",
                     {"model": "srcdemo/alpha-1",
                      "messages": [{"role": "user", "content": "hi"}]}, key=KEY)
        check("routed status", st, 200)
        check("hit source once", len(SEEN), 1)
        if SEEN:
            check("path not /v1/v1", SEEN[0]["path"], "/v1/chat/completions")
            check("bare model forwarded", SEEN[0]["model"], "alpha-1")
            check("source key used", SEEN[0]["auth"], "Bearer sk-source-secret")

        print("G. unpublished alias is not routable")
        SEEN.clear()
        st, r = call("POST", "/v1/chat/completions",
                     {"model": "srcdemo/beta-9",
                      "messages": [{"role": "user", "content": "hi"}]}, key=KEY)
        check("unpublished refused", st in (400, 404), True)
        check("no source hit", len(SEEN), 0)

        print()
        if fails:
            print("FAILED: %d" % len(fails))
            return 1
        print("ALL PASS (sources integration)")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
