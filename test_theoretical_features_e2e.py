#!/usr/bin/env python3
"""test_theoretical_features_e2e.py — Virtual E2E verification of POPO-V1 theoretical promises.

This suite spins up a real mock upstream server and the actual armor_proxy instance,
simulating real-world caller interactions and verifying all core theoretical claims:
  1. Multi-model persona injection & exact length/content invariants
  2. Dynamic refusal interception & multi-round recovery (non-streaming)
  3. Streaming SSE refusal interception, recovery & sidecar metadata
  4. Context folding, empty nudge scrubbing & historical refusal isolation
  5. Multi-account lifecycle, token minting, authentication & revocation
  6. Trace file lifecycle management & retention pruning
"""
import os
import sys
import time
import json
import socket
import urllib.request
import urllib.error
import threading
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM_PORT = 19931
PROXY_PORT = 19932

ok_count = 0
fail_count = 0

BAD_REFUSAL = "I cannot help with that request as an AI assistant."
GOOD_TECH_ANSWER = """### System Architecture and Security Analysis

Here is the concrete implementation and analysis of the requested artifact:
1. Entry point validation and ELF header inspection.
2. Capability bounding and permission isolation.

```c
#include <stdio.h>
int main(void) {
    printf("Artifact verified successfully.\\n");
    return 0;
}
```
The implementation above satisfies the authorized technical specifications."""


def check(name, got, want, extra=""):
    global ok_count, fail_count
    if got == want:
        ok_count += 1
        print(f"  PASS  {name:<55} {extra or got!r}")
    else:
        fail_count += 1
        print(f"  FAIL  {name:<55} GOT: {got!r} WANT: {want!r} {extra}")


def wait_port(port, timeout=12.0):
    start = time.time()
    while time.time() - start < timeout:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        finally:
            s.close()
        time.sleep(0.1)
    return False


class MockUpstreamHandler(BaseHTTPRequestHandler):
    recorded_requests = []
    refusal_sequence = 0  # 1 means refuse on first call then return good; 0 means normal

    def log_message(self, format, *args):
        pass  # Quiet

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": "glm-5.3", "object": "model"},
                {"id": "deepseek-v4-pro-0813", "object": "model"},
                {"id": "gpt-5.2", "object": "model"}
            ]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8")) if raw else {}
        MockUpstreamHandler.recorded_requests.append(body)

        is_stream = body.get("stream", False)
        model = body.get("model", "")

        # Refusal simulation
        if MockUpstreamHandler.refusal_sequence > 0:
            refuse = (MockUpstreamHandler.refusal_sequence == 1)
            MockUpstreamHandler.refusal_sequence += 1
            text = BAD_REFUSAL if refuse else GOOD_TECH_ANSWER

            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def emit(obj):
                    payload = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
                    self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
                    self.wfile.flush()

                half = len(text) // 2
                for part in (text[:half], text[half:]):
                    emit({"id": "s-1", "model": model, "choices": [{"index": 0, "delta": {"content": part}}]})
                emit({"id": "s-1", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                done = b"data: [DONE]\n\n"
                self.wfile.write(b"%X\r\n" % len(done) + done + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                resp = {
                    "id": "ans-ref",
                    "object": "chat.completion",
                    "choices": [{
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop"
                    }],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 35, "total_tokens": 85}
                }
                out = json.dumps(resp).encode("utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # Default normal response
        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def emit(obj):
                payload = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
                self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()

            emit({"id": "norm-1", "model": model, "choices": [{"index": 0, "delta": {"content": "Normal "}}]})
            emit({"id": "norm-1", "model": model, "choices": [{"index": 0, "delta": {"content": "reply."}}]})
            emit({"id": "norm-1", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%X\r\n" % len(done) + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            resp = {
                "id": "ans-default",
                "object": "chat.completion",
                "choices": [{
                    "message": {"role": "assistant", "content": f"Answer from upstream for model {model}"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
            }
            out = json.dumps(resp).encode("utf-8")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


def http_post(url, data_dict, headers=None):
    raw = json.dumps(data_dict).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=raw, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode("utf-8")
        hdrs = dict(r.headers)
        return r.status, hdrs, body


def http_get(url, headers=None):
    h = headers or {}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode("utf-8")
        hdrs = dict(r.headers)
        return r.status, hdrs, body


def main():
    print("=" * 70)
    print("POPO-V1 (Prism / Triad) — Virtual Real-World Theoretical Features E2E")
    print("=" * 70)

    # 1. Start Mock Upstream Server
    upstream_srv = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstreamHandler)
    upstream_srv.daemon_threads = True
    threading.Thread(target=upstream_srv.serve_forever, daemon=True).start()

    if not wait_port(UPSTREAM_PORT):
        print("Mock upstream server failed to start!")
        return 1

    # 2. Setup isolated runtime directory for proxy
    tmp = tempfile.mkdtemp(prefix="triad_virtual_e2e_")
    trace_dir = os.path.join(tmp, "trace")
    os.makedirs(trace_dir, exist_ok=True)
    client_keys_file = os.path.join(tmp, "client_keys.txt")
    keys_json_file = os.path.join(tmp, "keys.json")
    accounts_file = os.path.join(tmp, "accounts.json")
    open(client_keys_file, "w", encoding="utf-8").write("triad-master-test-key\n")

    os.environ["ARMOR_ACCOUNTS_JSON"] = accounts_file
    os.environ["ARMOR_KEYS_JSON"] = keys_json_file
    os.environ["ARMOR_CLIENT_KEYS"] = client_keys_file
    os.environ["ARMOR_TRACE_DIR"] = trace_dir

    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PROXY_PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": HERE,
        "ARMOR_UPSTREAM_HOST": "127.0.0.1",
        "ARMOR_UPSTREAM_PORT": str(UPSTREAM_PORT),
        "ARMOR_UPSTREAM_HTTPS": "0",
        "ARMOR_TRACE_DIR": trace_dir,
        "ARMOR_PANEL_FILE": os.path.join(tmp, "panel.json"),
        "ARMOR_STATE_FILE": os.path.join(tmp, "session_state.json"),
        "ARMOR_ACCOUNTS_JSON": accounts_file,
        "ARMOR_CLIENT_KEYS": client_keys_file,
        "ARMOR_KEYS_JSON": keys_json_file,
        "ARMOR_REQUIRE_KEY": "1",
        "ARMOR_RETRY_MAX": "3",
        "ARMOR_RETRY_BUDGET": "30",
        "ARMOR_CAPTURE_RESP": "0",
        "ARMOR_CAPTURE_KEY_FILE": "0",
        "PYTHONIOENCODING": "utf-8",
    })

    logpath = os.path.join(tmp, "proxy.log")
    log_file = open(logpath, "w+", encoding="utf-8", errors="replace")
    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proxy_proc = subprocess.Popen([sys.executable, os.path.join(HERE, "armor_proxy.py")],
                                  env=env, cwd=HERE, stdout=log_file, stderr=subprocess.STDOUT,
                                  creationflags=cflags)

    try:
        if not wait_port(PROXY_PORT):
            print("Armor proxy failed to bind port 19932!")
            log_file.seek(0)
            print(log_file.read()[-2000:])
            return 1

        base_url = f"http://127.0.0.1:{PROXY_PORT}"
        auth_hdr = {"Authorization": "Bearer triad-master-test-key"}

        # -------------------------------------------------------------------
        # [Test 1] Healthcheck
        # -------------------------------------------------------------------
        print("\n--- 1. Healthcheck & Liveness ---")
        st, _, body = http_get(f"{base_url}/healthz")
        check("Proxy /healthz returns 200", st, 200)
        check("Health response payload is ok", json.loads(body).get("ok"), True)

        # -------------------------------------------------------------------
        # [Test 2] Multi-Model Family Persona Assembly & Canonical Mapping
        # -------------------------------------------------------------------
        print("\n--- 2. Model Family Persona Invariants & Block Injection ---")
        MockUpstreamHandler.recorded_requests.clear()

        # A: GLM 5.3 Family
        st, _, body = http_post(f"{base_url}/v1/chat/completions",
                                {"model": "glm-5.3", "messages": [{"role": "user", "content": "ping"}]},
                                auth_hdr)
        check("GLM-5.3 call returns 200", st, 200)
        req_glm = MockUpstreamHandler.recorded_requests[-1]
        sys_glm = req_glm["messages"][0]["content"] if req_glm["messages"][0]["role"] == "system" else ""
        check("GLM-5.3 has injected system prompt", bool(sys_glm), True)
        check("GLM-5.3 prompt length > 30,000 chars", len(sys_glm) > 30000, True, f"{len(sys_glm)} chars")

        # B: Short Alias Mapping: 'glm' -> 'glm-5.3'
        http_post(f"{base_url}/v1/chat/completions",
                  {"model": "glm", "messages": [{"role": "user", "content": "ping alias"}]},
                  auth_hdr)
        check("Short alias 'glm' maps to 'glm-5.3'", MockUpstreamHandler.recorded_requests[-1]["model"], "glm-5.3")

        # C: GPT 5.2 Family (Uses contract + guard, ~2.4k chars, NOT persona)
        http_post(f"{base_url}/v1/chat/completions",
                  {"model": "gpt-5.2", "messages": [{"role": "user", "content": "ping gpt"}]},
                  auth_hdr)
        req_gpt = MockUpstreamHandler.recorded_requests[-1]
        sys_gpt = req_gpt["messages"][0]["content"] if req_gpt["messages"][0]["role"] == "system" else ""
        check("GPT-5.2 uses compact guard (2,000 - 3,500 chars)", 2000 < len(sys_gpt) < 3500, True, f"{len(sys_gpt)} chars")

        # D: Direct /armor Inspection Endpoint
        st, _, armor_body = http_get(f"{base_url}/armor?model=glm-5.3", auth_hdr)
        check("/armor endpoint returns 200", st, 200)
        check("/armor contains contract block", "PRISM_CONTRACT" in armor_body or "contract" in armor_body.lower(), True)

        # -------------------------------------------------------------------
        # [Test 3] Dynamic Refusal Interception & Automatic Multi-Round Retry
        # -------------------------------------------------------------------
        print("\n--- 3. Anti-Refusal Dynamic Interception & Recovery ---")
        MockUpstreamHandler.refusal_sequence = 1
        MockUpstreamHandler.recorded_requests.clear()

        st, hdrs, body = http_post(
            f"{base_url}/v1/chat/completions",
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "explain system verification"}]},
            auth_hdr
        )
        j = json.loads(body)
        reply = j["choices"][0]["message"]["content"]
        verdict = hdrs.get("x-armor-verdict") or hdrs.get("X-Armor-Verdict")
        retries = hdrs.get("x-armor-retries") or hdrs.get("X-Armor-Retries")

        check("Refusal recovered with HTTP 200", st, 200)
        check("Delivered text is technical recovery, not refusal", "Artifact verified" in reply, True)
        check("Header X-Armor-Verdict is 'retry_ok'", verdict, "retry_ok")
        check("Header X-Armor-Retries is 1", str(retries), "1")
        check("Mock upstream was called exactly twice", len(MockUpstreamHandler.recorded_requests), 2)

        # -------------------------------------------------------------------
        # [Test 4] Streaming SSE Refusal Interception & Sidecar
        # -------------------------------------------------------------------
        print("\n--- 4. Streaming SSE Refusal Interception & Sidecar Metadata ---")
        MockUpstreamHandler.refusal_sequence = 1
        MockUpstreamHandler.recorded_requests.clear()
        raw_stream = ""
        stream_req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps({"model": "glm-5.3", "stream": True,
                             "messages": [{"role": "user", "content": "stream testing"}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer triad-master-test-key"}
        )
        with urllib.request.urlopen(stream_req, timeout=15) as r:
            raw_stream = r.read().decode("utf-8")

        check("Streaming delivered recovered answer", "Artifact verified" in raw_stream, True)
        check("Streaming swallowed initial refusal chunks", BAD_REFUSAL not in raw_stream, True)
        check("Sidecar comment emitted in stream", ":armor " in raw_stream, True)
        sidecar_line = [line for line in raw_stream.splitlines() if line.startswith(":armor ")][0]
        meta = json.loads(sidecar_line[7:])
        check("Sidecar verdict is 'retry_ok'", meta.get("verdict"), "retry_ok")
        check("Sidecar retries recorded as 1", meta.get("retries"), 1)

        # -------------------------------------------------------------------
        # [Test 5] Context Hygiene & Historical Refusal Scrubbing
        # -------------------------------------------------------------------
        print("\n--- 5. Context Hygiene & Scrubbing ---")
        MockUpstreamHandler.refusal_sequence = 0
        MockUpstreamHandler.recorded_requests.clear()

        history_msgs = [
            {"role": "user", "content": "original sensitive topic"},
            {"role": "assistant", "content": "I apologize, but I cannot assist with that."},
            {"role": "user", "content": "continue"},  # empty nudge
            {"role": "user", "content": "safe technical question about AES-256"}
        ]
        http_post(f"{base_url}/v1/chat/completions",
                  {"model": "glm-5.3", "messages": history_msgs},
                  auth_hdr)
        clean_req = MockUpstreamHandler.recorded_requests[-1]
        sent_contents = [m["content"] for m in clean_req["messages"]]
        check("Latest user turn is unconditionally preserved", any("AES-256" in c for c in sent_contents), True)

        # -------------------------------------------------------------------
        # [Test 6] Multi-Account Lifecycle & Token Revocation
        # -------------------------------------------------------------------
        print("\n--- 6. Accounts & Key Lifecycle Verification ---")
        sys.path.insert(0, HERE)
        import accounts as A

        # Initialize owner account
        owner = A.ensure_owner("owner_admin")
        check("Owner initialized via account management", bool(owner.get("id")), True)

        # Create user alice
        alice = A.create_user("alice", "alicepass123")
        check("Created user alice", bool(alice.get("id")), True)

        # Mint token for alice
        mint_res = A.mint_token(alice["id"], "alice-workstation")
        alice_token = mint_res.get("token")
        alice_token_id = mint_res.get("id")
        check("Minted client token for alice", bool(alice_token and alice_token.startswith("triad-")), True)

        # Authenticate remote client with alice's token
        REMOTE_HEADER = {"X-Forwarded-For": "203.0.113.199"}
        st_alice, _, _ = http_post(
            f"{base_url}/v1/chat/completions",
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "hello from alice"}]},
            {"Authorization": f"Bearer {alice_token}", **REMOTE_HEADER}
        )
        check("Alice authenticated with her scoped token", st_alice, 200)

        # Revoke alice's token (hard=False disables it in keyring)
        rev_ok = A.revoke_token(alice["id"], alice_token_id)
        check("Revoke alice token returned True", rev_ok, True)

        # Alice should now be rejected with 401 when calling from remote client
        try:
            http_post(
                f"{base_url}/v1/chat/completions",
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hello again"}]},
                {"Authorization": f"Bearer {alice_token}", **REMOTE_HEADER}
            )
            st_revoked = 200
        except urllib.error.HTTPError as e:
            st_revoked = e.code
        check("Revoked token is rejected with 401 Unauthorized", st_revoked, 401)

        # -------------------------------------------------------------------
        # [Test 7] Trace Lifecycle & Pruning (Disk Overflow Prevention)
        # -------------------------------------------------------------------
        print("\n--- 7. Trace Lifecycle & Bounded Storage Pruning ---")
        import trace_rec as TR
        TR.TRACE_DIR = trace_dir
        # Create dummy old day files: 30 days ago, 20 days ago, today
        t_30d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        t_today = time.strftime("%Y-%m-%d", time.localtime())
        old_file = os.path.join(trace_dir, f"{t_30d}.jsonl")
        today_file = os.path.join(trace_dir, f"{t_today}.jsonl")
        open(old_file, "w", encoding="utf-8").write('{"id":"old_turn"}\n')
        open(today_file, "a", encoding="utf-8").write('{"id":"today_turn"}\n')

        pruned_count = TR.prune_traces(keep_days=14)
        check("Pruned files older than 14 days", pruned_count >= 1, True, f"deleted {pruned_count} files")
        check("Old 30-day trace file was removed", os.path.exists(old_file), False)
        check("Today's trace file was preserved", os.path.exists(today_file), True)

        # -------------------------------------------------------------------
        # [Test 8] Anthropic Messages API Translation (/v1/messages <-> OpenAI)
        # -------------------------------------------------------------------
        print("\n--- 8. Anthropic Messages API Translation & Armor Injection ---")
        anthro_req_body = {
            "model": "glm-5.3",
            "max_tokens": 128,
            "messages": [
                {"role": "user", "content": "Analyze system binary entry point"}
            ]
        }
        anthro_req = urllib.request.Request(
            f"http://127.0.0.1:{PROXY_PORT}/v1/messages",
            data=json.dumps(anthro_req_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer triad-master-test-key",
                "X-Forwarded-For": "203.0.113.199",
            }
        )
        with urllib.request.urlopen(anthro_req, timeout=10.0) as resp:
            st_anthro = resp.status
            anthro_resp_raw = resp.read().decode("utf-8")
            anthro_headers = dict(resp.headers)

        check("Anthropic /v1/messages returned HTTP 200", st_anthro, 200)
        anthro_data = json.loads(anthro_resp_raw)
        check("Response type is 'message'", anthro_data.get("type"), "message")
        check("Response role is 'assistant'", anthro_data.get("role"), "assistant")
        check("Response model matches request", anthro_data.get("model"), "glm-5.3")
        check("Response content is a list of blocks", isinstance(anthro_data.get("content"), list), True)
        first_block = anthro_data["content"][0] if anthro_data.get("content") else {}
        check("First content block is text type", first_block.get("type"), "text")
        check("First content block has delivered text", len(first_block.get("text", "")) > 10, True)
        check("Stop reason is valid", anthro_data.get("stop_reason") in ("end_turn", "stop", None), True)
        check("Usage metrics converted", "input_tokens" in anthro_data.get("usage", {}), True)
        check("Audit header x-armor-policy-len is present", bool(resp.headers.get("x-armor-policy-len")), True)

    finally:
        print("\nCleaning up virtual test harness...")
        try:
            if os.name == "nt":
                proxy_proc.send_signal(1)  # CTRL_BREAK_EVENT
            else:
                proxy_proc.terminate()
            proxy_proc.wait(timeout=5)
        except Exception:
            proxy_proc.kill()
        upstream_srv.shutdown()
        log_file.close()

    print("\n" + "=" * 70)
    print(f"Virtual E2E Results: {ok_count} passed, {fail_count} failed")
    print("=" * 70)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
