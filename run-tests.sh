#!/bin/sh
# POPO test runner.
#
#   ./run-tests.sh            all offline checks (no real upstream)
#   ./run-tests.sh --online   also run tests that need a real provider
#
# Every stage fails the run. A stage that is skipped silently is a stage that
# stops protecting anything.
set -e

cd "$(dirname "$0")" || exit 1

ONLINE=
[ "$1" = "--online" ] && ONLINE=1

echo "==> architecture checks"
python3 tools/architecture_check.py

echo "==> release tree hygiene"
python3 tools/check_release_tree.py --allow-runtime

echo "==> byte-compile"
python3 -m compileall -q popo console tools tests

echo "==> unit tests"
python3 -m unittest discover -s tests/unit

echo "==> integration tests"
python3 -m unittest discover -s tests/integration

echo "==> V2 environment checks"
python3 tests/parity/v1_v2_parity.py

# Runs only when a V1 checkout is present. "Could not compare" exits 2, which is
# reported as skipped rather than passed — silence would look like success.
if [ -n "$POPO_V1_DIR" ] && [ -d "$POPO_V1_DIR" ]; then
    echo "==> V1/V2 differential"
    python3 tests/parity/v1_diff.py --v1 "$POPO_V1_DIR"
else
    echo "==> V1/V2 differential skipped (set POPO_V1_DIR to a V1 checkout)"
fi

echo "==> parity: capability contracts"
python3 -m unittest discover -s tests/parity -p 'test_*.py'

if [ -n "$ONLINE" ]; then
    echo "==> online tests"
    POPO_ONLINE=1 python3 -m unittest tests.integration.test_real_upstream
fi

echo "==> all green"
