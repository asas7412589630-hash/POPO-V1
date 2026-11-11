"""
Armor block loader and profile resolution.

Blocks are plain text files; registry.json maps them to profiles and models.
All prompt text lives outside Python.
"""

from __future__ import annotations
import json
import os
import pathlib
from typing import Any


# Resolved against the package location, not the process working directory.
# A relative "blocks" made the armour depend on where the process was started,
# so a tool that chdir'd elsewhere silently loaded a different project's prompt —
# and a comparison against that other project reported "identical" for the wrong
# reason.
DEFAULT_BLOCKS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "blocks")


def load_registry(blocks_dir: str | None = None) -> dict:
    blocks_dir = blocks_dir or DEFAULT_BLOCKS_DIR
    path = os.path.join(blocks_dir, "registry.json")
    if not os.path.exists(path):
        return {"blocks": {}, "profiles": {}, "models": [], "default_profile": "native_contract"}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_block_text(blocks_dir: str, block_name: str) -> str:
    """Load raw block text. Missing blocks return empty string."""
    registry = load_registry(blocks_dir)
    blocks = registry.get("blocks", {})
    if block_name not in blocks:
        return ""
    file_name = blocks[block_name].get("file", f"{block_name}.txt")
    path = os.path.join(blocks_dir, file_name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Two armour systems that must not mix. V1 learned this the hard way: an explicit
# `X-Armor-Level: full` used to resolve to the persona for every model, and a GPT
# that reads a forced persona refuses *harder* (measured 2026-10-27). Conversely a
# glm build falling through to the GPT levers loses the role framing entirely.
# Both failures are silent: the log says armour=yes and the answer is just worse.
_ROLE_BLOCKS = frozenset(("persona", "cinder_nodebt"))
_LEVER_BLOCKS = frozenset(("gpt_guard", "gpt6_luna"))
_ROLE_FULL = ["persona", "contract"]
_LEVER_FULL = ["gpt_guard", "gpt6_luna", "contract"]


def is_gpt_model(model_id: str) -> bool:
    """Whether a model belongs to the OpenAI family."""
    name = (model_id or "").split("/")[-1].lower()
    return name.startswith("gpt") or name.startswith("o1") or name.startswith("o3")


def resolve_full_blocks(model_id: str) -> list[str]:
    """What "more pressure" means for this family.

    Family-relative on purpose: the persona for glm/deepseek/kimi, the lever set
    for OpenAI. A single global answer would be wrong for one of them.
    """
    return list(_LEVER_FULL if is_gpt_model(model_id) else _ROLE_FULL)


def keep_families_apart(model_id: str, blocks: list[str]) -> list[str]:
    """Drop blocks belonging to the other family's system.

    Returns the blocks unchanged when they already belong to the right family, so
    a neutral profile (contract only) passes through untouched.
    """
    gpt = is_gpt_model(model_id)
    forbidden = _ROLE_BLOCKS if gpt else _LEVER_BLOCKS
    return [b for b in blocks if b not in forbidden]


def resolve_profile(model_id: str, registry: dict | None = None, blocks_dir: str | None = None, *, level: str | None = None, profile: str | None = None) -> list[str]:
    """Return the ordered list of block names for a model/level."""
    if registry is None:
        registry = load_registry(blocks_dir)
    profiles = registry.get("profiles", {})
    models = registry.get("models", [])
    default_profile = registry.get("default_profile", "native_contract")
    clean_model = model_id.split("/")[-1]

    def fingerprint_profile() -> str | None:
        # Keep exact model fingerprints ahead of family wildcards even if a
        # future registry edit places a wildcard rule earlier in the file.
        for rule in models:
            match = rule.get("match", "")
            if "*" not in match and clean_model == match:
                return rule.get("profile")
        for rule in models:
            match = rule.get("match", "")
            if match.endswith("-*") and clean_model.startswith(match[:-2]):
                return rule.get("profile")
        return None

    matched_profile = fingerprint_profile()

    # Level overrides.
    levels = registry.get("levels", {})
    if level and level in levels:
        profile_name = levels[level].get("profile")
        if profile_name == "off":
            return []
        if level == "full":
            return resolve_full_blocks(model_id)
        if profile_name and profile_name in profiles:
            return keep_families_apart(model_id, list(profiles[profile_name].get("blocks", [])))

    # Operator-selected per-model profile. Explicit request-level armor levels
    # above still take precedence, preserving the X-Armor-Level contract.
    if profile and profile in profiles:
        selected = profiles[profile]
        # `full` is family-relative: the lever set for GPT, the persona otherwise.
        if profile == "full":
            return resolve_full_blocks(model_id)
        alias = selected.get("alias_of") if isinstance(selected, dict) else None
        if alias in profiles:
            selected = profiles[alias]
        if isinstance(selected, dict):
            # Choosing a family in the console keeps that family as the base,
            # while retaining a measured exact-model delivery overlay when one
            # exists. Explicit generic/off profiles have no family and remain
            # true overrides.
            overlay = profiles.get(matched_profile or "", {})
            if (selected.get("family") and isinstance(overlay, dict)
                    and overlay.get("family") == selected.get("family")
                    and overlay.get("parent") == profile):
                return keep_families_apart(model_id, list(overlay.get("blocks", [])))
            return keep_families_apart(model_id, list(selected.get("blocks", [])))

    # Automatic routing resolves the exact fingerprint first, then family.
    if matched_profile and matched_profile in profiles:
        return keep_families_apart(model_id, list(profiles[matched_profile].get("blocks", [])))

    if default_profile in profiles:
        return list(profiles[default_profile].get("blocks", []))
    return []


def build_system_prompt(model_id: str, blocks_dir: str | None = None, *, level: str | None = None, profile: str | None = None, registry: dict | None = None) -> tuple[str, list[str]]:
    """Concatenate blocks for a model. Returns (prompt_text, block_names_used)."""
    blocks_dir = blocks_dir or DEFAULT_BLOCKS_DIR
    if registry is None:
        registry = load_registry(blocks_dir)
    block_names = resolve_profile(model_id, registry, blocks_dir, level=level, profile=profile)
    if not block_names:
        return "", []
    parts = []
    used = []
    for name in block_names:
        text = load_block_text(blocks_dir or DEFAULT_BLOCKS_DIR, name)
        if text:
            parts.append(text)
            used.append(name)
    return "\n\n".join(parts), used


def strip_sentinel_prefix(content: str, sentinels: list[str]) -> str:
    """Remove a block's sentinel prefix if the client answered back with it."""
    text = content.strip()
    for sentinel in sentinels:
        sentinel = sentinel.strip()
        if not sentinel:
            continue
        if text.startswith(sentinel):
            text = text[len(sentinel):].strip()
    return text
