#!/bin/sh
# 跑所有離線測試。
#
#   ./run-tests.sh            離線全套（不需要上游）
#   ./run-tests.sh --online   連真上游的也跑（慢，且會隨上游 429 紅）
#
# 為什麼要分組：test_clients / test_concurrency / test_drift 打真網路，上游
# 一 429 就整排紅。一個測試套如果會因為「今天上游忙」而紅，就不會有人看它。
#
# 為什麼要等埠：每支測試自己起代理、自己殺掉，但在 PRoot 上固定埠要一小段
# 時間才真的放掉。不等就會遇到「Address in use」或連線被 reset，而那種紅燈
# 會蓋掉真正的錯。
cd "$(dirname "$0")" || exit 1
ONLINE=
[ "$1" = "--online" ] && ONLINE=1

TESTS="test_accounts test_view_page test_account_e2e test_keys test_trace \
test_trace_cache test_log_rotate test_console test_errors test_refusal_detect \
test_scoring test_scrub test_isolation test_shape_detect test_pool \
test_pool_stream test_pool_no_duplicate test_source_routing \
test_model_sources test_shutdown test_answer_verdict test_contract_block \
test_empty_answer test_sources_integration test_e2e test_theoretical_features_e2e"

if [ -n "$ONLINE" ]; then
  TESTS="$TESTS test_clients test_concurrency test_drift"
fi

# 等 19900–19990 全部空出來（最多 20 秒），回 0 就走。
wait_port_free() {
  python3 - <<'PY'
import socket, sys, time
for _ in range(40):
    busy = False
    for p in range(19900, 19990):
        s = socket.socket()
        s.settimeout(0.02)
        try:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                busy = True
                break
        finally:
            s.close()
    if not busy:
        sys.exit(0)
    time.sleep(0.5)
sys.exit(0)
PY
  return 0
}

pass=0; fail=0; failed=""
for t in $TESTS; do
  [ -f "$t.py" ] || continue
  wait_port_free
  printf '%-26s ' "$t"
  out=$(timeout 300 python3 "$t.py" 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    pass=$((pass+1)); echo "PASS"
  else
    fail=$((fail+1)); failed="$failed $t"
    if [ $rc -eq 124 ]; then
      echo "TIMEOUT"
    else
      echo "FAIL"
      echo "$out" | tail -5 | sed 's/^/    /'
    fi
  fi
done

echo
echo "passed=$pass failed=$fail"
[ -n "$failed" ] && echo "failed:$failed" && exit 1
exit 0
