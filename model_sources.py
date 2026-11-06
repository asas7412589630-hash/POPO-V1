import json, os, re, urllib.request
from urllib.parse import urlparse
# Honor ARMOR_BASE / explicit overrides so a test (or a second install) can
# point the registry at an isolated directory — same convention armor_proxy.py
# already uses. Without this the module silently wrote the live catalogue.
BASE = os.environ.get("ARMOR_BASE") or os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.environ.get("ARMOR_SOURCES_FILE") or BASE + "/proxy/model_sources.json"
SECRETS = os.environ.get("ARMOR_SOURCE_SECRETS") or BASE + "/proxy/source_secrets.json"
CATALOG = os.environ.get("ARMOR_ALIAS_FILE") or BASE + "/policy/source/armor_alias.json"
SOURCE_ID_RE = re.compile(r"^[a-z0-9._-]{1,40}$")

# Single source of truth for block profiles lives in armor_blocks; importing it
# here keeps the console's dropdown and the proxy's validator from drifting.
try:
    import armor_blocks as BLOCKS
except Exception:
    BLOCKS = None

def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default

def _write(path, value, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(value, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode); os.replace(tmp, path)

def sources():
    data = _read(REGISTRY, {"sources": []})
    secrets = _read(SECRETS, {})
    return [{**s, "configured": bool(secrets.get(s["id"]))} for s in data.get("sources", [])]

def save_source(source, token=""):
    sid = str(source.get("id", "")).strip()
    name = str(source.get("name", "")).strip()
    base = str(source.get("base_url", "")).strip().rstrip("/")
    u = urlparse(base)
    if not sid or not name or u.scheme not in ("https", "http") or not u.netloc or u.username or u.password:
        raise ValueError("source needs id, name and clean http(s) base_url")
    if not SOURCE_ID_RE.match(sid):
        raise ValueError("source id must be 1-40 chars of a-z, 0-9, _ or -")
    if u.query or u.fragment:
        raise ValueError("base_url must not carry a query or fragment")
    rows = _read(REGISTRY, {"sources": []})
    # One source per base_url. The id is derived from the URL, but a URL typed
    # as `https://x/v1` and `https://x` produced two ids and therefore two
    # independent route sets — every model then appeared twice in the client's
    # picker (measured 2026-11-05). Matching on the normalised base_url makes
    # re-applying the same upstream an update, not a duplicate.
    def _norm(u: str) -> str:
        return (u or "").rstrip("/").lower()

    dropped = [x for x in rows.get("sources", [])
               if x.get("id") != sid and _norm(x.get("base_url")) == _norm(base)]
    rows["sources"] = [x for x in rows.get("sources", [])
                       if x.get("id") != sid and _norm(x.get("base_url")) != _norm(base)]
    rows["sources"].append({"id": sid, "name": name, "base_url": base})
    _write(REGISTRY, rows, 0o644)
    if dropped:
        # Their routes point at a source that no longer exists; leaving them
        # would make those model ids unresolvable rather than merely absent.
        routefile = _read(ROUTES, {})
        keep = {}
        for alias, cfg in (routefile or {}).items():
            if (cfg or {}).get("source_id") in {d["id"] for d in dropped}:
                continue
            keep[alias] = cfg
        _write(ROUTES, keep, 0o644)
        cat = _read(CATALOG, {})
        for alias in list(cat):
            if alias not in keep:
                cat.pop(alias, None)
        _write(CATALOG, cat, 0o644)
    if token:
        sec = _read(SECRETS, {}); sec[sid] = str(token); _write(SECRETS, sec)
    return next(x for x in sources() if x["id"] == sid)

def discover(sid):
    src = next((s for s in sources() if s["id"] == sid), None)
    if not src: raise ValueError("unknown source")
    token = _read(SECRETS, {}).get(sid, "")
    req = urllib.request.Request(src["base_url"] + "/models", headers={"Accept": "application/json"})
    if token: req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as r: payload = json.load(r)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return sorted({x["id"] for x in rows if isinstance(x, dict) and isinstance(x.get("id"), str)})

ROUTES = os.environ.get("ARMOR_ROUTES_FILE") or BASE + "/proxy/model_routes.json"
def route_for(model):
    return _read(ROUTES, {}).get(model)

def route_token(source_id):
    return _read(SECRETS, {}).get(source_id, "")

def publish(source_id, model_ids, profiles=None):
    src = next((s for s in sources() if s["id"] == source_id), None)
    if not src: raise ValueError("unknown source")
    if not SOURCE_ID_RE.match(str(source_id)):
        raise ValueError("bad source id")
    available = set(discover(source_id)); chosen = sorted(set(map(str, model_ids)))
    if not set(chosen) <= available: raise ValueError("selected model not returned by source")
    aliases = _read(CATALOG, {}); routes = _read(ROUTES, {})
    profile_map = profiles or {}
    allowed_profiles = set(BLOCKS.PROFILE_BLOCKS) if BLOCKS is not None else set()
    allowed_profiles.discard("native_contract")
    if any(v and v not in allowed_profiles for v in profile_map.values()):
        raise ValueError("unknown block profile")
    prefix = source_id + "/"
    for alias in [x for x in routes if x.startswith(prefix)]:
        routes.pop(alias, None); aliases.pop(alias, None)
    # Bare aliases are NOT touched here.
    #
    # This block used to delete every alias whose target came from this source,
    # on the theory that un-ticking a model should remove it under every name.
    # Measured 2026-11-06: running 套用 once deleted the hand-written logical
    # names `glm`, `glm-flash` and `deepseek-flash` from armor_alias.json, so
    # every existing client sending model="glm-flash" started getting a 404 —
    # from a setup action that is supposed to be idempotent.
    #
    # The names a client is already using are data, not a derived view. Only the
    # namespaced alias this function owns (prefix + mid) is managed here; a bare
    # alias now survives publishing and un-publishing.
    _ = available  # kept for the validation above; not used to delete aliases
    for mid in chosen:
        alias = prefix + mid
        aliases[alias] = [mid]
        routes[alias] = {"source_id": source_id, "model_id": mid,
                         "base_url": src["base_url"],
                         "profile": (profiles or {}).get(mid, "")}
        # A bare alias so a client can say model="glm-5.3" instead of
        # "logfare.ai/glm-5.3". Only when the short name is free — never
        # shadow another source's model of the same name.
        if mid not in aliases:
            aliases[mid] = [mid]
            routes[mid] = {"source_id": source_id, "model_id": mid,
                           "base_url": src["base_url"],
                           "profile": (profiles or {}).get(mid, "")}
    _write(CATALOG, aliases, 0o644); _write(ROUTES, routes, 0o644)
    # Per-model result, not just a count. The console has to be able to say
    # "this one worked, that one did not" — a bare total forced the operator to
    # guess which checkbox had taken effect (measured 2026-11-05: after a
    # publish the page looked identical whether it had changed anything).
    dropped = sorted(available - set(chosen))
    return {"ok": True, "published": chosen,
            "published_count": len(chosen),
            "remaining": dropped,
            "aliases": {mid: (prefix + mid) for mid in chosen},
            "routes_total": len(routes)}

def models(sid):
    src = next((s for s in sources() if s["id"] == sid), None)
    if not src: raise ValueError("unknown source")
    routes = _read(ROUTES, {})
    return [{"id": mid, "alias": sid + "/" + mid,
             "enabled": sid + "/" + mid in routes,
             "profile": (routes.get(sid + "/" + mid) or {}).get("profile", "")}
            for mid in discover(sid)]


def published(alias):
    """Is this namespaced alias actually published (enabled) right now?

    A namespaced alias has the shape `<source_id>/<model_id>`. It means "route to
    that source", and it must only work while that pair is in the catalogue and
    enabled — otherwise `srcdemo/beta-9` for an unticked model would silently hit
    the source (or, worse, fall through to the shared provider) and the operator
    would see an answer from a model they deliberately did not publish.

    Returns None for a bare model (no slash) — those are not aliases and the
    caller decides what to do with them.
    """
    a = str(alias or "")
    if "/" not in a:
        return None
    cat = _read(CATALOG, {})
    if a not in cat:
        return False
    ent = cat.get(a)
    # The catalogue stores an alias as the list of upstream ids it forwards to
    # (e.g. {"srcdemo/alpha-1": ["alpha-1"]}), and as a dict in older/other
    # writers. Both shapes mean "published"; an EMPTY list/dict means the entry
    # exists but forwards nowhere, which is not published.
    if isinstance(ent, dict):
        return bool(ent.get("enabled", True)) and bool(ent)
    if isinstance(ent, (list, tuple)):
        return len(ent) > 0
    return bool(ent)
