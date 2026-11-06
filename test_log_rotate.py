#!/usr/bin/env python3
"""tracelog 輪轉 + 句柄快取測試。"""
import sys, os, tempfile, shutil, time, json
tmp = tempfile.mkdtemp(prefix="trlog_")
os.environ["ARMOR_SPLIT_LOG_DIR"] = tmp
os.environ["ARMOR_SPLIT_LOG_MAX"] = "2000"      # 強制快速輪轉
os.environ["ARMOR_SPLIT_LOG_KEEP"] = "2"
sys.path.insert(0, "/var/hark/shared/armor_triad")
import tracelog as TL

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  PASS  %-46s %s" % (name, extra))
    else:
        fail += 1; print("  FAIL  %-46s %s" % (name, extra))

check("MAX_BYTES 讀到 2000", TL.MAX_BYTES == 2000, str(TL.MAX_BYTES))
check("KEEP 讀到 2", TL.KEEP == 2, str(TL.KEEP))

TL.begin("cA", "v1")
for i in range(200):
    TL.event("line %03d %s" % (i, "x" * 40))

all_log = os.path.join(tmp, "_all.log")
check("_all.log 存在", os.path.exists(all_log))
size = os.path.getsize(all_log)
check("_all.log 受 MAX_BYTES 限制", size <= 2100, "size=%d" % size)

# 輪轉檔存在
rot = [p for p in os.listdir(tmp) if p.startswith("_all.log.")]
check("產生了輪轉檔", len(rot) >= 1, str(sorted(rot)))
check("輪轉檔不超過 KEEP 個", len(rot) <= 2, str(sorted(rot)))

# 內容不遺失（輪轉後新寫入落在主檔）
before = open(all_log, encoding="utf-8").read()
TL.event("AFTER_ROTATE_MARKER")
after = open(all_log, encoding="utf-8").read()
check("輪轉後新行進主檔", "AFTER_ROTATE_MARKER" in after and "AFTER_ROTATE_MARKER" not in before)

# 最後幾行在輪轉檔裡
tail = ""
for r in sorted(rot):
    try:
        tail += open(os.path.join(tmp, r), encoding="utf-8").read()
    except Exception:
        pass
check("輪轉檔保有先前的行", "line 199" in tail or "line 199" in after, "")

# 句柄快取：把上限拉高，確認連續寫入不重開檔案
TL.MAX_BYTES = 10 ** 9
TL.event("warm")
check("句柄快取有條目", len(TL._fh) >= 1, "n=%d" % len(TL._fh))
fh_id = id(TL._fh[all_log])
opens = [0]
_real_open = open
import builtins
def _counting_open(*a, **kw):
    if a and str(a[0]).endswith("_all.log"):
        opens[0] += 1
    return _real_open(*a, **kw)
builtins.open = _counting_open
try:
    for i in range(50):
        TL.event("burst %d" % i)
finally:
    builtins.open = _real_open
check("50 次寫入沒有重開檔案", opens[0] == 0, "reopens=%d" % opens[0])
check("句柄仍是同一個", id(TL._fh.get(all_log)) == fh_id)

# close_all
TL.close_all()
check("close_all 清空快取", TL._fh == {})

# 關閉後仍可繼續寫（重開）
TL.event("after close_all")
check("close_all 後仍能寫", "after close_all" in open(all_log, encoding="utf-8").read())

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
