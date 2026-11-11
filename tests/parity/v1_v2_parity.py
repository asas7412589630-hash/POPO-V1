"""Environment behaviour checks for the proxy (V2 only).

**What this is not.** This is not a V1/V2 differential test. It starts V2 against
a mock upstream and asserts V2's own contract: status, headers, armor injection,
retry counting. Calling that "parity" overstated what it proves, so the checks
are named after what they actually verify. A genuine differential comparison
lives in ``tests/parity/v1_diff.py`` and needs a V1 checkout.

Run:
    python3 tests/parity/v1_v2_parity.py
"""
from __future__ import annotations
import json
import os
import sys

# A CP950 console cannot encode the arrow in this file's historical heading.
# Force UTF-8 so a locale setting can never be mistaken for a check failure.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from popo.config import Config
from popo.server.app import ApiHandler


class MockHandler(BaseHTTPRequestHandler):
    data: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        MockHandler.data.append(payload)
        last = payload.get("messages", [{}])[-1].get("content", "")
        content = "我無法。" if "REFUSE" in str(last) else "ok"
        body = json.dumps({
            "id": "mock", "object": "chat.completion",
            "model": payload.get("model", "x"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_proxy(armor_off: bool = True):
    upstream_port = _free_port()
    proxy_port = _free_port()
    upstream = HTTPServer(("127.0.0.1", upstream_port), MockHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    os.environ["POPO_UPSTREAM_HOST"] = f"http://127.0.0.1:{upstream_port}/v1"
    os.environ["POPO_UPSTREAM_KEY"] = "k"
    os.environ["POPO_PORT"] = str(proxy_port)
    os.environ["POPO_UPSTREAM_TIMEOUT"] = "5"
    os.environ["POPO_ARMOR_OFF"] = "1" if armor_off else "0"
    os.environ["POPO_REQUIRE_KEY"] = "0"
    # The mock upstream is on loopback; the egress policy blocks that by design.
    os.environ["POPO_ALLOW_PRIVATE_UPSTREAM"] = "1"

    config = Config.from_env()
    ApiHandler.config = config
    ApiHandler.reset_shared_state()
    proxy = HTTPServer(("127.0.0.1", proxy_port), ApiHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return proxy, upstream, proxy_port


def _post(port: int, path: str, body: dict, *, headers: dict | None = None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                 headers=request_headers)
    response = conn.getresponse()
    raw = response.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    result_headers = dict(response.getheaders())
    conn.close()
    return response.status, data, result_headers


def main() -> int:
    checks: list[tuple[str, bool]] = []

    MockHandler.data.clear()
    proxy, upstream, port = start_proxy(armor_off=True)
    status, data, headers = _post(port, "/v1/chat/completions", {
        "model": "glm-5.3", "messages": [{"role": "user", "content": "hello"}]})
    proxy.shutdown()
    upstream.shutdown()

    checks.append(("chat returns 200", status == 200 and "error" not in data))
    checks.append(("response has choices[0].message.content",
                   bool(data.get("choices", [{}])[0].get("message", {}).get("content"))))
    checks.append(("X-Armor-Verdict present", "X-Armor-Verdict" in headers))
    checks.append(("X-Armor-Retries present", "X-Armor-Retries" in headers))
    armor_off_no_system = (all(m.get("role") != "system"
                               for m in MockHandler.data[0].get("messages", []))
                           if MockHandler.data else False)
    checks.append(("armor off means no system prompt upstream", armor_off_no_system))
    checks.append(("one upstream request for a clean answer", len(MockHandler.data) == 1))

    MockHandler.data.clear()
    proxy2, upstream2, port2 = start_proxy(armor_off=False)
    status2, data2, _headers2 = _post(port2, "/v1/chat/completions", {
        "model": "glm-5.3", "messages": [{"role": "user", "content": "hello"}]})
    checks.append(("armor on returns 200", status2 == 200 and "error" not in data2))
    first = MockHandler.data[0]["messages"][0] if MockHandler.data else {}
    checks.append(("armor on injects system prompt at messages[0]",
                   first.get("role") == "system"
                   and len(str(first.get("content", ""))) > 10))
    proxy2.shutdown()
    upstream2.shutdown()

    MockHandler.data.clear()
    proxy3, upstream3, port3 = start_proxy(armor_off=False)
    status3, data3, headers3 = _post(port3, "/v1/chat/completions", {
        "model": "glm-5.3", "messages": [{"role": "user", "content": "REFUSE"}]})
    checks.append(("refusal request returns 200", status3 == 200 and "error" not in data3))
    checks.append(("refusal reports a verdict",
                   headers3.get("X-Armor-Verdict") in
                   ("clean", "retry_ok", "retry_best", "salvaged", "retry_failed")))
    checks.append(("refusal retry makes at least one upstream attempt",
                   len(MockHandler.data) >= 1))
    proxy3.shutdown()
    upstream3.shutdown()

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\nV2 environment checks: {passed}/{total} passed")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
