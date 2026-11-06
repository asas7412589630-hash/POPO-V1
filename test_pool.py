#!/usr/bin/env python3
"""連線池單元測試 — 用本地假上游，不打真上游、不花錢。

驗證四件事：
 1. 第二次請求重用同一條 socket（用 peer port 判斷，不是用請求數）
 2. 被對端關掉的閒置連線不會被交出去（探活擋掉）
 3. `_pool_drop` 之後池子是空的
 4. 連線在請求前死掉時，_route_request 會自動重連而不是拋錯
"""
import sys
import os, os, threading, json, time, socket as _s
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

os.environ.setdefault("ARMOR_PROXY_PORT", "1")
import armor_proxy as AP

PEERS = []          # 每個請求看到的 client peer port
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        with LOCK:
            PEERS.append(self.connection.getpeername()[1])
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main():
    port = 18099
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    cls, kw = http.client.HTTPConnection, {"timeout": 10}
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %-44s %s" % (name, extra))
        else:
            fail += 1
            print("  FAIL  %-44s %s" % (name, extra))

    def call():
        conn, key, resp = AP._route_request(
            cls, "127.0.0.1", port, False, kw, "POST", "/x",
            body=b"{}", headers={"Content-Length": "2"})
        d = json.loads(resp.read())
        return conn, key, d

    # --- 1. 重用 -----------------------------------------------------------
    c1, k1, d1 = call()
    AP._pool_put(k1, c1)
    c2, k2, d2 = call()
    AP._pool_put(k2, c2)
    check("第 1、2 次請求都成功", d1["ok"] and d2["ok"])
    check("第 2 次重用同一條 socket", PEERS[0] == PEERS[1],
          "peer ports: %s" % PEERS)

    # --- 2. 死連線不會被交出 ------------------------------------------------
    with AP._CONN_POOL_LOCK:
        idle = AP._CONN_POOL.get(k1) or []
    if idle:
        try:
            idle[0].sock.close()
        except Exception:
            pass
        got = AP._pool_take(k1)
        check("被關掉的閒置連線被丟棄", got is None)

    # --- 3. _pool_drop 清空 -------------------------------------------------
    c3, k3, d3 = call()
    AP._pool_drop(k3, c3)
    with AP._CONN_POOL_LOCK:
        n_idle = len(AP._CONN_POOL.get(k3) or [])
    check("_pool_drop 之後池子是空的", n_idle == 0, "idle=%d" % n_idle)

    # --- 4. 探活後連線才死 -> 自動重連 --------------------------------------
    c4, k4, d4 = call()
    AP._pool_put(k4, c4)
    try:
        c4.sock.shutdown(_s.SHUT_RDWR)     # 探活會看到 EOF -> 丟棄後重連
    except Exception:
        pass
    c5, k5, d5 = call()
    AP._pool_put(k5, c5)
    check("死連線自動重連成功", d5["ok"])

    AP._pool_clear()
    srv.shutdown()
    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
