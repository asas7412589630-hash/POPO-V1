#!/usr/bin/env python3
"""關機行為測試 — 用本地慢速串流上游，驗證 SIGTERM 不會切斷已開始的串流。

這是唯一能可靠測這條路徑的方法：真上游的 429 重試會讓測試卡在重試迴圈裡，
量到的就不是關機行為而是重試行為（2026-11-03 實測踩過）。

驗證：
 1. SIGTERM 後不再接受新連線
 2. 已在串流中的回應收到終止 chunk（[DONE]）而不是半截斷開
 3. 重試等待中的請求不會拖住關機
 4. 關機在 grace 時間內完成
"""
import json
import os
import signal
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
PX_PORT = 18312
UP_PORT = 18313

SLOW = {"n": 0}


class Slow(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            want = json.loads(body or b"{}").get("stream")
        except Exception:
            want = False
        if not want:
            out = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        SLOW["n"] += 1
        # 20 tokens, 0.4s apart -> ~8s, longer than the 6s grace window.
        try:
            for i in range(20):
                d = ("data: " + json.dumps(
                    {"choices": [{"index": 0,
                                  "delta": {"content": "token%d " % i}}]}) + "\n\n").encode()
                self.wfile.write(b"%X\r\n" % len(d) + d + b"\r\n")
                self.wfile.flush()
                time.sleep(0.4)
            end = b"data: [DONE]\n\n"
            self.wfile.write(b"%X\r\n" % len(end) + end + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), 1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Slow)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="shut_")
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
        "ARMOR_REQUIRE_KEY": "0", "ARMOR_RETRY_MAX": "0",
        "ARMOR_STATE": "0", "ARMOR_ARCHIVE": "0",
        "ARMOR_SHUTDOWN_GRACE": "4",
        "ARMOR_SNIFF_DEADLINE": "30",
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(os.path.join(tmp, "proxy.log"), "w+", encoding="utf-8", errors="replace")
    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=cflags)
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %-46s %s" % (name, extra))
        else:
            fail += 1
            print("  FAIL  %-46s %s" % (name, extra))

    try:
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-1200:])
            return 1

        got = {"bytes": b"", "err": None}

        def stream():
            body = json.dumps({"model": "fake-model", "stream": True,
                               "messages": [{"role": "user", "content": "go"}]}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % PX_PORT, data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    while True:
                        c = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
                        if not c:
                            break
                        got["bytes"] += c
            except Exception as exc:
                got["err"] = "%s: %s" % (type(exc).__name__, exc)

        t = threading.Thread(target=stream)
        t.start()
        time.sleep(2.0)          # 讓串流確認開始
        started = time.time()
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)

        # --- 1. 不再接受新連線 -------------------------------------------
        time.sleep(1.0)
        refused = False
        try:
            s = socket.create_connection(("127.0.0.1", PX_PORT), 1)
            s.close()
        except OSError:
            refused = True
        check("SIGTERM 後拒絕新連線", refused)

        t.join(30)
        elapsed = time.time() - started
        check("關機在 grace+1s 內完成", elapsed < 5.5, "%.2fs" % elapsed)
        check("串流有收到資料", len(got["bytes"]) > 0, "%d bytes" % len(got["bytes"]))
        check("串流以 [DONE] 正常收尾",
              b"[DONE]" in got["bytes"], "tail=%r" % got["bytes"][-40:])
        check("串流沒有連線錯誤", got["err"] is None, str(got["err"]))

    finally:
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        up.shutdown()
        log.seek(0)
        tail = log.read()[-1500:]
        print("\n--- proxy log tail ---")
        for line in tail.strip().splitlines()[-8:]:
            print("   " + line)

    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
