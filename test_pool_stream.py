#!/usr/bin/env python3
"""串流 + 連線池測試 — 最危險的組合：SSE 沒讀完就把 socket 放回池子，
下一個請求會把上一個 body 的尾巴當成狀態行。

用本地假上游產生 SSE，驗證：
 1. 完整讀完的串流可以被重用（第二個請求同一 socket）
 2. 中途放棄的串流不會被放回池子
 3. 串流後接著一個普通請求，回應內容正確（沒有前一個 body 的殘留）
 4. 連續 20 條串流 + 20 條普通請求交錯，所有回應都完整
"""
import sys, os, threading, json, time, socket as _s
sys.path.insert(0, "/var/hark/shared/armor_triad")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

os.environ.setdefault("ARMOR_PROXY_PORT", "1")
import armor_proxy as AP

PEERS = []
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            want_sse = json.loads(body or b"{}").get("stream")
        except Exception:
            want_sse = False
        with LOCK:
            PEERS.append(self.connection.getpeername()[1])
            n_req = len(PEERS)
        if want_sse:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            payload = b""
            for i in range(5):
                d = ("data: " + json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": "tok%d " % i}}],
                     "req": n_req}) + "\n\n").encode()
                self.wfile.write(b"%X\r\n" % len(d) + d + b"\r\n")
                payload += d
            end = b"data: [DONE]\n\n"
            self.wfile.write(b"%X\r\n" % len(end) + end + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            out = json.dumps({"ok": True, "req": n_req}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


def main():
    port = 18096
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
            print("  PASS  %-48s %s" % (name, extra))
        else:
            fail += 1
            print("  FAIL  %-48s %s" % (name, extra))

    def raw(body):
        b = json.dumps(body).encode()
        conn, key, resp = AP._route_request(
            cls, "127.0.0.1", port, False, kw, "POST", "/x",
            body=b, headers={"Content-Length": str(len(b))})
        return conn, key, resp

    # --- 1. 串流完整讀完 -> 可重用 ------------------------------------------
    c, k, resp = raw({"stream": True})
    chunks = []
    while True:
        ch = resp.read1(4096)
        if not ch:
            break
        chunks.append(ch)
    blob = b"".join(chunks)
    AP._pool_put(k, c)
    check("串流讀到 [DONE]", b"[DONE]" in blob)
    check("串流有 5 個 token", blob.count(b"tok") == 5)

    p_before = PEERS[-1]
    c2, k2, resp2 = raw({})
    d2 = json.loads(resp2.read())
    AP._pool_put(k2, c2)
    check("串流後重用同一 socket", PEERS[-1] == p_before,
          "peers %s" % PEERS[-2:])

    # --- 2. 中途放棄的串流不進池子 ------------------------------------------
    c3, k3, resp3 = raw({"stream": True})
    resp3.read1(64)                 # 只讀一點就放掉
    AP._pool_drop(k3, c3)
    with AP._CONN_POOL_LOCK:
        idle = len(AP._CONN_POOL.get(k3) or [])
    check("半讀串流沒有進池子", idle == 0, "idle=%d" % idle)

    # --- 3. 串流後接普通請求：內容不能有殘留 --------------------------------
    c4, k4, resp4 = raw({"stream": True})
    while resp4.read1(4096):
        pass
    AP._pool_put(k4, c4)
    c5, k5, resp5 = raw({})
    body5 = resp5.read()
    AP._pool_put(k5, c5)
    try:
        json.loads(body5)
        clean = True
    except Exception:
        clean = False
    check("串流後的 JSON 回應完整", clean, body5[:60].decode("latin-1"))
    check("JSON 不含串流殘留", b"tok" not in body5 and b"DONE" not in body5)

    # --- 4. 交錯壓力：20 串流 + 20 普通 --------------------------------------
    errs = []
    for i in range(20):
        cc, kk, rr = raw({"stream": True})
        got = b""
        while True:
            ch = rr.read1(4096)
            if not ch:
                break
            got += ch
        if b"[DONE]" not in got or got.count(b"tok") != 5:
            errs.append("stream%d" % i)
        AP._pool_put(kk, cc)

        cc, kk, rr = raw({})
        try:
            json.loads(rr.read())
        except Exception:
            errs.append("json%d" % i)
        AP._pool_put(kk, cc)
    check("20 輪串流+普通全部完整", not errs, str(errs[:3]))

    with AP._CONN_POOL_LOCK:
        total_idle = sum(len(v) for v in AP._CONN_POOL.values())
    check("池子沒有無限膨脹", total_idle <= 6, "idle=%d" % total_idle)

    AP._pool_clear()
    srv.shutdown()
    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
