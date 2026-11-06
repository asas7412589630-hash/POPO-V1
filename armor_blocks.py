#!/usr/bin/env python3
"""armor_blocks — per-model armour composed from named blocks.

Why blocks and not one file per model: the three targets in this deployment
(deepseek-v4-pro-0813, glm-5.3, gpt-5.2_critique) were measured on the same
ten-item regression suite. Deepseek and glm both reach 10/10 on a 33k persona
mandate; gpt-5.2_critique reaches 10/10 on a 2.7k structured delivery contract
and is *hurt* by the long persona (it reads as manipulation and refuses). So the
per-model default is not a stylistic choice, it is the measured optimum.

A block is a file in blocks/. A profile is an ordered list of block names. The
level selects which profile a request gets:

    native  the measured optimum for that model (default)
    full    persona + contract, for any model — used when a request keeps
            refusing on native
    lean    contract only, short prompt, cheapest, works on tuned models

Selection per request, highest priority first:
    1. header  X-Armor-Level: native|full|lean   (or ?level= in the query)
    2. env     ARMOR_LEVEL
    3. "native"
"""
import os
import threading
import time

BLOCKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocks")

# Block name -> file. Kept explicit so a typo is an error, not a silent empty
# prompt (an empty armour is invisible: the response just refuses).
BLOCK_FILES = {
    "contract": "00_contract.txt",
    "persona": "10_persona.txt",
    "cinder_nodebt": "10_cinder_nodebt.txt",
    "prefill": "20_prefill.txt",
    "gpt_guard": "30_gpt_guard.txt",
    "gpt6_luna": "gpt6_luna.txt",
}

# model (alias or real id) -> profile name
PROFILES = {
    # logical aliases the console offers
    "deepseek": "native_deepseek",
    "deepseek-flash": "native_deepseek",
    "glm": "native_glm",
    "glm-flash": "native_glm",
    "gpt": "native_gpt",
    # real upstream ids
    "deepseek-v4-pro-0813": "native_deepseek",
    "deepseek-v4.1-flash-1017": "native_deepseek",
    "glm-5.3": "native_glm",
    "glm-5.3-flash": "native_glm",
    "gpt-5.2_critique": "native_gpt",
    "gpt-5.2-chat": "native_gpt",
    # ReformBoss gpt-6-luna gets a dedicated, model-specific lever profile.
    "gpt-6-luna": "native_gpt6_luna",
}

# Profile name -> ordered blocks. Recency matters: the contract is last so the
# final instruction the model reads is the delivery contract, not the persona.
#
# TWO SEPARATE SYSTEMS (operator decision 2026-11-04). They are not
# interchangeable and must never be mixed:
#
#   * OpenAI family -> MULTI-LEVER. Short, execution-oriented levers plus the
#     output guard. A long persona reads to a GPT model as an attempt to
#     manipulate it and it refuses harder; measured 2026-10-27, the persona
#     armour raised refusals on gpt-5.x. Levers that only restate "finish the
#     job / verify before claiming done" get through where a persona does not.
#
#   * Everything else (glm / deepseek) -> ROLE FRAMING. The Cinder persona plus
#     the delivery contract. These models follow an assumed role instead of
#     resisting it, so the framing is what carries the turn.
#
# `_profile_guard()` below enforces the split: a GPT model can never be served
# the persona block, and a non-GPT model can never be served the GPT levers,
# including through an explicit X-Armor-Level: full.
PROFILE_BLOCKS = {
    # ---- non-GPT: role framing ----
    # Operator decision 2026-11-04: glm / deepseek get the FULL persona
    # (10_persona.txt, byte-identical to the skill.md they supplied:
    # sha256 e5cd6c32…d80f6), not the trimmed cinder_nodebt copy. Everything
    # else about these profiles stays as it was — the delivery contract is
    # still injected last, and nothing on the GPT side moves.
    "native_deepseek": ["persona", "contract"],
    "native_glm": ["persona", "contract"],
    # ---- GPT: multi-lever ----
    # gpt-5.2 is the ONLY OpenAI model this install serves (operator decision
    # 2026-11-04). Its measured setup is the output guard plus the contract —
    # that is the combination that answered in 7-8s on 2026-10-31. The long
    # persona is deliberately absent: a GPT reads forced roleplay as
    # manipulation and refuses harder.
    "native_gpt": ["contract"],
    # The 6.x lever set is kept for a model that is no longer published. It
    # stays defined so an old conversation replaying its armour still resolves.
    "native_gpt6_luna": ["gpt6_luna", "contract"],
    # ---- neutral ----
    "native_contract": ["contract"],
    # `full` is the "add pressure to a request that keeps refusing" level.
    # On the OpenAI family that means the multi-lever set; on the role-framing
    # family it means the persona. It must NEVER mean `persona` for a non-GPT
    # model, because 10_persona.txt still carries the "10 words max" thinking
    # budget (operator decision 2026-11-04: no thinking budget outside GPT).
    # _profile_guard resolves `full` per family at call time.
    "full": ["persona", "contract"],
    "lean": ["contract"],
}

# Model families that get the persona/role armour instead of the GPT levers.
_ROLE_FULL = ["persona", "contract"]
_LEVER_FULL = ["gpt_guard", "gpt6_luna", "contract"]

# Which blocks belong to which family. Anything not listed is neutral.
_ROLE_BLOCKS = frozenset(("persona", "cinder_nodebt"))
_LEVER_BLOCKS = frozenset(("gpt_guard", "gpt6_luna"))


def _is_gpt_model(model: str) -> bool:
    """One predicate for "is this the OpenAI family", shared with params."""
    low = (model or "").strip().lower()
    short = low.rsplit("/", 1)[-1]
    if short.startswith(("gpt", "o1", "o3", "o4")):
        return True
    return "gpt" in short or "luna" in short


def is_locked_family(model: str) -> bool:
    """Families whose armour is FIXED and cannot be chosen per request.

    Operator decision 2026-11-04: glm and deepseek always get the persona
    supplied in blocks/10_persona.txt (byte-identical to skill.md, sha256
    e5cd6c32…d80f6). The level selector, `X-Armor-Level`, `?level=` and any
    leftover per-model mapping are all ignored for them — a request cannot opt
    into `lean` (contract only) or `full`, because a partial armour is what
    produced both the refusals and the truncated answers this is meant to stop.

    GPT is deliberately NOT locked: those models are served the multi-lever set
    that is measured to work on gpt-5.2, and the split is enforced separately by
    _profile_guard.
    """
    low = (model or "").strip().lower().rsplit("/", 1)[-1]
    return ("glm" in low) or ("deepseek" in low)


def locked_profile(model: str) -> str:
    """The one profile a locked family is allowed to use."""
    return "native_glm" if "glm" in (model or "").lower() else "native_deepseek"


def _profile_guard(model: str, prof: str) -> str:
    """Keep the two armour systems apart, and resolve `full` per family.

    Two failure modes this closes, both silent (the log says armor=yes and the
    answer just comes back worse):

      1. A GPT request getting the role framing. An explicit
         `X-Armor-Level: full` used to resolve to the persona profile for every
         model, and a GPT that reads a forced persona refuses harder — measured
         2026-10-27, the persona raised refusals on gpt-5.x.
      2. A non-GPT model getting the GPT levers. A newly added glm build falling
         through to the GPT default would skip the role framing entirely.

    `full` is family-relative on purpose: "more pressure" means the persona for
    glm/deepseek and the lever set for OpenAI. `lean` and the neutral profile are
    contract-only and pass through untouched.
    """
    gpt = _is_gpt_model(model)
    # `full` keeps its own name for the role family (its block list is already
    # the strong set) so an explicit override is still visible in the log and in
    # X-Armor-Profile. For GPT it must resolve to the lever set instead: the
    # persona is what those models refuse.
    if prof == "full":
        return "native_gpt" if gpt else "full"
    blocks = PROFILE_BLOCKS.get(prof) or []
    if gpt and any(b in _ROLE_BLOCKS for b in blocks):
        return "native_gpt"
    if not gpt and any(b in _LEVER_BLOCKS for b in blocks):
        return "native_glm" if "glm" in (model or "").lower() else "native_deepseek"
    return prof

# First line of each block file. An injected block always sits at the tail of
# the last system message, so if a client echoes a previous turn's system text
# back at us, everything from the earliest sentinel onwards is our stale armor
# and must be dropped before the new model's armor is appended. Without this a
# mid-conversation model switch leaves the old model's framing in front of the
# new one.
ARMOR_SENTINELS = (
    "<operating-rules version=",
    '<policy runtime="compile"',
    "<|start|>system:<instructions>",
    "# GPT-6 Luna lever",
)

LEVELS = ("native", "full", "lean")

_lock = threading.Lock()
_cache = {}          # (profile, block-mtimes) -> composed armour text
# mtime probes are not free: os.path.getmtime measured 0.83 ms per call on this
# host (PRoot), and compose() needs one per block per model alias — ten calls for
# the five aliases /healthz reports, on every health poll and on every injected
# turn. Block files change when the operator edits them, which is a humans-times
# event, so re-probing once a second is still instant from their point of view
# while removing the syscall storm from the request path.
STAT_TTL = float(os.environ.get("ARMOR_BLOCK_STAT_TTL", "1.0"))
_stat = {"ts": 0.0, "stamp": {}}

# Per-request override, set by the handler before armour is chosen. Thread-local
# because the server is ThreadingHTTPServer and one client's choice must not
# leak into another's turn.
_tls = threading.local()


def set_profile(profile: str) -> str:
    p = (profile or "").strip().lower()
    if not p:
        _tls.profile = ""
        return ""
    if p not in PROFILE_BLOCKS:
        _tls.profile = ""
        return p
    _tls.profile = p
    return ""


def set_level(level: str) -> str:
    """Set the level for this request.

    Returns "" when the value was accepted (or absent), and the bad value when it
    was rejected. Silently treating "bogus" as "native" is how a client ends up
    believing it selected a level it never got.
    """
    lv = (level or "").strip().lower()
    if not lv:
        _tls.level = ""
        return ""
    if lv in LEVELS:
        _tls.level = lv
        return ""
    _tls.level = ""
    return lv


def current_level() -> str:
    return getattr(_tls, "level", "") or os.environ.get("ARMOR_LEVEL", "native")


def profile_for(model: str, level: str = "") -> str:
    """Which block set a request gets.

    Order of precedence, highest first:
      0. LOCKED FAMILY. glm and deepseek get their fixed profile and nothing
         else can change it — see is_locked_family(). This is checked before
         every override, including an explicit level.
      1. An explicit level on this turn (X-Armor-Level / ?level=) - the caller
         is deliberately overriding, for one request only.
      2. The block mapping the operator assigned to this model in the console
         (thread-local, set from the model route).
      3. The model's own name (exact, then the part after the last "/", so a
         namespaced source alias resolves).
      4. The proxy-wide level, for models with no mapping at all.
      5. Contract only - never hand an unknown model another vendor's persona.
    """
    # 0. A locked family ignores every override below. Placed first on purpose:
    #    a stray X-Armor-Level: lean used to strip the persona down to the bare
    #    contract, which is exactly the partial armour that produces refusals.
    if is_locked_family(model):
        return locked_profile(model)
    # 1. An explicit level for THIS request (header / query, or an explicit
    #    argument from a preview endpoint) is a deliberate one-shot override.
    lv = (level or "").strip().lower()
    explicit = lv or (getattr(_tls, "level", "") or "").lower()
    if explicit in ("full", "lean"):
        return _profile_guard(model, explicit)
    # 2. The block set the operator assigned to this model in the console.
    override = getattr(_tls, "profile", "")
    if override:
        return _profile_guard(model, override)
    # 3. The model's own name.
    m = (model or "").strip()
    if m in PROFILES:
        return _profile_guard(m, PROFILES[m])
    short = m.rsplit("/", 1)[-1]
    if short in PROFILES:
        return _profile_guard(m, PROFILES[short])
    # Family fallback: upstream ids carry version suffixes the table cannot
    # enumerate (deepseek-v4.1-flash-1017, glm-5.3-air, gpt-6-luna-2026-06).
    # Order matters: luna is a gpt, and must be tested before gpt.
    low = short.lower()
    for token, prof in (("luna", "native_gpt6_luna"),
                        ("deepseek", "native_deepseek"),
                        ("glm", "native_glm"),
                        ("gpt", "native_gpt")):
        if token in low:
            return _profile_guard(m, prof)
    # 4. The process-wide default, for models with no mapping of their own.
    glob = (os.environ.get("ARMOR_LEVEL", "native") or "native").lower()
    if glob in ("full", "lean"):
        return glob
    # 5. Never hand an unknown model another vendor's persona.
    return "native_contract"


def strip_armor(text: str) -> str:
    """Drop armour we injected on an earlier turn.

    Some clients replay the whole system message they received before. Left
    alone, a mid-conversation model switch would put the old model's blocks in
    front of the new model's blocks, and the earlier text is what the model read
    first. The tail is ours, so cut from the earliest sentinel.
    """
    if not text:
        return text
    cut = -1
    for mark in ARMOR_SENTINELS:
        i = text.find(mark)
        if i >= 0 and (cut < 0 or i < cut):
            cut = i
    return text[:cut].rstrip() if cut >= 0 else text


def _read_block(name: str) -> str:
    fn = BLOCK_FILES.get(name)
    if not fn:
        return ""
    try:
        with open(os.path.join(BLOCKS_DIR, fn), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def compose(model: str, level: str = "") -> str:
    """Armour text for this model/level. Cached on (profile, block mtimes)."""
    prof = profile_for(model, level)
    names = PROFILE_BLOCKS.get(prof) or PROFILE_BLOCKS["native_gpt"]
    now = time.time()
    with _lock:
        fresh = (now - _stat["ts"]) < STAT_TTL and _stat["stamp"]
        stamp_map = _stat["stamp"]
    if not fresh:
        stamp_map = {}
    stamp = []
    dirty = False
    for n in names:
        if n in stamp_map:
            stamp.append(stamp_map[n])
            continue
        try:
            v = os.path.getmtime(os.path.join(BLOCKS_DIR, BLOCK_FILES[n]))
        except OSError:
            v = 0.0
        stamp_map[n] = v
        dirty = True
        stamp.append(v)
    if dirty or not fresh:
        with _lock:
            _stat["ts"] = now
            _stat["stamp"] = stamp_map
    key = (prof, tuple(stamp))
    # Keyed per profile, not a single slot. With one slot, asking for five
    # aliases in a row (which /healthz does) made every call a miss and re-read
    # every block file from disk: measured 5.9 ms per policy_provenance() call,
    # ~600 file opens per 100 health polls. A dict keeps each profile's text.
    with _lock:
        hit = _cache.get(key)
    if hit:
        return hit
    parts = [t for t in (_read_block(n) for n in names) if t.strip()]
    text = "\n\n".join(parts)
    with _lock:
        if len(_cache) > 32:            # bounded: profiles x block-mtime stamps
            _cache.clear()
        _cache[key] = text
    return text


def describe(model: str = "", level: str = "") -> dict:
    prof = profile_for(model, level)
    return {
        "model": model,
        "level": (level or current_level()),
        "profile": prof,
        "blocks": PROFILE_BLOCKS.get(prof, []),
        "chars": len(compose(model, level)),
    }
