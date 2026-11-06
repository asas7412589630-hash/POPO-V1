#!/usr/bin/env python3
"""trace_rec 快取正確性測試 — 增量讀取不能漏記錄、不能重複、不能讀半行。"""
import sys, os, tempfile, time, json, shutil
sys.path.insert(0, "/var/hark/shared/armor_triad")

tmp = tempfile.mkdtemp(prefix="trcache_")
os.environ["ARMOR_TRACE_DIR"] = tmp
import trace_rec as T

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %-50s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL  %-50s %s" % (name, extra))


def line(rid, client="c1", conv="v1", text="x" * 200):
    return json.dumps({"id": rid, "ts": time.time(), "client": client,
                       "conv": conv, "model": "m", "user": "u",
                       "final": text, "attempts": []},
                      ensure_ascii=False)


today = T._day_file()

# --- 1. 空目錄 ---------------------------------------------------------------
check("空目錄回空清單", T.list_records(days=7) == [])

# --- 2. 首次寫入 -------------------------------------------------------------
with open(today, "w", encoding="utf-8") as f:
    for i in range(5):
        f.write(line("r%d" % i) + "\n")
rows = T.list_records(days=7)
check("首讀拿到 5 筆", len(rows) == 5, "got %d" % len(rows))

# --- 3. 增量 append ----------------------------------------------------------
with open(today, "a", encoding="utf-8") as f:
    f.write(line("r5") + "\n")
rows = T.list_records(days=7)
check("append 1 筆後拿到 6 筆（無重複）", len(rows) == 6, "got %d" % len(rows))
ids = [r["id"] for r in rows]
check("id 無重複", len(ids) == len(set(ids)))

# --- 4. 半行（寫到一半）------------------------------------------------------
with open(today, "a", encoding="utf-8") as f:
    f.write('{"id": "half", "ts": 1')     # 沒有換行、沒有結尾
rows = T.list_records(days=7)
check("半行不被計入", len(rows) == 6, "got %d" % len(rows))

# --- 5. 補完半行 -------------------------------------------------------------
with open(today, "a", encoding="utf-8") as f:
    f.write(', "client": "c1", "conv": "v1", "final": "y"}\n')
rows = T.list_records(days=7)
check("半行補完後被讀到", len(rows) == 7, "got %d" % len(rows))
check("補完的那筆是 half", any(r["id"] == "half" for r in rows))

# --- 6. 檔案被截斷（rotate）--------------------------------------------------
with open(today, "w", encoding="utf-8") as f:
    f.write(line("new1") + "\n")
rows = T.list_records(days=7)
check("截斷後重讀只有 1 筆", len(rows) == 1, "got %d" % len(rows))
check("截斷後是 new1", rows[0]["id"] == "new1")

# --- 7. 客戶端/對話過濾在快取下仍正確 ----------------------------------------
with open(today, "a", encoding="utf-8") as f:
    f.write(line("a1", client="cA", conv="vA") + "\n")
    f.write(line("b1", client="cB", conv="vB") + "\n")
    f.write(line("a2", client="cA", conv="vB") + "\n")
check("client 過濾 cA = 2", len(T.list_records(client="cA")) == 2)
check("conv 過濾 vB = 2", len(T.list_records(conv="vB")) == 2)
# 檔案此時有 c1(new1) + cA(a1,a2) + cB(b1) = 3 個客戶端
check("clients() 看到 3 個客戶端", len(T.clients()) == 3)
convs = T.conversations(days=7)
# 對話有 v1(new1) + vA(a1) + vB(b1,a2) = 3 段
check("conversations() 看到 3 段對話", len(convs) == 3, "got %d" % len(convs))

# --- 8. get_record 命中尾端最新記錄 -----------------------------------------
rec = T.get_record("a2")
check("get_record 找得到最後一筆", rec is not None and rec["id"] == "a2")
check("get_record 找不到不存在 id", T.get_record("nope") is None)

# --- 9. 重複讀取不會累加 -----------------------------------------------------
for _ in range(20):
    T.list_records(days=7)
check("連讀 20 次仍是 4 筆", len(T.list_records(days=7)) == 4)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
