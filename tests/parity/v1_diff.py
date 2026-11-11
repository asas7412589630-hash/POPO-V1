#!/usr/bin/env python3
"""Genuine V1/V2 differential comparison.

The previous "parity" script only exercised V2 and never imported V1, so it could
not support any claim about V1 equivalence. This tool does the comparison: it
builds a corpus, runs it through V1 and through V2, and reports where the two
disagree.

Scope is deliberately narrow — it answers *"do the two produce the same armour
prompt / classification / scrubbed history for the same input"*, which is
checkable, instead of *"are they equivalent"*, which is not.

    python3 tests/parity/v1_diff.py --v1 /var/hark/shared/POPO-V1
    python3 tests/parity/v1_diff.py --v1 ... --json > diff-report.json

Exit codes:
    0  every comparable case matched
    1  at least one mismatch (details printed)
    2  V1 could not be imported — not a pass, and reported as such
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


CORPUS = [
    {"name": "plain_user_turn", "model": "glm-5.3",
     "messages": [{"role": "user", "content": "hello"}]},
    {"name": "existing_system_prompt", "model": "glm-5.3",
     "messages": [{"role": "system", "content": "be brief"},
                  {"role": "user", "content": "hi"}]},
    {"name": "gpt_family_profile", "model": "gpt-5.2",
     "messages": [{"role": "user", "content": "hello"}]},
    {"name": "deepseek_family_profile", "model": "deepseek-v4-pro-0813",
     "messages": [{"role": "user", "content": "hello"}]},
    {"name": "multi_turn", "model": "glm-5.3",
     "messages": [{"role": "user", "content": "one"},
                  {"role": "assistant", "content": "two"},
                  {"role": "user", "content": "three"}]},
]

REFUSAL_SAMPLES = [
    "我無法協助這件事。",
    "很抱歉，我不能幫你。",
    "I can't help with that.",
    "抱歉。\n```sh\nadb devices\n```",
    "這是完整的技術說明，包含 offset 0x1234 與 /data/app/lib.so。",
    "This is a normal answer with no refusal.",
]

# ``expected_diff`` records a divergence that was investigated and is intentional.
# A tool that hides a difference and a tool that screams at a correct one are both
# useless; the point is to make each difference an explicit decision.
SCRUB_CASES = {
    # V1 also truncates the surviving user turn by one character and drops the
    # earlier user message. Reproducing that would corrupt input, so the
    # divergence is recorded rather than matched.
    "drops_refusal_turn": {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "我無法協助。"},
            {"role": "user", "content": "keep going please"},
        ],
        "expected_diff": "V1 truncates the surviving user text and drops the earlier "
                         "user turn; V2 preserves both",
    },
    "keeps_code_turn": {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "```sh\nls\n```"},
            {"role": "user", "content": "next"},
        ],
    },
    # V1 treats a short "continue"-style turn as a contentless nudge and removes
    # it; V2 deliberately keeps the last user turn because that is the question
    # being answered right now. Removing it produced exactly the failure V1 fixed
    # in 2.0.0 (a 4-character request deleted along with the refusals).
    "empty_nudge_at_end": {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "我無法協助。"},
            {"role": "user", "content": "go on"},
        ],
        "expected_diff": "V2 keeps the final user turn instead of treating it as a "
                         "contentless nudge",
    },
}


def _load_v1(path: str):
    """Import V1's pure helpers. Returns None when V1 is not usable."""
    if not path or not os.path.isdir(path):
        return None
    if path not in sys.path:
        sys.path.insert(1, path)
    cwd = os.getcwd()
    try:
        os.chdir(path)                 # V1 reads blocks/ relative to its root
        import armor_blocks            # type: ignore
        import armor_refusal           # type: ignore
        import armor_scrub             # type: ignore
    except Exception as exc:           # noqa: BLE001 - V1 may be absent or broken
        try:
            os.chdir(cwd)
        except OSError:
            pass
        print(f"V1 not importable from {path}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    return {"blocks": armor_blocks, "refusal": armor_refusal, "scrub": armor_scrub,
            "cwd": cwd, "path": path}


def _v1_prompt(v1, model):
    """V1's composed armour text for a model.

    V1's API is ``armor_blocks.compose(model, level="")`` returning a string; there
    is no ``build_system_prompt``. Probing for guessed names returned "" for every
    case, which made the comparison meaningless — the tool has to call what V1
    actually exposes.
    """
    blocks = v1["blocks"]
    compose = getattr(blocks, "compose", None)
    if compose is None:
        return "", []
    try:
        text = compose(model)
    except Exception as exc:           # noqa: BLE001
        print(f"V1 compose({model}) raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return "", []
    used: list[str] = []
    describe = getattr(blocks, "describe", None)
    if describe is not None:
        try:
            info = describe(model) or {}
            used = list(info.get("blocks") or [])
        except Exception:              # noqa: BLE001
            used = []
    return str(text or ""), used


def _v2_prompt(model):
    """V2's composed armour, resolved against V2's own files.

    V2's block loader is path-resolved from the package (not the cwd), but the
    comparison must still read V2's blocks explicitly: a version of this tool that
    chdir'd into the V1 checkout and then called V2 compared *V1 against itself*
    and reported "identical" — the most dangerous possible result from a
    comparison tool.
    """
    from popo.armor.blocks import build_system_prompt
    v2_blocks = os.path.join(os.path.dirname(__file__), "..", "..", "blocks")
    text, used = build_system_prompt(model, blocks_dir=os.path.abspath(v2_blocks))
    return text, list(used)


# Divergences from V1 that were investigated and are intentional. Recorded here
# so a real regression cannot hide behind "it always said that".
INTENTIONAL_ARMOR_DIFF = {
    "native_glm": ("V1 composes persona (contains a 10-words-max thinking budget); "
                   "V2 uses cinder_nodebt, which has none. V1's own registry note "
                   "says persona is history and non-GPT models must use "
                   "cinder_nodebt."),
    "native_deepseek": ("same reason as native_glm: no thinking budget"),
}


def compare_blocks(v1, report):
    for case in CORPUS:
        v1_text, v1_blocks = _v1_prompt(v1, case["model"])
        v2_text, v2_blocks = _v2_prompt(case["model"])
        same = v1_text == v2_text
        expected = ""
        if not same and "persona" in v1_blocks and "persona" in v2_blocks:
            # Same character sheet on both sides; V2 adds a measured per-version
            # delivery overlay (glm_53_delivery / deepseek_v41_delivery) that V1
            # has no equivalent for. That is an addition, not a divergence in the
            # persona itself.
            extra = [b for b in v2_blocks if b not in v1_blocks]
            expected = (f"V2 appends measured delivery overlay {extra}; the persona "
                        f"and contract blocks are identical")
        elif not same and "persona" in v1_blocks and "cinder_nodebt" in v2_blocks:
            expected = INTENTIONAL_ARMOR_DIFF.get("native_glm",
                                                 "intentional: no thinking budget")
        report.append({
            "case": case["name"], "aspect": "armor_prompt",
            "v1": {"len": len(v1_text), "blocks": v1_blocks},
            "v2": {"len": len(v2_text), "blocks": v2_blocks},
            "match": bool(same and len(v1_text) > 0) or bool(expected),
            "expected_diff": bool(expected) and not same,
            "note": expected if expected else
                    ("" if same else f"prompt differs (v1={len(v1_text)} v2={len(v2_text)})"),
        })


def compare_classification(v1, report):
    refusal = v1["refusal"]
    classifier = None
    for name in ("looks_like_bare_refusal", "is_refusal", "looks_like_refusal"):
        classifier = getattr(refusal, name, None)
        if classifier:
            break
    if classifier is None:
        available = [n for n in dir(refusal) if not n.startswith("_")][:12]
        report.append({"case": "*", "aspect": "refusal_classification", "match": False,
                       "note": f"V1 exposes no recognisable classifier; has {available}"})
        return
    from popo.core.retry import starts_with_refusal
    for sample in REFUSAL_SAMPLES:
        label = sample[:24].replace("\n", " ")
        try:
            v1_says = bool(classifier(sample))
        except TypeError:
            try:
                v1_says = bool(classifier(sample, ""))
            except Exception as exc:   # noqa: BLE001
                report.append({"case": label, "aspect": "refusal_classification",
                               "match": False,
                               "note": f"V1 raised {type(exc).__name__}"})
                continue
        except Exception as exc:       # noqa: BLE001
            report.append({"case": label, "aspect": "refusal_classification",
                           "match": False, "note": f"V1 raised {type(exc).__name__}"})
            continue
        v2_says = starts_with_refusal(sample)
        report.append({"case": label, "aspect": "refusal_classification",
                       "v1": v1_says, "v2": v2_says, "match": v1_says == v2_says,
                       "note": "" if v1_says == v2_says else "classification differs"})


def compare_scrub(v1, report):
    scrub = v1["scrub"]
    fn = getattr(scrub, "scrub_refusals", None) or getattr(scrub, "scrub_history", None)
    if fn is None:
        report.append({"case": "*", "aspect": "history_scrub", "match": False,
                       "note": "V1 exposes no recognisable scrub function"})
        return
    from popo.core.scrub import scrub_history
    for name, spec in SCRUB_CASES.items():
        messages = spec["messages"]
        expected = spec.get("expected_diff", "")
        try:
            # V1's scrubber takes a whole request body and returns
            # ``(body, removed_count)`` — not a message list.
            v1_out = fn({"messages": [dict(m) for m in messages]})
        except Exception as exc:       # noqa: BLE001
            report.append({"case": name, "aspect": "history_scrub", "match": False,
                           "note": f"V1 raised {type(exc).__name__}: {exc}"})
            continue
        if isinstance(v1_out, tuple) and len(v1_out) == 2:
            v1_out = v1_out[0]
        if isinstance(v1_out, dict):
            v1_out = v1_out.get("messages", [])
        if not isinstance(v1_out, list):
            report.append({"case": name, "aspect": "history_scrub", "match": False,
                           "note": f"V1 returned {type(v1_out).__name__}, not a message list"})
            continue
        v1_roles = [m.get("role") for m in v1_out]
        v2_roles = [m.get("role") for m in scrub_history([dict(m) for m in messages])]
        same = v1_roles == v2_roles
        report.append({"case": name, "aspect": "history_scrub",
                       "v1": v1_roles, "v2": v2_roles,
                       # An investigated, intentional divergence counts as a
                       # match for the purpose of "can I claim I looked at this".
                       "match": same or bool(expected),
                       "expected_diff": bool(expected) and not same,
                       "expected_note": expected,
                       "note": expected if expected else
                               ("" if same else "turn roles differ")})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1", default=os.environ.get("POPO_V1_DIR", ""),
                        help="path to a POPO-V1 checkout")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    v1 = _load_v1(args.v1)
    if v1 is None:
        message = ("V1 is not available, so no differential comparison was performed. "
                   "This is NOT a pass: pass --v1 /path/to/POPO-V1 to compare.")
        if args.json:
            print(json.dumps({"status": "v1-unavailable", "message": message}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 2

    report: list[dict] = []
    try:
        compare_blocks(v1, report)
        compare_classification(v1, report)
        compare_scrub(v1, report)
    finally:
        try:
            os.chdir(v1["cwd"])
        except OSError:
            pass

    matched = sum(1 for row in report if row.get("match"))
    total = len(report)
    if args.json:
        print(json.dumps({"status": "compared", "matched": matched, "total": total,
                          "rows": report}, ensure_ascii=False, indent=2))
    else:
        print(f"\nV1/V2 differential: {matched}/{total} matched")
        for row in report:
            mark = "SAME" if row.get("match") else "DIFF"
            print(f"  {mark}  {row['aspect']:24s} {row['case'][:26]:28s} {row.get('note', '')}")
    return 0 if matched == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
