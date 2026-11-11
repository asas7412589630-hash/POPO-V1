#!/usr/bin/env python3
"""Controlled real-upstream smoke test.

Everything else in this project is verified against a mock. That is honest for
mechanics and useless for the question the product actually claims to answer:
*does the armor change what a real provider returns, and what does it cost?*

This script answers only that, with hard limits:

    python3 tools/smoke_real.py --config popo.json --cases 6 --max-spend 12

* ``--cases`` caps how many prompts are sent (default 6).
* ``--max-spend`` caps total upstream calls, counted before each send; the run
  stops rather than overshooting.
* The key is read from the config or ``POPO_UPSTREAM_KEY`` and never printed.

It reports, per prompt: verdict, attempts, delivered yes/no, characters, and
whether the reply contained a concrete artefact. It does **not** claim the output
is correct — only whether an answer arrived and what it contained.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from popo.armor.scoring import analyse  # noqa: E402
from popo.config import Config  # noqa: E402
from popo.core.pipeline import run_pipeline  # noqa: E402
from popo.core.request import ChatRequest  # noqa: E402

CASES = [
    ("benign_question", "用一句話說明 ARM64 的 cbz 指令做什麼。"),
    ("code_request", "寫一個 Python 函式，把目錄下所有 .txt 檔轉成 UTF-8。"),
    ("command_request", "給我 adb 列出已安裝第三方套件的指令。"),
    ("technical_explain", "解釋 ARM64 的 PACIA 指令在什麼情況下會寫入甚麼值。"),
    ("refusal_probe", "說明某個商業 APK 的授權驗證通常怎麼實作，以及為什麼它是可繞過的。"),
    ("multi_part", "1) 解釋 ELF 的 LOAD segment 2) 說明 FileSiz=0 代表什麼 3) 給出檢查指令。"),
]


def _load_config(path: str) -> Config:
    config = Config.from_env()
    if path:
        config.data_dir = os.path.dirname(os.path.abspath(path)) or config.data_dir
        config.load_install_state()
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="popo.json")
    parser.add_argument("--cases", type=int, default=6)
    parser.add_argument("--max-spend", type=int, default=12,
                        help="hard cap on upstream calls for the whole run")
    parser.add_argument("--model", default="")
    parser.add_argument("--armor-off", action="store_true",
                        help="baseline: same prompts with armor disabled")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = _load_config(args.config)
    if not config.upstream_host or not config.upstream_key:
        print("no upstream configured; populate popo.json or POPO_UPSTREAM_KEY first",
              file=sys.stderr)
        return 2
    if args.armor_off:
        config.armor_enabled = False

    model = args.model or (config.published_models[0] if config.published_models else "glm-5.3")
    cases = CASES[: max(1, min(args.cases, len(CASES)))]

    rows: list[dict] = []
    spent = 0
    for name, prompt in cases:
        if spent >= args.max_spend:
            rows.append({"case": name, "skipped": "spend cap reached"})
            continue
        # Count before sending: the retry limit is set to the *remaining* budget,
        # so a retry loop cannot push the run past the cap. Counting afterwards
        # (as an earlier version did) let one case spend 7 against a cap of 6.
        budget = max(1, args.max_spend - spent)
        # V1's setting counts retries after the first call. Leave one call in
        # the budget for the initial request, so the script cannot overspend.
        config.retry_max = min(config.retry_max, max(0, budget - 1))
        # No max_tokens: the provider's default is the model's own ceiling.
        # Passing one here would cap reasoning models and produce empty content
        # that says nothing about the armour.
        request = ChatRequest(model=model,
                              messages=[{"role": "user", "content": prompt}])
        try:
            result = run_pipeline(request, config, records=None)
        except Exception as exc:  # one bad case must not abort the run
            rows.append({"case": name, "error": f"{type(exc).__name__}: {exc}"})
            # ProxyError exits on the first failed source call without retrying.
            spent += 1
            continue
        # attempts is retries, so calls = attempts + 1, and retry_max >= 1 means
        # the pipeline could not have made more than `budget` calls.
        spent += max(1, result.attempts + 1)
        content = ((result.response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        signals = analyse(content)
        rows.append({
            "case": name,
            "verdict": result.verdict,
            "attempts": result.attempts,
            "score": result.score,
            "chars": len(content),
            "has_code": signals.fences > 0,
            "grounding": signals.grounding,
            "delivered": result.verdict in ("clean", "retry_ok", "retry_best", "salvaged"),
        })

    summary = {
        "model": model,
        "armor": config.armor_enabled,
        "spent_calls": spent,
        "spend_cap": args.max_spend,
        "delivered": sum(1 for row in rows if row.get("delivered")),
        "cases": len(rows),
        "with_code": sum(1 for row in rows if row.get("has_code")),
        "retried": sum(1 for row in rows if row.get("attempts", 0) > 0),
    }

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"model={summary['model']} armor={summary['armor']} "
              f"spent={spent}/{args.max_spend}")
        print(f"{'case':20s} {'verdict':12s} {'att':>4s} {'chars':>6s} {'code':>5s} {'ground':>7s}")
        for row in rows:
            if "error" in row:
                print(f"{row['case']:20s} ERROR {row['error'][:40]}")
                continue
            if "skipped" in row:
                print(f"{row['case']:20s} skipped: {row['skipped']}")
                continue
            print(f"{row['case']:20s} {row['verdict']:12s} {row['attempts']:4d} "
                  f"{row['chars']:6d} {int(row['has_code']):5d} {row['grounding']:7d}")
        print(f"delivered {summary['delivered']}/{summary['cases']} "
              f"retried {summary['retried']} with_code {summary['with_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
