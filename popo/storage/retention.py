"""Retention: delete records older than the operator's policy.

Two rules the caller can rely on:

* **Bounded work.** It reads line by line and stops at the retention cut-off;
  a single multi-gigabyte file cannot be loaded into memory.
* **Never delete what it cannot parse.** A line that will not parse is kept and
  counted. Silently dropping it would mean the audit's data disappears before
  anyone reads it.
"""

from __future__ import annotations
import os
import time
from dataclasses import dataclass, field


@dataclass
class RetentionReport:
    deleted_files: int = 0
    rewritten_files: int = 0
    kept_lines: int = 0
    dropped_lines: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"deleted_files": self.deleted_files,
                "rewritten_files": self.rewritten_files,
                "kept_lines": self.kept_lines,
                "dropped_lines": self.dropped_lines,
                "errors": self.errors}


def _cutoff(days: int) -> float:
    return time.time() - max(1, days) * 86400


def prune_dir(path: str, days: int, suffix: str = ".jsonl") -> RetentionReport:
    """Delete whole files whose newest entry is older than the cut-off.

    Used for date-named trace files: a file either belongs to a day or it does
    not, so there is nothing to rewrite.
    """
    report = RetentionReport()
    if not os.path.isdir(path):
        return report
    limit = _cutoff(days)
    for name in sorted(os.listdir(path)):
        if not name.endswith(suffix):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.getmtime(full) < limit:
                os.unlink(full)
                report.deleted_files += 1
        except OSError as exc:
            report.errors.append(f"{name}: {exc}")
    return report


def prune_jsonl(path: str, days: int, timestamp_key: str = "ts") -> RetentionReport:
    """Rewrite one append-only file, dropping entries older than the cut-off."""
    report = RetentionReport()
    if not os.path.exists(path):
        return report
    limit = _cutoff(days)
    kept: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if not stripped.strip():
                    continue
                stamp = _stamp_of(stripped, timestamp_key)
                if stamp is None:
                    # Unparseable: keep it. Deleting data we do not understand
                    # is how history disappears.
                    kept.append(stripped)
                    report.kept_lines += 1
                    continue
                if stamp >= limit:
                    kept.append(stripped)
                    report.kept_lines += 1
                else:
                    report.dropped_lines += 1
    except OSError as exc:
        report.errors.append(str(exc))
        return report

    if report.dropped_lines == 0:
        return report
    tmp = f"{path}.prune"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            for line in kept:
                handle.write(line + "\n")
        os.replace(tmp, path)
        report.rewritten_files += 1
    except OSError as exc:
        report.errors.append(str(exc))
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return report


def _stamp_of(line: str, key: str) -> float | None:
    import json
    from datetime import datetime
    try:
        row = json.loads(line)
    except ValueError:
        return None
    raw = str(row.get(key, ""))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def prune_records(data_dir: str, days: int) -> RetentionReport:
    """Apply retention across the whole record store."""
    report = prune_dir(os.path.join(data_dir, "runtime", "trace"), days)
    archive = prune_dir(os.path.join(data_dir, "runtime", "archive"), days)
    report.deleted_files += archive.deleted_files
    report.errors.extend(archive.errors)
    for relative in ("logs/requests.jsonl", "state/usage.jsonl"):
        part = prune_jsonl(os.path.join(data_dir, relative), days)
        report.deleted_files += part.deleted_files
        report.rewritten_files += part.rewritten_files
        report.kept_lines += part.kept_lines
        report.dropped_lines += part.dropped_lines
        report.errors.extend(part.errors)
    return report
