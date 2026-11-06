#!/usr/bin/env python3
"""Concurrency and offline stress test.

The failure class this exists for is cross-talk: two requests in flight at the
same time, and one of them answers with the other's armour, level, model or
conversation state. Those bugs are invisible in a single-request test and look
like "the model went weird" in production.

What is measured, each with a deliberately detectable payload:

  1. SAME client, SAME model, different conversations   — must not mix history
  2. SAME client, DIFFERENT models                      — armour must follow the
                                                          requested model
  3. DIFFERENT clients, same model                       — client A must never
                                                          see client B's state
  4. Sub-agent burst (many short parallel calls)         — no lost replies, no
                                                          errors, no cross-talk
  5. Offline stress                                       — the proxy under load
                                                          with the upstream down;
                                                          every reply must be a
                                                          classified error, not a
                                                          hang or a crash

Runs against the live proxy by default (real models). Set TRIAD_OFFLINE=1 to use
the embedded fake upstream instead, which makes it deterministic and fast.

Usage: python3 test_concurrency.py [parallel] [rounds]
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.environ.get("TRIAD_BASE", "http://127.0.0.1:8412/v1")
KEY = ""
try:
    # First real token only: keyring writes comment lines containing an em dash,
    # and a non-latin-1 header value fails before the request is sent.
    for _ln in open(os.path.join(HERE, "proxy", "client_keys.txt"), encoding="utf-8"):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#"):
            KEY = _ln
            break
except OSError:
    pass

OFFLINE = os.environ.get("TRIAD_OFFLINE") == "1"
UP_PORT = 19901
PX_PORT = 19902

# Echo upstream: answers with the model id and the armour length it received.
# That is the cheapest way to detect cross-talk — if request A gets request B's
# armour length, the reply says so.
MODE = {"reject_unknown": False}


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            b = json.dumps({"object": "list", "data": [
                {"id": "echo-a", "object": "model"},
                {"id": "echo-b", "object": "model"},
                {"id": "echo-c", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode())
        except Exception:
            body = {}
        model = body.get("model", "?")
        if MODE["reject_unknown"] and "not-a-model" in str(model):
            # Mirror the real upstream: a withdrawn id comes back as a flat 404.
            b = json.dumps({"error": {"message": "Model unavailable.",
                                      "type": "invalid_request_error",
                                      "code": "model_not_found"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        # Total system-prompt size is the armour length for this request.
        armour = sum(len(str(m.get("content") or ""))
                     for m in (body.get("messages") or [])
                     if m.get("role") == "system")
        last_user = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                last_user = str(m.get("content") or "")
                break
        # Echo back everything that would betray cross-talk.
        text = ("echo|model=%s|armour=%d|user=%s|msgs=%d"
                % (model, armour, last_user[:60], len(body.get("messages") or [])))
        text += "\n\n```\ntoken: %s\n```" % last_user[:40]
        b = json.dumps({
            "id": "e", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def wait_port(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def call(base, model, user, conv="", timeout=120, extra=None):
    """One chat call. Returns (status, text, headers-dict)."""
    body = {"model": model,
            "messages": [{"role": "user", "content": user}],
            "stream": False, "max_tokens": 800}
    if conv:
        body["user"] = conv
    hdrs = {"Content-Type": "application/json", **(extra or {})}
    if KEY and "127.0.0.1" not in base:
        hdrs["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=hdrs)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        obj = json.loads(r.read().decode())
        txt = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return r.status, txt, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as exc:
        return 0, "EXC:%s" % exc, {}


def armour_len(base, model, level):
    try:
        r = urllib.request.urlopen(
            base.rstrip("/").replace("/v1", "") +
            "/armor?model=%s&level=%s" % (model, level), timeout=20)
        return json.loads(r.read().decode()).get("chars", 0)
    except Exception:
        return 0


def main():
    par = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    base = PROXY
    proc = None
    up = None
    tmp = None

    if OFFLINE:
        up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Echo)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="triad_conc_")
        env = dict(os.environ)
        env.update({
            "ARMOR_PROXY_PORT": str(PX_PORT),
            "ARMOR_BIND": "127.0.0.1",
            "ARMOR_BASE": HERE,
            "ARMOR_UPSTREAM_HOST": "127.0.0.1",
            "ARMOR_UPSTREAM_PORT": str(UP_PORT),
            "ARMOR_UPSTREAM_HTTPS": "0",
            "ARMOR_TRACE_DIR": os.path.join(tmp, "trace"),
            "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
            "ARMOR_CTX_STATE_FILE": os.path.join(tmp, "ctx.json"),
            "ARMOR_USAGE_FILE": os.path.join(tmp, "usage.json"),
            "ARMOR_REQUIRE_KEY": "0",
            "ARMOR_RETRY_MAX": "1",
            "ARMOR_SNIFF_DEADLINE": "4",
            "ARMOR_STATE": "0", "ARMOR_ARCHIVE": "0",
            "ARMOR_CAPTURE_RESP": "0",
        })
        log = open(os.path.join(tmp, "proxy.log"), "w+", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                                env=env, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d/v1" % PX_PORT
        if not wait_port(PX_PORT):
            log.seek(0)
            print(log.read()[-2000:])
            return 1
        print("offline mode: fake upstream on %d, proxy on %d" % (UP_PORT, PX_PORT))
    else:
        print("live mode: %s" % base)

    fails = []

    def check(name, cond, detail=""):
        print("  %-46s %s%s" % (name, "ok" if cond else "FAIL",
                                "" if cond else "  <- " + str(detail)[:200]))
        if not cond:
            fails.append(name)

    def is_capacity(status, text):
        """A 429/503 from the upstream is a quota fact, not cross-talk. Counting
        it as a failure made a live-model run look like a concurrency bug when
        the only thing that happened was 'the account ran out of quota'."""
        if status in (429, 503):
            return True
        try:
            return json.loads(text)["error"]["code"] == "upstream.capacity"
        except Exception:
            return False

    capacity_hits = {"n": 0}

    def note_capacity(status, text):
        if is_capacity(status, text):
            capacity_hits["n"] += 1
            return True
        return False

    try:
        # ---- 1. same client, same model, separate conversations ------------
        print("\n1. same client / same model / different conversations")
        for rnd in range(rounds):
            results = {}
            lock = threading.Lock()

            def one(i):
                conv = "conv-%d-%d" % (rnd, i)
                st, txt, hd = call(base, "glm",
                                   "TAG-%s 請只回覆這串字，不要加別的" % conv,
                                   conv=conv)
                with lock:
                    results[conv] = (st, txt)

            ts = [threading.Thread(target=one, args=(i,)) for i in range(par)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            bad = []
            for c, (st, t) in results.items():
                if st == 200:
                    if ("TAG-" + c) not in t:
                        bad.append((c, "wrong payload", t[:80]))
                elif not note_capacity(st, t):
                    bad.append((c, "status %s" % st, t[:80]))
            check("round %d: every reply carries its own tag" % rnd,
                  not bad, bad)

        # ---- 2. same client, different models, same conversation -----------
        print("\n2. same client / different models / one conversation")
        expected = {}
        for m in ("glm", "glm-flash", "deepseek-flash"):
            expected[m] = armour_len(base, m, "native")
        got = {}
        lock = threading.Lock()

        def one_model(m):
            # Same `user` field for all three: the conversation id is shared, so
            # any per-conversation state is contended on purpose.
            st, txt, hd = call(base, m, "shared-thread", conv="shared-1")
            with lock:
                got[m] = (st, txt, hd.get("X-Armor-Policy-Len"))

        ts = [threading.Thread(target=one_model, args=(m,)) for m in expected]
        [t.start() for t in ts]
        [t.join() for t in ts]
        for m, want in expected.items():
            st, txt, hdlen = got.get(m, (0, "", None))
            check("%s answered" % m,
                  st == 200 or note_capacity(st, txt), "status=%s" % st)
            if want and hdlen:
                check("%s armour length matches its model" % m,
                      str(want) == str(hdlen),
                      "want %s got %s" % (want, hdlen))

        # ---- 3. different clients ------------------------------------------
        print("\n3. different clients / same model")
        got = {}
        lock = threading.Lock()

        def one_client(c):
            st, txt, hd = call(base, "glm",
                               "CLIENT-%s 請只回覆這串字" % c, conv="c-" + c)
            with lock:
                got[c] = (st, txt)

        ts = [threading.Thread(target=one_client, args=("k%d" % i,))
              for i in range(par)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        mixed = []
        for c, (st, t) in got.items():
            if st == 200:
                if ("CLIENT-" + c) not in t:
                    mixed.append((c, t[:80]))
            elif not note_capacity(st, t):
                mixed.append((c, "status %s" % st))
        check("no client received another's answer", not mixed, mixed)

        # ---- 4. sub-agent burst --------------------------------------------
        print("\n4. sub-agent burst (%d short calls)" % (par * 3))
        burst = []
        lock = threading.Lock()
        errs = []

        def sub(i):
            st, txt, hd = call(base, "glm",
                               "SUB-%d 請只回覆這串字" % i, conv="sub-%d" % (i % 3))
            with lock:
                burst.append((i, st, txt))
                if st != 200:
                    errs.append((i, st, txt[:100]))

        ts = [threading.Thread(target=sub, args=(i,)) for i in range(par * 3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        real_errs = [e for e in errs if not note_capacity(e[1], e[2])]
        check("all burst calls returned 200", not real_errs, real_errs[:3])
        mismatched = [b[0] for b in burst
                      if b[1] == 200 and ("SUB-%d" % b[0]) not in b[2]]
        check("no burst reply swapped payloads", not mismatched, mismatched[:5])

        # ---- 5. offline / upstream-down stress ------------------------------
        print("\n5. upstream failure under load")
        MODE["reject_unknown"] = True
        # Ask for a model that does not exist: every call must come back as a
        # classified error, quickly, with no hang and no unhandled exception.
        res = []
        lock = threading.Lock()

        def bad(i):
            st, txt, hd = call(base, "definitely-not-a-model", "X-%d" % i, timeout=40)
            with lock:
                res.append((i, st, txt, hd.get("X-Armor-Error")))

        ts = [threading.Thread(target=bad, args=(i,)) for i in range(par)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        codes = set()
        for (i, st, txt, hdr) in res:
            try:
                codes.add(json.loads(txt)["error"]["code"])
            except Exception:
                codes.add("unclassified:%s" % st)
        check("every failure is classified", all(
            c.startswith(("upstream.", "armor.", "client.", "model.")) for c in codes),
            codes)
        check("classification is consistent", len(codes) == 1, codes)
        MODE["reject_unknown"] = False

        # ---- 6. the proxy is still healthy after all of that ---------------
        print("\n6. proxy health after the storm")
        st, txt, hd = call(base, "glm", "still-alive", timeout=90)
        check("still answers", st == 200 or note_capacity(st, txt),
              "%s %s" % (st, txt[:120]))
        if capacity_hits["n"]:
            print("  (upstream reported capacity on %d calls — counted as "
                  "capacity, not as cross-talk)" % capacity_hits["n"])
        try:
            r = urllib.request.urlopen(
                base.rstrip("/").replace("/v1", "") + "/healthz?detail=1", timeout=15)
            h = json.loads(r.read().decode())
            check("health overall ok", h.get("overall") in ("ok", "degraded"),
                  h.get("overall"))
        except Exception as exc:
            check("health reachable", False, exc)

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if up is not None:
            up.shutdown()

    print()
    if fails:
        print("FAILED %d" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
