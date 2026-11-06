#!/usr/bin/env python3
"""連線池的「不重複送出」保證測試。

POST 到計費 API 送兩次 = 付兩次錢、可能重複執行工具呼叫。所以：
  * 從池子取出的連線在「寫入階段」死掉 -> 可以重試（bytes 沒送完整）
  * 請求已寫出後才失敗（getresponse 掛掉）-> 必須報錯，不能重送
  * 全新連線失敗 -> 不重試（那是真的網路問題）

用一個會記錄「收到幾次完整請求」的假上游來驗證。
"""
import json
import os
import sys
import threading
import time
import socket as _s
sys.path.insert(0, "/var/hark/shared/armor_triad")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

os.environ.setdefault("ARMOR_PROXY_PORT", "1")
import armor_proxy as AP

RECEIVED = []
LOCK = threading.Lock()
BEHAVIOUR = {"mode": "ok"}


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        with LOCK:
            RECEIVED.append(body)
        if BEHAVIOUR["mode"] == "hang_up_after_read":
            # 讀完請求但直接斷線 -> 客戶端在 getresponse() 失敗
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return
        out = json.dumps({"n": len(RECEIVED), "len": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main():
    port = 18095
    srv = ThreadingHTTPServer(("127.0.0.1", port), Up)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    cls, kw = http.client.HTTPConnection, {"timeout": 10}
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %-50s %s" % (name, extra))
        else:
            fail += 1
            print("  FAIL  %-50s %s" % (name, extra))

    # --- 1. 正常：一個請求送一次 ------------------------------------------
    RECEIVED.clear()
    body = b'{"a":1}'
    conn, key, resp = AP._route_request(cls, "127.0.0.1", port, False, kw,
                                        "POST", "/x", body=body,
                                        headers={"Content-Length": str(len(body))})
    resp.read()
    AP._pool_put(key, conn)
    check("正常請求只送出一次", len(RECEIVED) == 1, "收到的次數=%d" % len(RECEIVED))

    # --- 2. 池中連線在寫入前死掉 -> 可重試，且總共只成功一次 ---------------
    with AP._CONN_POOL_LOCK:
        idle = AP._CONN_POOL.get(key) or []
    if idle:
        try:
            idle[0].sock.close()      # 對端已關，寫入時會 EPIPE
        except Exception:
            pass
    RECEIVED.clear()
    conn2, key2, resp2 = AP._route_request(cls, "127.0.0.1", port, False, kw,
                                           "POST", "/x", body=body,
                                           headers={"Content-Length": str(len(body))})
    d2 = json.loads(resp2.read())
    AP._pool_put(key2, conn2)
    check("死連線重連後成功", d2.get("n") is not None)
    check("上游只收到一次完整請求", len(RECEIVED) == 1,
          "收到 %d 次（重試不得造成重複）" % len(RECEIVED))

    # --- 3. 請求寫出後才失敗 -> 必須報錯，不能重送 -------------------------
    BEHAVIOUR["mode"] = "hang_up_after_read"
    RECEIVED.clear()
    raised = None
    try:
        c3, k3, r3 = AP._route_request(cls, "127.0.0.1", port, False, kw,
                                       "POST", "/x", body=body,
                                       headers={"Content-Length": str(len(body))})
        try:
            r3.read()
        except Exception:
            pass
        AP._pool_drop(k3, c3)
    except Exception as exc:
        raised = exc
    check("請求已送出後失敗會報錯", raised is not None,
          type(raised).__name__ if raised else "沒有拋錯")
    check("已送出的請求沒有被重送", len(RECEIVED) == 1,
          "上游收到 %d 次（必須是 1）" % len(RECEIVED))

    # --- 4. 全新連線失敗 -> 不重試 ----------------------------------------
    BEHAVIOUR["mode"] = "ok"
    RECEIVED.clear()
    AP._pool_clear()
    raised2 = None
    try:
        AP._route_request(cls, "127.0.0.1", 18000, False, {"timeout": 2},
                          "POST", "/x", body=body,
                          headers={"Content-Length": str(len(body))})
    except Exception as exc:
        raised2 = exc
    check("連不上時直接報錯（不重試）", raised2 is not None,
          type(raised2).__name__ if raised2 else "沒有拋錯")

    AP._pool_clear()
    srv.shutdown()
    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
