#!/usr/bin/env python3
"""run_tests.py — Cross-platform test suite runner for POPO-V1 / Triad.

Runs all offline test suites in clean, isolated subprocesses, waiting for test
ports (19900–19990) to release between suites.

Usage:
  python run_tests.py             Run all offline test suites
  python run_tests.py --online    Include live-provider integration suites
"""
import os
import sys
import time
import socket
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

OFFLINE_TESTS = [
    "test_accounts",
    "test_view_page",
    "test_account_e2e",
    "test_keys",
    "test_trace",
    "test_trace_cache",
    "test_log_rotate",
    "test_console",
    "test_errors",
    "test_refusal_detect",
    "test_scoring",
    "test_scrub",
    "test_isolation",
    "test_shape_detect",
    "test_pool",
    "test_pool_stream",
    "test_pool_no_duplicate",
    "test_source_routing",
    "test_model_sources",
    "test_shutdown",
    "test_answer_verdict",
    "test_contract_block",
    "test_empty_answer",
    "test_sources_integration",
    "test_e2e",
    "test_clients",
    "test_concurrency",
    "test_theoretical_features_e2e",
]

ONLINE_TESTS = [
    "test_drift",
]


def wait_port_free(start=19900, end=19990, max_wait=20.0):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        busy = False
        for p in range(start, end):
            s = socket.socket()
            s.settimeout(0.02)
            try:
                if s.connect_ex(("127.0.0.1", p)) == 0:
                    busy = True
                    break
            except Exception:
                pass
            finally:
                s.close()
        if not busy:
            return True
        time.sleep(0.3)
    return False


def main():
    online = "--online" in sys.argv
    suite = list(OFFLINE_TESTS)
    if online:
        suite.extend(ONLINE_TESTS)

    print(f"Running {'online + offline' if online else 'offline'} test matrix ({len(suite)} suites)...")
    print("=" * 65)

    passed = 0
    failed = []

    for t in suite:
        test_file = os.path.join(HERE, f"{t}.py")
        if not os.path.isfile(test_file):
            print(f"SKIP  {t:<35} (file not found)")
            continue

        wait_port_free()
        sys.stdout.write(f"{t:<35} ")
        sys.stdout.flush()

        t0 = time.time()
        try:
            res = subprocess.run(
                [sys.executable, test_file],
                cwd=HERE,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8",
                errors="replace"
            )
            dt = time.time() - t0
            if res.returncode == 0:
                print(f"PASS ({dt:.2f}s)")
                passed += 1
            else:
                print(f"FAIL (rc={res.returncode}, {dt:.2f}s)")
                failed.append((t, res.stdout, res.stderr))
                # Print last 5 lines of output
                lines = (res.stderr or res.stdout).strip().splitlines()
                for ln in lines[-5:]:
                    print(f"    {ln}")
        except subprocess.TimeoutExpired:
            print("TIMEOUT (>300s)")
            failed.append((t, "", "Timeout expired after 300 seconds"))

    print("=" * 65)
    print(f"Summary: {passed} passed, {len(failed)} failed")
    if failed:
        print("Failed suites:", ", ".join(f[0] for f in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
