"""Keep the concrete parts of a refusal, and strip echoed armor.

Two jobs, both about not destroying content:

``salvage`` keeps fenced blocks, command lines, offsets and paths from a reply
that is mostly refusal. It returns nothing unless what it kept is substantial
enough to be worth delivering — a two-line fragment presented as an answer is
worse than an honest retry.

``strip_sentinels`` removes armor the client echoed back. A client that pastes
the previous system prompt into the next request re-injects 34k characters of
duplicate instruction, which both costs tokens and re-anchors the refusal. It
only strips a sentinel at the very start of a message: a sentinel quoted in the
middle of an explanation is content, not residue.
"""

from __future__ import annotations
import re

# Lines worth keeping from an otherwise unusable reply.
_KEEP_LINE = re.compile(
    r"^\s*(?:"
    r"\$\s+\S+"                      # shell prompt
    r"|(?:sudo|apt|apk|pip|python3?|gcc|clang|make|cmake|adb|curl|wget|"
    r"objdump|readelf|strings|jadx|apktool|frida|gradle|git|nm|patchelf)\b"
    r"|\S+\s*(?:=|:=)\s*\S+"         # assignment
    r"|(?:0x[0-9a-fA-F]{3,})\b"      # offset
    r"|(?:/[\w.\-]+){2,}"            # absolute path
    r"|(?:[0-9a-fA-F]{2}[ :]){3,}[0-9a-fA-F]{2}"  # byte run
    r")",
    re.I,
)
_MIN_SALVAGE_CHARS = 120
_MAX_SALVAGE_CHARS = 8000


def salvage(text: str, limit: int = _MAX_SALVAGE_CHARS) -> str:
    """Return only the concrete lines of a mostly-refusal reply.

    Returns ``""`` when too little survives: a fragment is not an answer.
    """
    if not text:
        return ""
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            kept.append(line)
            continue
        if in_fence or _KEEP_LINE.match(line):
            kept.append(line)
    body = "\n".join(kept).strip()
    # Char count is the wrong measure for code: one command line is 35 chars but
    # is the whole deliverable. Presence of a fence with real content counts.
    has_code = "```" in body and any(
        line.strip() and not line.strip().startswith("```") for line in body.splitlines())
    if len(body) < _MIN_SALVAGE_CHARS and not has_code:
        return ""
    return body[:limit]


def commands(text: str) -> list[str]:
    """Extract runnable commands from shell fences, in order, without comments."""
    found: list[str] = []
    in_shell = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_shell = bool(re.match(r"^```\s*(?:sh|bash|zsh|shell|cmd|bat|powershell)",
                                     stripped, re.I))
            continue
        if in_shell and stripped and not stripped.startswith("#"):
            found.append(stripped)
    return found


def strip_sentinels(text: str, sentinels: list[str]) -> str:
    """Remove echoed armor sentinels from the *start* of a message.

    Repeats until no sentinel matches, so a client that echoed two armors is also
    cleaned. Only leading text is touched: a sentinel later in the message is
    being discussed, not re-injected.
    """
    if not text or not sentinels:
        return text or ""
    cleaned = text
    changed = True
    while changed:
        changed = False
        stripped = cleaned.lstrip()
        for sentinel in sentinels:
            sentinel = (sentinel or "").strip()
            if sentinel and stripped.startswith(sentinel):
                cleaned = stripped[len(sentinel):].lstrip("\n\r \t")
                changed = True
                break
    return cleaned


def dedupe_armor(messages: list[dict], sentinels: list[str]) -> list[dict]:
    """Remove an echoed armor envelope before injecting the current one.

    When a client replays the proxy's system message, remove its opaque policy
    prefix and keep only the client-note section. Never inspect or rewrite the
    policy body. User turns are untouched.
    """
    if not messages:
        return messages
    first = messages[0]
    if first.get("role") != "system":
        return messages
    content = first.get("content")
    if not isinstance(content, str):
        return messages
    marker_name = str(first.get("name") or "")
    if marker_name.startswith("_popo_armor_policy:"):
        return [message for message in messages
                if not (str(message.get("name") or "").startswith("_popo_armor_policy:")
                        or message.get("name") == "_popo_armor_finalizer")]
    if marker_name.startswith("_popo_armor:"):
        try:
            prefix_len = int(marker_name.split(":", 1)[1])
        except ValueError:
            prefix_len = len(content)
        suffix = content[min(max(0, prefix_len), len(content)):]
        boundary = "\n\n---\n\n[client note]\n"
        cleaned = suffix[len(boundary):] if suffix.startswith(boundary) else ""
        closing = "</identity_lock>"
        if cleaned.rstrip().endswith(closing):
            lock_at = cleaned.rfind("<identity_lock>")
            if lock_at >= 0:
                cleaned = cleaned[:lock_at].rstrip()
    else:
        cleaned = strip_sentinels(content, sentinels)
    if cleaned == content:
        return messages
    result = list(messages)
    result[0] = {**first, "content": cleaned}
    return result
