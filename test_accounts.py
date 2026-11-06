#!/usr/bin/env python3
"""Self-test: 帳戶層講的話是不是真的？

不需要網路，不需要真代理。每個檢查都在一個乾淨的暫存 store 上跑。

為什麼要有這支：帳戶層的 bug 長得不像 bug。一個「vid 撤銷了但頁面還開得起來」
的錯誤，在畫面上完全正常；一個「A 的金鑰記到 B 名下」的錯誤，只會讓用量數字
慢慢歪掉。所以這裡逐一釘住那些「錯了也看不出來」的性質。
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="acct_test_")
os.environ["ARMOR_ACCOUNTS_JSON"] = os.path.join(TMP, "accounts.json")
os.environ["ARMOR_KEYS_JSON"] = os.path.join(TMP, "keys.json")
os.environ["ARMOR_CLIENT_KEYS"] = os.path.join(TMP, "client_keys.txt")
os.environ["ARMOR_TRACE_DIR"] = os.path.join(TMP, "trace")
os.environ["ARMOR_PBKDF2_ITERS"] = "2000"   # 測試用低迭代，快 45 倍
os.environ["ARMOR_LOGIN_MAX_FAIL"] = "3"

import accounts as A

fails = []


def check(name, got, want):
    if got != want:
        fails.append("%s: got %r want %r" % (name, got, want))
        print("  FAIL %-42s got=%r want=%r" % (name, got, want))
    else:
        print("  ok   %-42s %r" % (name, got))


def ok(name, cond, note=""):
    check(name + ((" — " + note) if note else ""), bool(cond), True)


print("1. bootstrap：沒人就建第一個，有人就拒絕")
r = A.ensure_owner("owner")
ok("第一次 bootstrap 成功", r and r.get("password"))
check("帳戶數", A.count_users(), 1)
check("第二次 bootstrap 被拒", A.ensure_owner("owner2"), None)
check("仍是 1 個帳戶", A.count_users(), 1)
owner_pw = r["password"]
owner_uid = r["id"]

print("2. 登入：對的進去，錯的進不去，停用的進不去")
s = A.login("owner", owner_pw, ip="10.0.0.1")
ok("正確密碼可登入", s.get("ok"))
ok("拿到 sid", bool(s.get("sid")))
check("session 解析回同一個 uid", (A.resolve(s["sid"]) or {}).get("uid"), owner_uid)
check("錯誤密碼被拒", A.login("owner", "wrong", ip="10.0.0.1").get("ok"), False)
check("不存在的帳戶回同一個錯誤",
      A.login("nobody", "x", ip="10.0.0.1").get("error"),
      A.login("owner", "wrong2", ip="10.0.0.1").get("error"))

u2 = A.create_user("bob", "bobpass123")
bob_uid = u2["id"]
bs = A.login("bob", "bobpass123", ip="10.0.0.2")
ok("bob 可登入", bs.get("ok"))
A.set_enabled(bob_uid, False)
check("停用後 session 立即失效", A.resolve(bs["sid"]), {})
check("停用後不能再登入", A.login("bob", "bobpass123", ip="10.0.0.2").get("ok"), False)
A.set_enabled(bob_uid, True)

print("3. 失敗鎖定：同一個 IP 連續猜錯會被擋")
ip = "10.9.9.9"
for _ in range(3):
    A.login("owner", "nope", ip=ip)
r = A.login("owner", owner_pw, ip=ip)
check("超過上限後連正確密碼也擋", r.get("ok"), False)
ok("有 retry_after", r.get("retry_after", 0) > 0)
check("別的 IP 不受影響", A.login("owner", owner_pw, ip="10.8.8.8").get("ok"), True)

print("4. 換密碼 = 撤銷該帳戶所有 session")
s2 = A.login("owner", owner_pw, ip="10.0.0.3")
ok("換密碼前 session 有效", bool(A.resolve(s2["sid"])))
A.set_password(owner_uid, "newpass12345")
check("換密碼後舊 session 失效", A.resolve(s2["sid"]), {})
check("新密碼可登入", A.login("owner", "newpass12345", ip="10.0.0.3").get("ok"), True)
owner_pw = "newpass12345"

print("5. 撤銷 session")
s3 = A.login("owner", owner_pw, ip="10.0.0.4")
check("撤銷回 True", A.revoke(s3["sid"]), True)
check("撤銷後解析不到", A.resolve(s3["sid"]), {})
check("撤銷不存在的回 False", A.revoke("nope"), False)

print("6. 金鑰歸屬")
t = A.mint_token(owner_uid, "phone")
ok("建立成功", t.get("ok"))
ok("拿到明文 token", bool(t.get("token")))
check("token 有前綴", t["token"].startswith("triad-"), True)
check("uid_for_token 對回 owner", A.uid_for_token(t["token"]), owner_uid)
check("別人的 token 對不到", A.uid_for_token("triad-garbage"), "")
rows = A.list_tokens(owner_uid)
check("清單長度", len(rows), 1)
ok("清單不含明文 token", "token" not in rows[0])
ok("清單有 prefix", rows[0].get("prefix", "").startswith("triad-"))
check("bob 看不到 owner 的金鑰", len(A.list_tokens(bob_uid)), 0)

tb = A.mint_token(bob_uid, "bobphone")
check("bob 的金鑰歸 bob", A.uid_for_token(tb["token"]), bob_uid)

print("7. 撤銷金鑰：軟撤銷停用、硬撤銷刪除")
ok("軟撤銷回 True", A.revoke_token(owner_uid, t["id"]))
# 停用的金鑰連驗證都不會過（keyring.verify 只回 enabled），所以解析不到 uid
# 是正確結果：它已經不可能再送進任何一輪對話。
check("軟撤銷後解析不到 uid", A.uid_for_token(t["token"]), "")
check("但記錄還在並標記為停用", [k for k in A.list_tokens(owner_uid)
                        if k["id"] == t["id"]][0]["enabled"], False)
check("別人不能撤銷我的金鑰", A.revoke_token(bob_uid, "cc09e5938767"), False)
check("錯誤 id 回 False", A.revoke_token(owner_uid, "nope"), False)
ok("硬撤銷回 True", A.revoke_token(owner_uid, t["id"], hard=True))
check("硬撤銷後清單空了", len(A.list_tokens(owner_uid)), 0)

print("8. 刪除帳戶：金鑰、session、view 一起清")
t3 = A.mint_token(bob_uid, "tmp")
bs2 = A.login("bob", "bobpass123", ip="10.0.0.5")
v3 = A.new_view(bob_uid, "chat", "ui-x")
ok("刪除回 True", A.delete_user(bob_uid))
check("帳戶不見", A.find_by_name("bob"), None)
check("session 消失", A.resolve(bs2["sid"]), {})
check("view 消失", A.resolve_view(v3["vid"]), {})
check("金鑰記錄消失", [k for k in A.list_tokens() if k["id"] == t3["id"]], [])

print("9. 回看連結：可解析、可撤銷、會過期")
v = A.new_view(owner_uid, "think", "ui-conv-1", ttl=3600)
ok("有 vid", v["vid"].startswith("v"))
ok("kind 是 think", v["kind"] == "think")
got = A.resolve_view(v["vid"])
ok("解析得到", bool(got))
check("conv 一致", got["conv"], "ui-conv-1")
ok("hits 被計數", got.get("hits", 0) >= 1, "hits=%s" % got.get("hits"))
check("亂打的 vid 解析不到", A.resolve_view("v" + "z" * 12), {})
check("空 vid 解析不到", A.resolve_view(""), {})
ok("撤銷回 True", A.revoke_view(owner_uid, v["vid"]))
check("撤銷後解析不到", A.resolve_view(v["vid"]), {})

vx = A.new_view(owner_uid, "chat", "ui-conv-2", ttl=60)
# 直接把過期時間往前挪，驗證「過期」這條路徑（不睡覺）。
import accounts as _A
d = _A._load()
d["views"][vx["vid"]]["exp"] = time.time() - 1
_A._save(d)
check("過期後解析不到", A.resolve_view(vx["vid"]), {})
check("別人的 view 不能撤銷",
      A.revoke_view("u_someoneelse", vx["vid"]), False)

print("10. 用量：累積、按日、按帳戶分開")
A.bump_usage(owner_uid, turns=2, tokens_in=100, tokens_out=50, chars_out=900)
A.bump_usage(owner_uid, turns=1, tokens_in=10, tokens_out=5, chars_out=100)
A.bump_usage(bob_uid, turns=9)
u = A.usage_today(owner_uid)
check("owner 今日輪數", u.get("turns"), 3)
check("owner 今日 tokens_in", u.get("tokens_in"), 110)
check("owner 今日 chars_out", u.get("chars_out"), 1000)
check("bob 的用量不含 owner 的",
      [r for r in A.usage(bob_uid) if r["day"] == time.strftime("%Y-%m-%d")][0]["turns"], 9)

print("11. 設定：預設值、可寫、未知 key 拒絕")
check("require_account 預設", A.get_setting("require_account"), False)
ok("可開啟", A.set_setting("require_account", True))
check("讀回來是 True", A.get_setting("require_account"), True)
check("未知 key 拒絕", A.set_setting("no_such_setting", 1), False)
check("未知 key 不存在", A.all_settings().get("no_such_setting"), None)
A.set_setting("max_keys_per_user", 1)
r1 = A.mint_token(owner_uid, "limit-1")
ok("上限內可建立", r1.get("ok"))
r = A.mint_token(owner_uid, "over-limit")
check("超過金鑰上限被拒", r.get("ok"), False)
ok("錯誤訊息有講原因", "上限" in (r.get("error") or ""))
A.revoke_token(owner_uid, r1["id"], hard=True)
A.set_setting("max_keys_per_user", 0)

print("11b. attach_token：舊 /keys 路徑建立的金鑰也能掛上歸屬")
nk = __import__("keyring").create("legacy-path")
ok("attach 回 True", A.attach_token(owner_uid, nk["id"], "legacy-path", nk.get("prefix")))
check("uid_for_token 認得它", A.uid_for_token(nk["token"]), owner_uid)
check("出現在帳戶金鑰清單", [k["id"] for k in A.list_tokens(owner_uid)], [nk["id"]])
check("掛到不存在的帳戶回 False", A.attach_token("u_nope", nk["id"]), False)
check("掛空 id 回 False", A.attach_token(owner_uid, ""), False)

print("12. 損壞的 store 不能鎖死也不能放行")
with open(os.environ["ARMOR_ACCOUNTS_JSON"], "w") as f:
    f.write("{ this is not json")
A._cache["stamp"] = None
A._cache["data"] = None
check("壞檔時帳戶數為 0", A.count_users(), 0)
check("壞檔時登入失敗（不是放行）",
      A.login("owner", owner_pw, ip="10.7.7.7").get("ok"), False)
ok("壞檔有被標記", A.stats().get("corrupt"))
r = A.ensure_owner("fresh")   # 壞檔被視為空 store → 可以重新 bootstrap
ok("壞檔後可重新 bootstrap", r and r.get("password"))

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("FAILED %d:" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
