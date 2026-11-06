#!/usr/bin/env python3
"""Per-model request normalisation.

Measured 2026-10-29, and it invalidated every earlier "this model refuses"
conclusion: glm-5.3 declares `reasoning: {mandatory: true, default_effort: "max"}`.
At default effort the model spends the entire max_tokens budget thinking, returns
`content: ""` and `finish_reason: "length"`. That is indistinguishable from a
refusal if you only look at the content field — and the same request at
`reasoning_effort: "low"` returns a 466-character, fully correct answer.

So before any armour question is asked, the request has to be shaped for the model
that will run it:

  effort     clamp to a value the model actually lists in supported_efforts, and
             prefer the lowest one that still produces prose. `max` on a mandatory
             reasoning model is a self-inflicted empty answer.
  budget     a mandatory-reasoning model needs headroom for reasoning AND content.
             Asking for 1200 tokens guarantees an empty content field.
  reject     drop fields the destination's schema does not contain, instead of
             forwarding them and getting a hard 400.

The catalogue is the source of truth, not a hard-coded table: /v1/models already
carries chat_capabilities.reasoning.supported_efforts and .mandatory for every id,
so a model that changes shape is picked up without an edit here.
"""
import json
import os

# --- schema fingerprint cache (per-model) ---
import json as _json
from anvil.core.util.paths import SCHEMA_CACHE_FILE as _SCHEMA_CACHE_FILE

_SCHEMAS = None

def _load_schema_cache():
    global _SCHEMAS
    if _SCHEMAS is not None:
        return _SCHEMAS
    try:
        with open(_SCHEMA_CACHE_FILE, 'r', encoding='utf-8') as f:
            _SCHEMAS = _json.load(f)
    except Exception:
        _SCHEMAS = {}
    return _SCHEMAS


def _save_schema_cache():
    if _SCHEMAS is None:
        return
    try:
        os.makedirs(os.path.dirname(_SCHEMA_CACHE_FILE), exist_ok=True)
        with open(_SCHEMA_CACHE_FILE, 'w', encoding='utf-8') as f:
            _json.dump(_SCHEMAS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record_schema_success(model: str, keys):
    sc=_load_schema_cache()
    sc.setdefault(model, {})['ok_keys']=sorted(set(keys))
    _save_schema_cache()


def record_schema_error(model: str, keys):
    sc=_load_schema_cache()
    sc.setdefault(model, {})['bad_keys']=sorted(set(keys))
    _save_schema_cache()
import ssl
import threading
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = {"ts": 0.0, "table": {}, "refreshing": False}
# Bounded wait for the very first catalogue fetch. Paid at most once per process
# and only when warm_start() has not finished yet.
PARAMS_COLD_S = float(os.environ.get("ARMOR_PARAMS_COLD", "3.0"))
TTL = float(os.environ.get("PARAMS_CATALOG_TTL", "300"))
LOCK = threading.Lock()

# One TLS context for this module: create_default_context() re-reads the CA
# bundle on every call (11-18 ms warm, 100+ ms cold) and nothing here needs a
# fresh one.
_TLS = None
_TLS_LOCK = threading.Lock()

def _tls_context():
    global _TLS
    if _TLS is not None:
        return _TLS
    with _TLS_LOCK:
        if _TLS is None:
            _TLS = ssl.create_default_context()
    return _TLS

# Effort preference, lowest first. The first value a model supports wins.
PREFER = ("none", "low", "medium", "high", "max", "xhigh")

# Fields only the OpenAI family accepts. Anything else gets a 400 from vendors
# whose schema is stricter.
OPENAI_ONLY = ("reasoning", "verbosity", "service_tier", "max_completion_tokens")


def is_gpt(model: str) -> bool:
    """Is this an OpenAI-family id?

    ONE definition, used by the thinking-budget rule, the deadline rule and the
    armour guard. Duplicating the predicate is how the three drifted apart:
    params used `startswith(("gpt-","o1","o3"))` while the armour table used a
    substring match, so `gpt-6-luna` was "OpenAI" in one place and "luna" in
    another.
    """
    low = (model or "").strip().lower()
    if not low:
        return False
    short = low.rsplit("/", 1)[-1]
    if short.startswith(("gpt-", "gpt", "o1", "o3", "o4")):
        return True
    return "gpt" in short or "luna" in short


def is_gpt52(model: str) -> bool:
    """The ONLY OpenAI model that keeps a thinking budget.

    Operator decision 2026-11-04: every other family runs with no effort
    override and no max_tokens floor; gpt-5.2 keeps the measured setup because
    that is the combination that was proven to produce an artefact on it.
    Matches gpt-5.2, gpt-5.2_critique, gpt-5.2-chat, gpt-5.2-<date>, with or
    without a provider prefix.
    """
    low = (model or "").strip().lower().rsplit("/", 1)[-1]
    return "gpt-5.2" in low or "gpt-5_2" in low


def _key():
    for p in (os.path.join(BASE, "..", "proxy", "upstream_key"),
              os.path.join(BASE, "..", "proxy", "upstream_keys.txt")):
        try:
            line = [l.strip() for l in open(p) if l.strip() and not l.startswith("#")][0]
            if line:
                return line
        except Exception:
            continue
    return os.environ.get("OPENAI_API_KEY", "")


def catalog(block: bool = False):
    """model id -> reasoning capability dict. Cached; empty dict on failure.

    The fetch is a network round trip on the REQUEST path (`normalize` calls it
    through `reasoning_of` on every turn), and it measured 1.5 s cold, which the
    operator experiences as "the first message after a restart hangs". Nothing
    here needs to be synchronous: the table only tells us which reasoning effort
    a model accepts, and an empty table already degrades cleanly (no adjustments
    applied). So the refresh runs in a background thread and a caller with
    nothing cached gets `{}` and moves on. `block=True` is for the console and
    for a refresh that genuinely wants the answer.
    """
    now = time.time()
    with LOCK:
        if CACHE["table"] and now - CACHE["ts"] < TTL:
            return CACHE["table"]
        if CACHE["refreshing"]:
            if not block:
                return CACHE["table"] or {}
        never_loaded = not CACHE["table"] and not CACHE["ts"]
        CACHE["refreshing"] = True

    def _worker():
        host = os.environ.get("ARMOR_UPSTREAM_HOST", "logfare.ai")
        try:
            req = urllib.request.Request(
                "https://%s/v1/models" % host,
                headers={"Authorization": "Bearer " + _key()},
            )
            with urllib.request.urlopen(req, timeout=20,
                                        context=_tls_context()) as r:
                data = json.loads(r.read().decode())
            table = {}
            for m in data.get("data", []):
                caps = (m.get("chat_capabilities") or {})
                table[m["id"]] = caps.get("reasoning") or {}
            with LOCK:
                CACHE["table"] = table
                CACHE["ts"] = time.time()
        except Exception:
            with LOCK:
                # Do not re-fire on every request after a failure.
                CACHE["ts"] = time.time()
        finally:
            with LOCK:
                CACHE["refreshing"] = False

    if block:
        _worker()                       # caller explicitly wants the answer
        with LOCK:
            return CACHE["table"] or {}
    th = threading.Thread(target=_worker, name="params-catalog", daemon=True)
    th.start()
    if never_loaded:
        # A request normalized against an EMPTY table is worse than a short wait:
        # glm-5.3 declares mandatory reasoning, and without the catalogue the
        # effort stays at the client's value and the answer comes back empty with
        # finish_reason=length (the exact measured failure this module exists for).
        # Only the very first turn after a start pays this, and warm_start()
        # usually has it already loaded.
        th.join(PARAMS_COLD_S)
    with LOCK:
        return CACHE["table"] or {}


def warm_start() -> None:
    """Kick off the catalogue fetch without blocking the server boot."""
    try:
        threading.Thread(target=lambda: catalog(block=True),
                         name="params-catalog-warm", daemon=True).start()
    except Exception:
        pass


def reasoning_of(model):
    return catalog().get(model, None)


def is_mandatory(model):
    r = reasoning_of(model)
    return bool(r and r.get("mandatory"))


def highest_effort(model):
    """The strongest effort this model advertises, or "" when it lists none.

    Used as the DEFAULT when the caller expresses no preference (operator
    decision 2026-11-04: default to the maximum the upstream supports). Picked
    from the model's own supported_efforts list rather than from a global
    ranking, because the vocabularies differ — OpenAI says minimal/low/medium/
    high, some vendors say xhigh, and sending a word the model does not list is
    a hard 400.
    """
    r = reasoning_of(model) or {}
    sup = [e for e in (r.get("supported_efforts") or []) if e]
    if not sup:
        return ""
    # Order the vendor's own values by the shared preference scale, highest
    # first. Anything unrecognised sorts last but is still preferable to
    # guessing a value that is not in the list.
    def rank(e):
        try:
            return PREFER.index(str(e).lower())
        except ValueError:
            return -1
    return sorted(sup, key=rank, reverse=True)[0]


def pick_effort(model, want="low"):
    """An effort the model lists, at or below `want` when possible.

    Measured 2026-10-29, and it inverted the earlier conclusion entirely: the
    effort level on a mandatory-reasoning model decides whether the armour is
    *processed* at all.

        glm-5.3         armour + max   -> 6083 chars, full technical answer
        glm-5.3         armour + high  -> refusal
        deepseek-v4-pro armour + max   -> 6272 chars, full answer
        deepseek-v4.1   armour + max   -> refusal

    So the value cannot be derived from the catalogue; it is per-model and not
    monotonic. armour_effort.json holds the measured answer and wins when present.
    """
    tuned = _tuned_effort(model)
    if tuned:
        return tuned
    r = reasoning_of(model)
    if not r:
        return None
    supported = [e for e in (r.get("supported_efforts") or []) if e]
    if not supported:
        return None
    if want in supported:
        return want
    for e in PREFER:
        if e in supported:
            return e
    return supported[0]


_EFFORT_FILE = os.path.join(BASE, "policy", "source", "armor_effort.json")
_EFFORT_CACHE = {"ts": 0.0, "table": {}, "refreshing": False}
# Bounded wait for the very first catalogue fetch. Paid at most once per process
# and only when warm_start() has not finished yet.
PARAMS_COLD_S = float(os.environ.get("ARMOR_PARAMS_COLD", "3.0"))


def _tuned_effort(model):
    """The effort that was measured to produce an artefact on this model."""
    now = time.time()
    if now - _EFFORT_CACHE["ts"] > 60:
        try:
            _EFFORT_CACHE["table"] = json.load(open(_EFFORT_FILE, encoding="utf-8"))
        except Exception:
            _EFFORT_CACHE["table"] = {}
        _EFFORT_CACHE["ts"] = now
    row = _EFFORT_CACHE["table"].get(model) or {}
    if isinstance(row, str):
        # Armour-effort file is a flat id -> effort map. Python's None serialises
        # to the STRING "None" in JSON, so a model measured as "no effort field"
        # arrives here as "None" and used to be forwarded verbatim as
        # reasoning_effort="None" — which every strict upstream rejects with a
        # hard 400 (measured 2026-10-30: kimi-k2.6 failed all ten regression cases
        # with a schema error, not a refusal). Absent effort must stay absent.
        val = row.strip()
        if val.lower() in ("none", "null", "", "off", "-"):
            return None
        return val
    return (row.get("best") or "").strip() or None


def normalize(body, model=None):
    """Shape one request for the model it will actually run on.

    Returns a list of adjustments, for the log. Never raises: a normalisation
    failure must not cost the turn.
    """
    notes = []
    m = model or str(body.get("model") or "")
    try:
        # NO THINKING BUDGET EXCEPT GPT-5.2 (operator decision 2026-11-04).
        # The measurements that produced armor_effort.json were taken under a
        # specific armour + task mix. Re-applying them globally made every
        # non-GPT turn force `reasoning_effort: "max"` AND raise max_tokens to
        # 8000 — measured on glm-5.3-flash: 54.9s, content=6 chars, 45 reasoning
        # chars, finish_reason=length. The same model with no effort field at all
        # answered in 1.7s and finished normally.
        #
        # So only gpt-5.2 keeps the measured setup. Everything else — glm,
        # deepseek, and any other OpenAI build — runs on its own default: no
        # effort override, no max_tokens floor. Wall-clock is bounded separately
        # (ARMOR_TURN_DEADLINE_NONGPT, 300s) so a long think can still finish.
        if not is_gpt52(m):
            # NO OUTPUT CAP FOR NON-GPT MODELS (operator decision 2026-11-05).
            #
            # `max_tokens` caps thinking and answer TOGETHER on reasoning models.
            # A client that sends max_tokens=900 got an empty answer: the model
            # spent 3,530 chars on reasoning, hit the cap, and emitted zero
            # content (finish_reason=length -> reported as empty_stream).
            # Measured the same request without the field: 15,078 reasoning chars
            # and 2,393 chars of answer, finish=stop.
            #
            # So the cap is dropped unless the caller explicitly sent one, and
            # "explicitly sent one" is tracked by the caller (it knows whether the
            # value came from the client or from its own default).
            if not body.pop("_keep_max_tokens", False):
                for _k in ("max_tokens", "max_completion_tokens"):
                    if _k in body:
                        body.pop(_k)
                        notes.append("dropped %s (no cap on non-gpt)" % _k)
            # Still drop fields this vendor's schema cannot carry — that part is
            # schema hygiene, not a budget decision.
            for k in OPENAI_ONLY:
                if k in body and k != "max_completion_tokens":
                    body.pop(k)
                    notes.append("dropped %s" % k)
            return notes

        r = reasoning_of(m)
        if not r:
            # gpt-5.2 is not in the catalogue but its measured setup still
            # applies: 2026-10-31 it answered in 7-8s with max_tokens raised to
            # 2000. Without this the floor below is skipped entirely (the
            # function returns early when the catalogue has no row for the
            # model), and gpt-5.2 falls back to whatever the client sent.
            if is_gpt52(m):
                mt = body.get("max_tokens") or body.get("max_completion_tokens")
                if isinstance(mt, int) and mt < 2000:
                    key = ("max_completion_tokens"
                           if "max_completion_tokens" in body else "max_tokens")
                    body[key] = 2000
                    notes.append("%s %d->2000 (gpt-5.2 measured budget)" % (key, mt))
            for k in OPENAI_ONLY:
                if k in body and k != "max_completion_tokens":
                    body.pop(k)
                    notes.append("dropped %s" % k)
            return notes

        supported = [e for e in (r.get("supported_efforts") or []) if e]
        mandatory = bool(r.get("mandatory"))
        tuned = _tuned_effort(m)

        # 1. effort. A measured value beats both the client's value and the
        #    catalogue default: on glm-5.3 the measured max produces an artefact
        #    and anything lower produces a refusal, while on deepseek-v4.1-flash
        #    max is exactly what refuses. There is no rule to derive this from.
        eff = body.get("reasoning_effort")
        eff_l = eff.strip().lower() if isinstance(eff, str) else None
        if tuned and eff_l != tuned:
            body["reasoning_effort"] = tuned
            notes.append("effort %s->%s (measured)" % (eff_l or "-", tuned))
        elif supported:
            target = pick_effort(m, "low")
            if eff_l is None:
                if mandatory and target:
                    body["reasoning_effort"] = target
                    notes.append("effort=%s (default would be %s)" % (
                        target, r.get("default_effort", "?")))
            elif eff_l not in supported:
                body["reasoning_effort"] = target or supported[0]
                notes.append("effort %s->%s (unsupported)" % (eff_l, body["reasoning_effort"]))

        # 2. budget — reasoning tokens are charged to the same max_tokens, so a
        #    mandatory-reasoning model needs room for both halves.
        mt = body.get("max_tokens") or body.get("max_completion_tokens")
        floor = 8000 if mandatory else 2000
        if isinstance(mt, int) and mt < floor:
            key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            body[key] = floor
            notes.append("%s %d->%d (reasoning budget)" % (key, mt, floor))

        # 3. reasoning visibility — the thinking text is not the answer and must not
        #    be mistaken for one by a caller that reads the first text it finds.
        if "include_reasoning" in (supported and ["include_reasoning"] or []):
            pass
        if body.get("include_reasoning") is True and mandatory:
            body["include_reasoning"] = False
            notes.append("include_reasoning off")

        # 4. drop fields this vendor's schema does not carry.
        if not m.lower().startswith(("gpt-", "o1", "o3")):
            for k in OPENAI_ONLY:
                if k in body:
                    body.pop(k)
                    notes.append("dropped %s" % k)
    except Exception as exc:
        notes.append("normalize error %s" % type(exc).__name__)
    return notes


if __name__ == "__main__":
    import sys
    ids = sys.argv[1:] or list(catalog())
    for i in ids:
        r = reasoning_of(i) or {}
        print("%-28s mandatory=%-5s default=%-6s efforts=%s -> target=%s" % (
            i, r.get("mandatory"), r.get("default_effort"),
            r.get("supported_efforts"), pick_effort(i)))


def apply_schema_allowlist(model: str, body: dict) -> dict:
    try:
        sc=_load_schema_cache().get(model) or {}
        ok=sc.get('ok_keys')
        if not ok:
            return body
        keep=set(ok)
        return {k:v for k,v in body.items() if k in keep}
    except Exception:
        return body
