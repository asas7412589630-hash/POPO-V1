#!/usr/bin/env python3
"""Console contract tests — every endpoint the UI calls, offline.

WHY this file exists: the console is served by the same process as the proxy, so
a route that changes shape breaks the UI silently (the page renders, the panel
stays "載入中…", and nothing in the proxy's own tests notices). Each case pins a
field the browser actually reads.

Run: python3 test_console.py
"""
import json
import os
import tempfile
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TRIAD_CONSOLE_TEST_PORT", "8432"))
ROOT = "http://127.0.0.1:%d" % PORT


def _start():
    env = dict(os.environ)
    # ISOLATED DATA. ARMOR_BASE is the install directory, so the alias and route
    # tables would be the REAL ones — and this test creates/deletes sources.
    # Measured 2026-11-06: running the suite deleted the `glm` alias from
    # policy/source/armor_alias.json and left a "../.." route behind, which then
    # broke /effort and /access for every later run.
    tmp = tempfile.mkdtemp(prefix="triad_console_")
    for name, default in (("armor_alias.json", '{"glm":["glm-5.3"],"glm-flash":["glm-5.3-flash"],'
                                              '"deepseek":["deepseek-v4-pro-0813"],'
                                              '"deepseek-flash":["deepseek-v4.1-flash-1017"],'
                                              '"gpt":["gpt-5.2_critique"]}'),
                          ("model_sources.json", '{"sources":[]}'),
                          ("model_routes.json", '{}'),
                          ("source_secrets.json", '{}'),
                          ("panel_settings.json", '{}')):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(default)
    env.update({
        "ARMOR_PROXY_PORT": str(PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": BASE,
        "ARMOR_ALIAS_FILE": os.path.join(tmp, "armor_alias.json"),
        "ARMOR_SOURCES_FILE": os.path.join(tmp, "model_sources.json"),
        "ARMOR_ROUTES_FILE": os.path.join(tmp, "model_routes.json"),
        "ARMOR_SOURCE_SECRETS": os.path.join(tmp, "source_secrets.json"),
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel_settings.json"),
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_REQUIRE_KEY": "0",       # loopback-only test server
    })
    p = subprocess.Popen([sys.executable, os.path.join(BASE, "armor_proxy.py")],
                         env=env, cwd=BASE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.2)
        try:
            if _get("/healthz")[0] == 200:
                return p
        except Exception:
            continue
    p.kill()
    raise RuntimeError("proxy did not come up on %d" % PORT)


def _get(path, headers=None):
    import urllib.request
    req = urllib.request.Request(ROOT + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _json(path, headers=None):
    st, body = _get(path, headers)
    try:
        return st, json.loads(body)
    except Exception:
        return st, {"_raw": body}


CHECKS = []


def check(name, got, want):
    ok = got == want
    CHECKS.append((name, ok, got, want))
    print("%-46s %s" % (name, "PASS" if ok else "FAIL (got %r want %r)" % (got, want)))
    return ok


def main():
    proc = _start()
    try:
        # --- shell assets the HTML references must actually exist -------
        st, html = _get("/app/")
        check("/app/ serves html", st, 200)
        for f in ("app.js", "i18n.js", "log.js", "keys.js",
                  "manifest.webmanifest", "icon.svg"):
            names = ['src="%s' % f, 'href="%s' % f]
            check("index references %s" % f, any(n in html for n in names), True)
            st, _ = _get("/app/" + f)
            check("/app/%s reachable" % f, st, 200)

        # --- endpoints the UI reads ------------------------------------
        st, d = _json("/healthz")
        check("/healthz ok", d.get("ok"), True)
        # The console prints the armour source; a build that reports a file it
        # never reads sends the operator to edit the wrong thing.
        check("/healthz names a policy source",
              d.get("policy_source") in ("blocks", "file"), True)
        if d.get("policy_source") == "blocks":
            check("/healthz policy_file_used is false",
                  d.get("policy_file_used"), False)
            check("/healthz lists per-model profiles",
                  isinstance(d.get("policy_models"), dict) and
                  len(d["policy_models"]) >= 3, True)

        st, d = _json("/access")
        check("/access ok", d.get("ok"), True)
        for k in ("base_url", "origin", "require_key", "enabled_keys",
                  "total_keys", "models"):
            check("/access has %s" % k, k in d, True)
        check("/access models is a list", isinstance(d.get("models"), list), True)

        st, d = _json("/keys")
        check("/keys ok", d.get("ok"), True)
        check("/keys never returns a raw token",
              any("token" in k for k in (d.get("keys") or [{}])[0]), False)

        st, d = _json("/armor?model=glm&level=lean")
        check("/armor ok", d.get("ok"), True)
        check("/armor reports chars", isinstance(d.get("chars"), int), True)
        # glm is a LOCKED family (2026-11-04): every level resolves to the fixed
        # persona profile, so ?level=lean must NOT shrink it to the bare contract.
        check("/armor ignores level for a locked family",
              d.get("profile"), "native_glm")
        check("/armor locked text is the full persona",
              d.get("chars", 0) > 30000, True)

        st, d = _json("/armor?model=gpt-5.2_critique&level=lean")
        check("/armor honours level for gpt",
              d.get("profile"), "lean")

        st, d = _json("/armor/info")
        check("/armor/info ok", d.get("ok"), True)
        check("/armor/info levels", d.get("levels"), ["native", "full", "lean"])
        check("/armor/info model map covers aliases",
              set(d.get("models") or {}) >= {"glm", "gpt"}, True)

        st, d = _json("/effort?model=glm")
        check("/effort ok", d.get("ok"), True)
        check("/effort resolves alias", d.get("resolved"), "glm-5.3")

        # --- trace endpoints: the 3-level drill-down -------------------
        st, d = _json("/trace/clients?days=7")
        check("/trace/clients ok", d.get("ok"), True)
        check("/trace/clients returns a list",
              isinstance(d.get("clients"), list), True)

        st, d = _json("/trace/convs?days=7")
        check("/trace/convs ok", d.get("ok"), True)
        check("/trace/convs returns a list",
              isinstance(d.get("conversations"), list), True)

        st, d = _json("/trace/list?n=5&days=7")
        check("/trace/list ok", d.get("ok"), True)
        check("/trace/list returns a list",
              isinstance(d.get("records"), list), True)
        # The UI lists 7 days of conversations, so the turn list must be able to
        # look back the same distance; a single-day default dead-ends the
        # drill-down on any thread that is not from today.
        convs = (_json("/trace/convs?days=7")[1].get("conversations") or [])
        if convs:
            conv = convs[-1]["conv"]
            st, d = _json("/trace/list?n=5&days=7&conv=" + conv)
            check("drill-down finds the conversation's turns",
                  d.get("total", 0) >= 1, True)
            rid = d["records"][0]["id"]
            st, rec = _json("/trace/get?id=" + rid)
            check("/trace/get returns the record", rec.get("ok"), True)
            check("/trace/get body has final text",
                  "final" in (rec.get("record") or {}), True)

        # alias -> real id: the console shows the model that actually answers, so
        # the map has to be present and consistent with /effort's resolution.
        st, d = _json("/access")
        mm = d.get("model_map") or {}
        check("/access model_map non-empty", bool(mm), True)
        check("/access model_map maps glm", mm.get("glm"), "glm-5.3")
        check("/access models matches model_map keys",
              sorted(d.get("models") or []), sorted(mm))
        check("/access reports tunnel state", "tunnel_ok" in d, True)

        st, d = _json("/logs?n=10")
        check("/logs ok", d.get("ok"), True)
        check("/logs returns lines", isinstance(d.get("lines"), list), True)

        # --- methods a monitor/proxy uses ------------------------------
        import urllib.request
        req = urllib.request.Request(ROOT + "/healthz", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            check("HEAD /healthz", r.status, 200)
            check("HEAD returns no body", r.read(), b"")

        req = urllib.request.Request(ROOT + "/v1/chat/completions", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as r:
            check("OPTIONS preflight", r.status, 204)

        # --- console shell assets the new pages need -------------------
        for f in ("tune.js",):
            check("/app/%s reachable" % f, _get("/app/" + f)[0], 200)
        st, tune = _get("/app/tune.js")
        check("tune.js posts to /panel/config", "/panel/config" in tune, True)
        st, html2 = _get("/app/")
        check("index has a tune view", 'id="view-tune"' in html2, True)
        check("index has a tunnel restart button",
              'id="btnTunnelRestart"' in html2, True)
        check("tune view has retry/context status chips",
              'id="tuneStatus"' in html2, True)
        check("tune view has grouped layout styles",
              "tune-group-title" in html2 and "tune-number-grid" in html2, True)
        check("tune script labels retry policy",
              "重試與評分" in tune and "自動重試" in tune, True)
        check("tune script separates summary state",
              "摘要與狀態保留" in tune, True)

        st, d = _json("/panel/config")
        check("/panel/config ok", d.get("ok"), True)
        check("/panel/config exposes switches",
              isinstance(d.get("switches"), dict) and len(d["switches"]) >= 8, True)
        check("/panel/config exposes numbers",
              isinstance(d.get("numbers"), dict) and len(d["numbers"]) >= 8, True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    bad = [c for c in CHECKS if not c[1]]
    print()
    if bad:
        print("FAILED %d/%d" % (len(bad), len(CHECKS)))
        return 1
    print("ALL PASS (%d checks)" % len(CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
