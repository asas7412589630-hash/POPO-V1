#!/usr/bin/env python3
"""Reject runtime state and accidental credentials in a release tree.

Usage:
    python3 tools/check_release_tree.py                 # check the source tree
    python3 tools/check_release_tree.py --allow-runtime  # dev tree: ignore data/, logs/, .git/
    python3 tools/check_release_tree.py /path/to/staging # check exactly what will ship

The release build must run this **without** --allow-runtime against the staged
directory, because a release that contains runtime state is the failure mode.
"""
from __future__ import annotations
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = {".git", "data", "run", "logs", "state", ".venv"}
RUNTIME_NAMES = {"popo.json", "admin_key.txt", "keys.json", "source_secrets.json",
                 "upstream_keys.txt", "client_keys.txt", "model_sources.json",
                 "model_routes.json", "panel_settings.json", "rate_limit.json"}
TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|triad-[A-Za-z0-9_-]{16,})")
PLACEHOLDERS = {"", "sk-replace-me", "replace-me", "your-key-here", "example", "dummy", "test"}


def _scan_json_secrets(value, rel, issues, key=""):
    if isinstance(value, dict):
        for k, child in value.items():
            _scan_json_secrets(child, rel, issues, k)
    elif isinstance(value, list):
        for child in value:
            _scan_json_secrets(child, rel, issues, key)
    elif isinstance(value, str):
        lower_key = key.lower()
        if any(word in lower_key for word in ("key", "secret", "token", "password")):
            if value.lower() not in PLACEHOLDERS:
                issues.append(f"non-placeholder secret field: {rel}:{key}")
        elif TOKEN.search(value) and value.lower() not in PLACEHOLDERS:
            issues.append(f"credential-shaped value: {rel}")


def scan(root: pathlib.Path, allow_runtime: bool = False) -> list[str]:
    issues: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        if rel.parts[0] in RUNTIME_ROOTS:
            if allow_runtime:
                continue
            if path.is_file():
                issues.append(f"runtime/generated path included: {rel}")
            continue
        if not path.is_file():
            continue
        if path.name in RUNTIME_NAMES:
            issues.append(f"runtime/secret config must not ship: {rel}")
        if path.suffix.lower() in {".pyc", ".gz", ".zip", ".so", ".apk"}:
            issues.append(f"generated/binary artifact must not ship: {rel}")
        if path.suffix.lower() == ".json":
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(obj, dict):
                _scan_json_secrets(obj, rel, issues)
        elif path.suffix.lower() in {".txt", ".yaml", ".yml", ".toml"} and "example" not in path.name:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if TOKEN.search(line) and not any(word in line.lower() for word in ("replace-me", "example", "dummy")):
                    issues.append(f"credential-shaped value: {rel}:{lineno}")
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    allow_runtime = "--allow-runtime" in argv
    argv = [a for a in argv if a != "--allow-runtime"]
    root = pathlib.Path(argv[0]).resolve() if argv else ROOT
    issues = scan(root, allow_runtime=allow_runtime)
    if issues:
        print("RELEASE TREE CHECK FAILED")
        print("\n".join(f" - {issue}" for issue in issues))
        return 1
    print(f"release tree hygiene passed: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
