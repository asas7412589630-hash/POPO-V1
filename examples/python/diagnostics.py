#!/usr/bin/env python3
"""Read the proxy's diagnostics — the part most clients hide.

Worth knowing about three endpoints:

  GET /healthz              liveness + pool state + how much armour is loaded
  GET /armor?model=…        the EXACT text a model would receive this turn
  GET /routes               base URLs and key count

`/armor` is the useful one when an answer looks wrong: you can read what the
model was actually told, instead of inferring it from the reply.

Standard library only — this example needs no packages.
"""
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("PROXY", "http://127.0.0.1:8412")
KEY = os.environ.get("API_KEY", "")


def get(path: str):
    req = urllib.request.Request(BASE + path)
    if KEY:
        req.add_header("Authorization", "Bearer " + KEY)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main() -> int:
    st, health = get("/healthz")
    print("healthz:", st, "ok=%s" % health.get("ok"))
    if st == 200 and health.get("client_keys") is not None:
        print("  client keys :", health.get("client_keys"))
        print("  require key :", health.get("require_key"))
        print("  max conc    :", health.get("max_concurrent"))
        models = health.get("policy_models") or {}
        print("  armour per model family:")
        for fam, d in models.items():
            print("    %-16s %-16s %6d chars  %s"
                  % (fam, d.get("profile"), d.get("chars") or 0,
                     " + ".join(d.get("blocks") or [])))

    # What would this model actually be told?
    st, armor = get("/armor?model=glm-5.3&level=native")
    if st == 200:
        text = armor.get("armor") or armor.get("prompt") or ""
        print("\narmor for glm-5.3: %d chars" % len(text))
        print("  first line: %r" % (text.strip().splitlines()[0][:70] if text.strip() else ""))
    else:
        print("\narmor:", st, "— needs a client key (set API_KEY)")

    st, routes = get("/routes")
    if st == 200:
        print("\nroutes:", json.dumps(routes, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
