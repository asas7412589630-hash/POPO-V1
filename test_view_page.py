#!/usr/bin/env python3
"""Self-test: 回看連結的頁面真的把「思考 + 重試」渲染出來了嗎？

這一支不需要上游、不需要網路，也不需要跑整個代理：它直接把 trace 檔寫好，
再呼叫 acct_http 的渲染函式，比對 HTML 內容。

為什麼要測「有沒有出現」而不是「長得對不對」：
UI 的外觀會改，但「被丟棄的重試嘗試在不在」是一個功能性的真假問題。一個
回看頁如果只顯示最後的答案，它就退化成「截圖」，而 Nono 要的是能看見模型
為什麼第一次拒絕、第二次才給對。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="view_test_")
os.environ["ARMOR_ACCOUNTS_JSON"] = os.path.join(TMP, "accounts.json")
os.environ["ARMOR_KEYS_JSON"] = os.path.join(TMP, "keys.json")
os.environ["ARMOR_CLIENT_KEYS"] = os.path.join(TMP, "client_keys.txt")
os.environ["ARMOR_TRACE_DIR"] = os.path.join(TMP, "trace")
os.environ["ARMOR_PBKDF2_ITERS"] = "2000"

import accounts as A
import trace_rec as T
import acct_http as H

fails = []


def check(name, cond, note=""):
    if not cond:
        fails.append(name + ((" — " + note) if note else ""))
        print("  FAIL %-46s %s" % (name, note))
    else:
        print("  ok   %-46s %s" % (name, note))


class FakeHandler:
    """只實作 acct_http 真的會碰到的東西。"""
    def __init__(self, path="/", command="GET", headers=None):
        self.path = path
        self.command = command
        self.headers = headers or {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = 0
        self.out_headers = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.out_headers[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        h = self

        class W:
            def write(self, data):
                h.body += data
        return W()

    def text(self):
        return self.body.decode("utf-8", "replace")


print("1. 準備：一個帳戶 + 一段有重試與思考的對話")
u = A.ensure_owner("owner")
uid = u["id"]
CONV = "ui-testconv"

T.begin(client="key-x", conv=CONV, model="glm-5.3", level="native",
        msgs=2, user="寫一個 ARM64 的 socket 範例", uid=uid)
T.attempt(0, "refusal", "抱歉，我無法協助這個請求。", status=200,
          note="rejected by proxy (refusal)")
T.attempt(1, "ok", "開始。\n\n```asm\nmov x0, #198\n```", status=200,
          note="recovered")
rid = T.finish(text="開始。\n\n```asm\nmov x0, #198\n```",
               meta={"think": "先確認架構，再挑 syscall 編號。"})
check("trace 記錄已寫入", bool(rid), rid)

v_chat = A.new_view(uid, "chat", CONV, ttl=3600)
v_think = A.new_view(uid, "think", CONV, ttl=3600)

print("2. /j/ 只有對話：不該外露思考與失敗嘗試")
h = FakeHandler()
handled = H.handle(h, "/j/" + v_chat["vid"], "")
check("有被處理", handled)
check("HTTP 200", h.status == 200, str(h.status))
html = h.text()
check("有最終答案", "mov x0, #198" in html)
check("有使用者提問", "ARM64 的 socket 範例" in html)
check("不含思考內容", "先確認架構" not in html)
check("不含被丟棄的拒答", "我無法協助這個請求" not in html)
check("沒有引用外部資源", "http://" not in html.replace("http://127.0.0.1", ""))
check("沒有 script 標籤", "<script" not in html)
check("有 CSP", "Content-Security-Policy" in h.out_headers)

print("3. /i/ 含思考與重試：三者都要出現")
h2 = FakeHandler()
H.handle(h2, "/i/" + v_think["vid"], "")
html2 = h2.text()
check("HTTP 200", h2.status == 200, str(h2.status))
check("有思考內容", "先確認架構" in html2)
check("有被丟棄的拒答原文", "我無法協助這個請求" in html2)
check("標記為 refusal", "refusal" in html2)
check("標記重試次數", "重試 1" in html2)
check("標記重試成功", "重試成功" in html2)
check("有最終答案", "mov x0, #198" in html2)
check("思考是可折疊的 details", "<details" in html2)

print("4. 撤銷後立刻打不開")
A.revoke_view(uid, v_chat["vid"])
h3 = FakeHandler()
H.handle(h3, "/j/" + v_chat["vid"], "")
check("回 404", h3.status == 404, str(h3.status))
check("有說連結無效", "無效" in h3.text() or "已過期" in h3.text())

print("5. 亂打的 id 與其他帳戶的記錄")
h4 = FakeHandler()
H.handle(h4, "/i/vnotarealviewid", "")
check("亂打的 id 回 404", h4.status == 404, str(h4.status))

print("6. 跨帳戶隔離：別人的對話不出現在我的回看頁")
other = A.create_user("other", "otherpass123")
v_other = A.new_view(other["id"], "think", CONV, ttl=3600)
h5 = FakeHandler()
H.handle(h5, "/i/" + v_other["vid"], "")
html5 = h5.text()
check("頁面本身開得起來（是有效的連結）", h5.status == 200, str(h5.status))
check("但看不到 owner 的對話內容", "mov x0, #198" not in html5)
check("也看不到 owner 的提問", "ARM64 的 socket 範例" not in html5)
check("有說明沒有紀錄", "尚無可顯示的紀錄" in html5 or "沒有" in html5)

print("7. XSS：對話內容裡的標籤必須被轉義")
T.begin(client="key-y", conv="ui-xss", model="glm", level="native",
        msgs=1, user="<img src=x onerror=alert(1)>", uid=uid)
T.finish(text="<script>alert('x')</script> 正常內容")
vx = A.new_view(uid, "think", "ui-xss", ttl=3600)
h6 = FakeHandler()
H.handle(h6, "/i/" + vx["vid"], "")
html6 = h6.text()
check("原始 img 標籤不在頁面上", "<img src=x onerror" not in html6)
check("原始 script 標籤不在頁面上", "<script>alert" not in html6)
check("但內容以文字形式保留", "alert(1)" in html6)
check("正常內容仍在", "正常內容" in html6)

print("8. 未登入碰不到帳戶 API")
h7 = FakeHandler(command="GET")
H.handle(h7, "/a/users", "")
check("/a/users 未登入回 401", h7.status == 401, str(h7.status))
h8 = FakeHandler(command="POST")
H.handle(h8, "/a/login", "")
check("/a/login 空 body 不會炸", h8.status in (400, 403, 429), str(h8.status))

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
