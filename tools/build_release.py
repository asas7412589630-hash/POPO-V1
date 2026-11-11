#!/usr/bin/env python3
"""Build a reproducible release archive from an allow-list.

Only files that are part of the product ship. Runtime state, install
configuration, logs, caches and byte-compiled artifacts never do — not because
they are git-ignored, but because they are not on this list.

Reproducibility: entries are sorted, mtimes are fixed, ownership is zeroed and
gzip's own timestamp is zeroed. Two builds of the same tree produce the same
SHA-256, which is what makes the published checksum meaningful.
"""
from __future__ import annotations
import argparse
import hashlib
import os
import pathlib
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ("popo", "console", "blocks", "scripts", "tests", "tools", ".github")
INCLUDE_FILES = (
    "README.md", "CHANGELOG.md", "GOALS.md", "PARITY_REAL.md", "FEATURE_MATRIX.md",
    "REWRITE_PLAN.md", "ARCHITECTURE.md", "SECURITY.md", "CONTRIBUTING.md",
    "OPEN_SOURCE_STATUS.md", "LICENSE", "LICENSE.en.md", "LICENSE.zh-Hant.md",
    "popo.example.json", "popo.sh", "run-tests.sh", ".gitignore", ".gitattributes",
)
EXCLUDE_PARTS = {"__pycache__", ".git", "data", "logs", "run", "state", ".venv",
                 ".pytest_cache", ".mypy_cache",
                 # A stashed copy of my earlier console. Shipping two consoles
                 # would be worse than shipping the wrong one.
                 "console_mine_backup", "console_201_backup"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".orig", ".rej", ".bak"}
EXCLUDE_NAMES = {"popo.json", "admin_key.txt", "keys.json", "sessions.json",
                 "accounts.json", "usage.jsonl"}


def collect() -> list[pathlib.Path]:
    selected: list[pathlib.Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            selected.append(path)
    for dirname in INCLUDE_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDE_PARTS for part in relative.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
                continue
            selected.append(path)
    return sorted(set(selected))


def build(output: pathlib.Path, prefix: str) -> tuple[pathlib.Path, str, int]:
    files = collect()
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as raw:
        # mtime=0 keeps the gzip header stable across builds.
        import gzip
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for path in files:
                    relative = path.relative_to(ROOT)
                    info = tar.gettarinfo(str(path), arcname=f"{prefix}/{relative}")
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mode = 0o644 if path.suffix != ".sh" and not relative.parts[0] == "scripts" else 0o755
                    with open(path, "rb") as handle:
                        tar.addfile(info, handle)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return output, digest, len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="")
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    version = args.version or _read_version()
    output = pathlib.Path(args.output) if args.output else pathlib.Path(f"/tmp/POPO-{version}.tar.gz")
    path, digest, count = build(output, f"POPO-{version}")
    print(f"built {path}")
    print(f"files {count}")
    print(f"sha256 {digest}")
    return 0


def _read_version() -> str:
    text = (ROOT / "popo" / "config.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version = "):
            return stripped.split("=", 1)[1].strip().strip('"\'')
    return "0.0.0"


if __name__ == "__main__":
    raise SystemExit(main())
