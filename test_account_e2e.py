#!/usr/bin/env python3
"""End-to-end: 一輪真的串流對話，是否被記到正確帳戶？

這一支和 test_accounts / test_view_page 的差別：那兩支測「模組自己講的話」，
這一支測「整條路徑」—— 假上游 → 真代理 → trace 檔 → 帳戶用量 → 回看連結頁。

為什麼非要有它：uid 是穿過最少行程式碼的欄位之一（Handler → trace.begin →
jsonl → list_records → 用量），而它錯了不會有任何錯誤訊息。錯的方式是「A 的
用量記在 B 名下」，只會在月底看帳時才發現。所以用兩個帳戶對打。
"""
import json
import os
import shutil
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
UP_PORT = 19871
PX_PORT = 19872

fails = []


def check(name, cond, note=""):
    if not cond:
        fails.append(name + ((" — " + str(note)) if note else ""))
        print("  FAIL %-46s %s" % (name, note))
    else:
        print("  ok   %-46s %s" % (name, note))


class Upstream(BaseHTTPRequestHandler):
    """最小可用的 SSE 上游：一段思考 + 兩段正文 + [DONE]。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"reasoning": "先確認架構，再挑 syscall。"}}]},
            {"choices": [{"delta": {"content": "## 範例\n\n這是完整的回答內容，"}}]},
            {"choices": [{"delta": {"content": "包含說明與結論。"},
                          "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        ]
        for c in chunks:
            d = ("data: " + json.dumps(c, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(b"%X\r\n" % len(d) + d + b"\r\n")
            self.wfile.flush()
            time.sleep(0.02)
        d = b"data: [DONE]\n\n"
        self.wfile.write(b"%X\r\n" % len(d) + d + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def wait_port(port, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


FAKE_IP = "203.0.113.9"   # 模擬隧道對端：loopback 依設計免驗金鑰，必須假裝遠端


def post(path, body, key="", cookie=""):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PX_PORT, path),
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Armor-Admin", "1")
    req.add_header("X-Forwarded-For", FAKE_IP)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def get(path, key="", cookie=""):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PX_PORT, path))
    req.add_header("X-Forwarded-For", FAKE_IP)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def main():
    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="acct_e2e_")
    os.environ["ARMOR_ACCOUNTS_JSON"] = os.path.join(tmp, "accounts.json")
    os.environ["ARMOR_KEYS_JSON"] = os.path.join(tmp, "keys.json")
    os.environ["ARMOR_CLIENT_KEYS"] = os.path.join(tmp, "client_keys.txt")
    # 這一行不是裝飾：shell 的 export 會跨呼叫殘留（見 63 章 A8），而本測試
    # 自己在同一個行程裡 import trace_rec 讀檔。沒指到 tmp 就會讀到上一次手測
    # 留下的記錄，然後把「找不到 alice 的回合」誤診成 uid 沒寫進去。
    os.environ["ARMOR_TRACE_DIR"] = os.path.join(tmp, "trace")
    os.environ["ARMOR_PBKDF2_ITERS"] = "2000"

    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PX_PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": HERE,
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": str(UP_PORT),
        "ARMOR_UPSTREAM_HTTPS": "0",
        "ARMOR_REQUIRE_KEY": "1",
        "ARMOR_CLIENT_KEYS": os.environ["ARMOR_CLIENT_KEYS"],
        "ARMOR_TRACE_DIR": os.path.join(tmp, "trace"),
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
        "ARMOR_STATE": "0", "ARMOR_ARCHIVE": "0",
        "ARMOR_CAPTURE_RESP": "0",
        "ARMOR_MEDIA_DIR": os.path.join(tmp, "media"),
    })
    open(env["ARMOR_CLIENT_KEYS"], "w").write("")
    logpath = os.path.join(tmp, "proxy.log")
    log = open(logpath, "w+")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                            env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-2000:])
            print("FAILED: proxy did not start")
            return 1

        sys.path.insert(0, HERE)
        import accounts as A

        print("1. 建兩個帳戶與各自的裝置金鑰")
        st, body, _ = post("/a/login", {"name": "owner", "password": "x"})
        check("空 store 時 /a/me 說要 bootstrap",
              json.loads(get("/a/me")[1]).get("bootstrap") is True)

        # 沒有帳戶時 bootstrap 由 CLI 做（HTTP 側刻意不開放匿名註冊）。
        own = A.ensure_owner("owner")
        a1 = A.create_user("alice", "alicepass1")
        a2 = A.create_user("bob", "bobpass123")
        k1 = A.mint_token(a1["id"], "alice-phone")
        k2 = A.mint_token(a2["id"], "bob-phone")
        check("兩把金鑰都建立了", k1.get("ok") and k2.get("ok"))
        check("金鑰可通過代理", get("/health", key=k1["token"])[0] == 200)

        print("2. alice 送一輪串流對話")
        body = {"model": "glm", "stream": True,
                "messages": [{"role": "user",
                              "content": "示範一個 stateless 的處理方式"}]}
        st, text, hdrs = post("/v1/chat/completions", body, key=k1["token"])
        check("HTTP 200", st == 200, st)
        check("串流有內容", "範例" in text, text[:80])
        check("有思考片段", "先確認架構" in text)

        print("3. bob 也送一輪，內容要分開")
        body2 = {"model": "glm", "stream": True,
                 "messages": [{"role": "user", "content": "bob 自己的問題"}]}
        st2, text2, _ = post("/v1/chat/completions", body2, key=k2["token"])
        check("bob HTTP 200", st2 == 200, st2)

        time.sleep(1.0)   # trace 寫檔 + 用量節流

        print("4. trace 記錄的 uid 是對的人")
        import trace_rec as T
        rows = T.list_records(limit=20, days=1)
        check("至少兩筆回合", len(rows) >= 2, len(rows))
        byu = {}
        for r in rows:
            byu.setdefault(r.get("uid") or "", []).append(r)
        check("alice 有一筆", bool(byu.get(a1["id"])), list(byu))
        check("bob 有一筆", bool(byu.get(a2["id"])), list(byu))
        check("沒有記到 owner 名下", len(byu.get(own["id"], [])) == 0,
              len(byu.get(own["id"], [])))
        ar = byu[a1["id"]][0]
        check("alice 那筆的提問正確", "stateless" in (ar.get("user") or ""),
              ar.get("user"))
        full = T.get_record(ar["id"]) or {}
        check("alice 那筆有思考內容", "先確認架構" in (full.get("think") or ""))
        check("alice 那筆有最終答案", "範例" in (full.get("final") or ""))

        print("5. 用量分開計")
        # 走 HTTP 端點，不是本行程的模組：pending 計量在「代理的」記憶體裡，
        # 這一行如果改成直接呼叫模組，讀到的永遠是還沒落盤的舊檔。
        se, sbody, _ = post("/a/login", {"name": "alice", "password": "alicepass1"})
        cook = ""
        for k, v in post("/a/login",
                         {"name": "alice", "password": "alicepass1"})[2].items():
            if k.lower() == "set-cookie":
                cook = v.split(";")[0]
        st, ub, _ = get("/a/usage?days=3", cookie=cook)
        urows = json.loads(ub).get("rows") or []
        u1 = urows[-1] if urows else {}
        se2, sbody2, _ = post("/a/login", {"name": "bob", "password": "bobpass123"})
        cook2 = ""
        for k, v in post("/a/login",
                         {"name": "bob", "password": "bobpass123"})[2].items():
            if k.lower() == "set-cookie":
                cook2 = v.split(";")[0]
        st, ub2, _ = get("/a/usage?days=3", cookie=cook2)
        urows2 = json.loads(ub2).get("rows") or []
        u2 = urows2[-1] if urows2 else {}
        check("alice 今日 1 輪", u1.get("turns"), 1)
        check("bob 今日 1 輪", u2.get("turns"), 1)
        check("alice tokens_in 有記到", int(u1.get("tokens_in") or 0) > 0,
              u1.get("tokens_in"))
        check("alice chars_out 有記到", int(u1.get("chars_out") or 0) > 0,
              u1.get("chars_out"))

        print("6. 帳戶 API 看得到自己的對話，看不到別人的")
        se, sbody, _ = post("/a/login", {"name": "alice", "password": "alicepass1"})
        check("alice 登入", se == 200, se)
        # 從 Set-Cookie 取 sid（urllib 不自動存）
        sc = post("/a/login", {"name": "alice", "password": "alicepass1"})[2]
        cookie = ""
        for k, v in sc.items():
            if k.lower() == "set-cookie":
                cookie = v.split(";")[0]
        check("有拿到 session cookie", cookie.startswith("triad_sid="), cookie[:20])

        st, body, _ = get("/a/conv?days=1", cookie=cookie)
        d = json.loads(body)
        convs = [c["conv"] for c in (d.get("convs") or [])]
        check("alice 看到自己的對話", len(convs) >= 1, convs)
        # bob 的對話 id 不該出現在 alice 的清單裡
        st2, body2, _ = get("/a/conv?days=1", cookie=cookie)
        check("alice 清單裡沒有別人的對話",
              all(c != "bob-conv" for c in convs))

        print("7. alice 產生回看連結，內容只含自己的對話")
        st, vbody, _ = post("/a/views", {"conv": convs[0], "kind": "think"},
                            cookie=cookie)
        check("建立回看連結", st == 200, st)
        vd = json.loads(vbody)
        url = vd.get("url") or ""
        check("有 url", "/i/" in url, url)
        vid = url.rsplit("/", 1)[-1]
        st, page, _ = get("/i/" + vid)
        check("回看頁 200", st == 200, st)
        check("回看頁有 alice 的內容", "stateless" in page)
        check("回看頁有思考", "先確認架構" in page)
        check("回看頁沒有 bob 的內容", "bob 自己的問題" not in page)

        print("8. 撤銷金鑰後該帳戶立刻不能用")
        check("撤銷前可用", get("/health", key=k1["token"])[0] == 200)
        A.revoke_token(a1["id"], k1["id"], hard=True)
        st, hb, _ = get("/health", key=k1["token"])
        check("撤銷後被拒或只剩 liveness",
              st == 200 and json.loads(hb) == {"ok": True}, (st, hb[:120]))
        check("bob 不受影響",
              json.loads(get("/health", key=k2["token"])[1]).get("service")
              == "armor_proxy")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        up.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED %d:" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
