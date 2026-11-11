"""Static file serving for the console app."""

from __future__ import annotations
import mimetypes
import os
from urllib.parse import unquote

import popo.config as config_module


CONSOLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "console")
# In-package fallback (e.g. pip installs).
_PKG_CONSOLE = os.path.join(os.path.dirname(__file__), "..", "console")


def resolve_path(path: str) -> tuple[str, bool]:
    """Map a URL path under /app/ to a safe local file path.

    Returns (local_path, exists). Paths outside the console directory are
    rejected by collapsing '..' before joining.
    """
    if not path:
        path = "index.html"
    path = unquote(path).lstrip("/")
    # Strip the leading app/ segment if present.
    if path.startswith("app/"):
        path = path[4:]
    if not path:
        path = "index.html"
    # Normalize: split, drop empty/.. components, rebuild.
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    joined = os.path.join(*parts) if parts else "index.html"
    for root in (CONSOLE_DIR, _PKG_CONSOLE):
        candidate = os.path.join(os.path.abspath(root), joined)
        abs_root = os.path.abspath(root)
        # Ensure candidate stays under root.
        if os.path.commonpath([candidate, abs_root]) != abs_root:
            continue
        if os.path.isfile(candidate):
            return candidate, True
    # Return the primary candidate even if missing, so callers can 404 cleanly.
    return os.path.join(os.path.abspath(CONSOLE_DIR), joined), False


def content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def is_console_available() -> bool:
    return os.path.isdir(CONSOLE_DIR) or os.path.isdir(_PKG_CONSOLE)


def get_config_json() -> bytes:
    """Serve a tiny runtime config for the console."""
    cfg = config_module.Config.from_env()
    import json

    body = json.dumps(
        {
            "title": cfg.console_title,
            "version": "2.0.0-alpha.1",
            "require_client_key": cfg.require_client_key,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    return body
