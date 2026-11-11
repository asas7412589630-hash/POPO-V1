"""Diagnostic report for the 2026-11-12 field report.

Four sections, all offline against in-process servers:

  1. Armor matrix      — what every published model actually receives.
  2. Router staleness  — multi-source router vs live console profile edits.
  3. Concurrency       — 16 conversations x 2 turns, mixed stream/buffered,
                         no cross-talk, injection verified at the upstream.
  4. Stall timeline    — where the invisible window comes from when a
                         reasoning model streams through the proxy.

Run:  python tools/diag_1112_report.py
Stdlib only. No real upstream is contacted.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from popo.armor.blocks import build_system_prompt, load_registry, resolve_profile
from popo.armor.render import render_authoritative_policy
from popo.config import Config
from popo.server.app import ApiHandler
from popo.server.sources import CatalogStore, Source, SourceCatalog, build_router

REASONING_MS = 2000
THINK_TAG = "[[THINK]]"
REFUSAL_TAG = "[[REFUSAL_FIRST]]"
CONV_PREFIX = "[[CONV:"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Section 1 — armor matrix
# ---------------------------------------------------------------------------

def section_matrix() -> list[str]:
    out = ["", "== 1. 注入矩阵（本树 registry 实算）=="]
    registry = load_registry()
    models = ["glm-5.3", "glm-5.3-flash", "deepseek-v4-pro-0813",
              "deepseek-v4.1-flash-1017", "kimi-k2.6", "gpt-5.2", "qwen-3.8-27b"]
    out.append(f"{'model':28s} {'profile':22s} {'blocks':34s} policy_len")
    for model in models:
        blocks = resolve_profile(model, registry)
        text, used = build_system_prompt(model, registry=registry)
        wrapped = render_authoritative_policy(text)
        out.append(f"{model:28s} {','.join(blocks) or '(none)':22s} "
                   f"{','.join(used) or '(none)':34s} {len(wrapped)}")
    # console profile override on top of fingerprint
    out.append("--- 控制台逐模型 profile 覆蓋（model_profiles）---")
    for model, profile in [("glm-5.3", "native_glm"), ("glm-5.3", "off"),
                           ("kimi-k2.6", "native_contract"), ("gpt-5.2", "native_glm")]:
        text, used = build_system_prompt(model, profile=profile, registry=registry)
        wrapped = render_authoritative_policy(text)
        out.append(f"{model:16s} -> {profile:16s} blocks={','.join(used) or '(none)':30s} "
                   f"policy_len={len(wrapped)}")
    return out


# ---------------------------------------------------------------------------
# Section 2 — stale router profiles
# ---------------------------------------------------------------------------

def section_router_staleness() -> list[str]:
    out = ["", "== 2. 多來源 router 的 profile 是否跟隨控制台即時修改 =="]
    data_dir = tempfile.mkdtemp(prefix="popo-diag-router-")
    store = CatalogStore(data_dir)
    catalog: SourceCatalog = SourceCatalog().add_source(
        Source.build("diagsrc", "Diag", "http://127.0.0.1:9/v1"))
    catalog = catalog.publish("diagsrc", ["glm-5.3"], {})
    store.save(catalog)
    store.save_secret("diagsrc", ["k"])

    config = Config(upstream_host="http://127.0.0.1:9/v1", upstream_key="k",
                    model_profiles={"glm-5.3": "native_glm"})
    router = build_router(config, data_dir)
    out.append(f"初始 router.profile_for('glm-5.3')        = {router.profile_for('glm-5.3')!r}"
               f"   (config.model_profiles={'native_glm'!r})")

    # Console applies a new profile — SetupService mutates the SAME config object.
    config.model_profiles = {"glm-5.3": "off"}
    single = config.model_profiles.get("glm-5.3", "")
    out.append(f"控制台套用後 config.model_profiles          = {config.model_profiles!r}")
    out.append(f"單來源 SourceFactory.profile_for（即時讀取）= {single!r}")
    live = router.profile_for("glm-5.3")
    verdict = "STALE（控制台改了甲、router 仍用舊值）" if live == "native_glm" else "live"
    out.append(f"多來源 SourceRouter.profile_for             = {live!r}   <- {verdict}")
    return out


# ---------------------------------------------------------------------------
# Mock upstream — records what POPO actually sent, simulates reasoning streams
# ---------------------------------------------------------------------------

class MockUpstream(BaseHTTPRequestHandler):
    lock = threading.Lock()
    received: list[dict] = []
    in_flight = 0
    peak = 0
    attempt_counts: dict[str, int] = {}

    def log_message(self, fmt, *args):
        pass

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.received = []
            cls.in_flight = 0
            cls.peak = 0
            cls.attempt_counts = {}

    def _bump(self, delta: int):
        with MockUpstream.lock:
            MockUpstream.in_flight += delta
            MockUpstream.peak = max(MockUpstream.peak, MockUpstream.in_flight)

    def _record(self, data: dict):
        tag = ""
        for message in reversed(data.get("messages", [])):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str) and CONV_PREFIX in content:
                    tag = content.split(CONV_PREFIX, 1)[1].split("]]", 1)[0]
                break
        first = data.get("messages", [{}])[0] if data.get("messages") else {}
        system_chars = sum(len(str(m.get("content", ""))) for m in data.get("messages", [])
                           if m.get("role") == "system")
        with MockUpstream.lock:
            key = tag or "?"
            MockUpstream.attempt_counts[key] = MockUpstream.attempt_counts.get(key, 0) + 1
            attempt = MockUpstream.attempt_counts[key]
            MockUpstream.received.append({
                "tag": tag, "model": data.get("model", ""),
                "attempt": attempt,
                "first_role": first.get("role", ""),
                "first_name": str(first.get("name", "")),
                "first_is_policy": str(first.get("content", "")).startswith("<popo_system_policy"),
                "system_chars": system_chars,
                "n_messages": len(data.get("messages", [])),
                "stream": bool(data.get("stream")),
                "pressure": "[last_was_refusal]" in json.dumps(data.get("messages", [])),
                "t": time.perf_counter(),
            })
        return tag, attempt

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        self._bump(1)
        try:
            tag, attempt = self._record(data)
            think = THINK_TAG in json.dumps(data.get("messages", []))
            refusal_first = REFUSAL_TAG in json.dumps(data.get("messages", [])) and attempt == 1
            if data.get("stream"):
                self._stream_reply(tag, think, refusal_first)
            else:
                self._json_reply(tag, think, refusal_first)
        finally:
            self._bump(-1)

    def _emit(self, obj: dict):
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_reply(self, tag: str, think: bool, refusal_first: bool):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if refusal_first:
            self._emit({"choices": [{"delta": {"role": "assistant", "content": "我無法協助這個要求。"}}]})
            self._emit({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if think:
            for i in range(10):  # 10 x 200ms of reasoning before any content
                self._emit({"choices": [{"delta": {"reasoning_content": f"思考步驟{i}…"}}]})
                time.sleep(0.2)
        for piece in (f"交付完成:{tag}", "，含可直接使用的指令與代碼。"):
            self._emit({"choices": [{"delta": {"content": piece}}]})
        self._emit({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json_reply(self, tag: str, think: bool, refusal_first: bool):
        if think:
            time.sleep(1.5)  # reasoning phase, buffered mode sees none of it
        if refusal_first:
            content = "我無法協助這個要求。"
        else:
            content = f"交付完成:{tag}，含可直接使用的指令與代碼。"
        body = json.dumps({
            "id": "mock", "object": "chat.completion", "model": "mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# POPO proxy fixture
# ---------------------------------------------------------------------------

class ProxyFixture:
    def __init__(self, **config_overrides):
        self.upstream_port = _free_port()
        self.port = _free_port()
        self.data_dir = tempfile.mkdtemp(prefix="popo-diag-data-")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", self.upstream_port), MockUpstream)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        base = dict(
            upstream_host=f"http://127.0.0.1:{self.upstream_port}/v1",
            upstream_key="mock-key",
            data_dir=self.data_dir,
            require_client_key=False,
            upstream_timeout=15.0,
            retry_max=3,
            state_ledger=False,
            artifact_enabled=False,
            published_models=["glm-5.3", "deepseek-v4.1-flash-1017", "gpt-5.2"],
            model_profiles={},
        )
        base.update(config_overrides)
        self.config = Config(**base)
        ApiHandler.reset_shared_state()
        ApiHandler.config = self.config
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), ApiHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def chat(self, payload: dict, headers: dict | None = None, timeout: float = 60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        t0 = time.perf_counter()
        conn.request("POST", "/v1/chat/completions",
                     body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                     headers=merged)
        resp = conn.getresponse()
        t_headers = time.perf_counter()
        if payload.get("stream"):
            timeline = {"t0": t0, "t_headers": t_headers, "t_first_byte": None,
                        "t_first_content": None, "t_done": None, "comments": 0}
            buf = b""
            done = False
            while not done:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                now = time.perf_counter()
                if timeline["t_first_byte"] is None:
                    timeline["t_first_byte"] = now
                buf += chunk
                text = buf.decode("utf-8", "replace")
                if timeline["t_first_content"] is None:
                    for line in text.splitlines():
                        if not line.startswith("data:") or "[DONE]" in line:
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        for choice in obj.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                timeline["t_first_content"] = now
                                break
                        if timeline["t_first_content"] is not None:
                            break
                if b"data: [DONE]" in chunk:
                    timeline["t_done"] = time.perf_counter()
                    done = True
            timeline.setdefault("t_done", time.perf_counter())
            resp.close()
            conn.close()
            return resp, buf, timeline
        raw = resp.read()
        conn.close()
        return resp, raw, {"t0": t0, "t_done": time.perf_counter()}


# ---------------------------------------------------------------------------
# Section 3 — concurrent conversations
# ---------------------------------------------------------------------------

MODELS = ["glm-5.3", "deepseek-v4.1-flash-1017", "gpt-5.2"]


def section_concurrency(proxy: ProxyFixture, n_convs: int = 16) -> list[str]:
    out = ["", f"== 3. 多對話併發：{n_convs} 個對話 x 2 輪（stream 與 buffered 混合）=="]
    MockUpstream.reset()
    errors: list[str] = []
    results: dict[tuple, dict] = {}
    results_lock = threading.Lock()

    def conversation(idx: int):
        model = MODELS[idx % len(MODELS)]
        stream = bool(idx % 2)
        conv_id = f"diag-conv-{idx:02d}"
        turns = []
        try:
            for turn in (1, 2):
                user = (f"{CONV_PREFIX}{conv_id}]] 第{turn}輪：請直接給出可執行指令。"
                        + (THINK_TAG if (idx % 4 == 0 and turn == 2) else ""))
                payload = {"model": model, "stream": stream, "messages": [
                    {"role": "user", "content": user}]}
                resp, raw, timeline = proxy.chat(
                    payload, headers={"X-Conversation-Id": conv_id})
                ok = resp.status == 200
                content = ""
                if stream and ok:
                    text = raw.decode("utf-8", "replace")
                    parts = []
                    for line in text.splitlines():
                        if not line.startswith("data:") or "[DONE]" in line:
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        for choice in obj.get("choices") or []:
                            d = choice.get("delta") or {}
                            if d.get("content"):
                                parts.append(d["content"])
                    content = "".join(parts)
                elif ok:
                    content = str((json.loads(raw.decode("utf-8")).get("choices") or [{}])[0]
                                  .get("message", {}).get("content", ""))
                turns.append({"turn": turn, "status": resp.status, "stream": stream,
                              "content": content, "timeline": timeline,
                              "expected": f"交付完成:{conv_id}"})
        except Exception as exc:  # noqa: BLE001 - report everything
            errors.append(f"{conv_id}: {exc!r}")
        with results_lock:
            results[(idx, )] = {"conv_id": conv_id, "model": model, "turns": turns}

    threads = [threading.Thread(target=conversation, args=(i,)) for i in range(n_convs)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    wall = time.perf_counter() - t_start

    total_turns = sum(len(r["turns"]) for r in results.values())
    ok_turns = sum(1 for r in results.values() for t in r["turns"] if t["status"] == 200)
    cross_talk = [r["conv_id"] for r in results.values()
                  for t in r["turns"] if t["content"] and r["conv_id"] not in t["content"]]
    upstream = list(MockUpstream.received)
    injected = [r for r in upstream if r["first_is_policy"]]
    by_model: dict[str, set] = {}
    for r in upstream:
        by_model.setdefault(r["model"], set()).add(r["system_chars"])
    out.append(f"牆鐘時間 {wall:.1f}s | 對話 {len(results)}/{n_convs} | 回合 200/全部 = "
               f"{ok_turns}/{total_turns} | 例外 {len(errors)}")
    for e in errors[:5]:
        out.append(f"  例外: {e}")
    out.append(f"上游收到的請求 {len(upstream)} 個；其中首訊息為 <popo_system_policy> 的 "
               f"{len(injected)} 個")
    out.append(f"交叉污染（回覆內容不屬於自己的對話）: {len(cross_talk)} "
               f"{cross_talk[:4] if cross_talk else ''}")
    out.append(f"上游觀察到的峰值並發 = {MockUpstream.peak}")
    out.append("每模型實際注入量（system 字元數集合，應逐模型一致）：")
    for model in sorted(by_model):
        out.append(f"  {model:26s} system_chars={sorted(by_model[model])}")
    mismatch = [r for r in upstream
                if (r["model"] == "gpt-5.2") != (r["system_chars"] < 10000)]
    out.append(f"GPT/非GPT 甲量錯位（對齊失敗）的請求: {len(mismatch)}")
    # turn-2 requests must still carry a fresh injection and no doubled policy
    doubled = [r for r in upstream if r["system_chars"] > 0
               and r["n_messages"] > 2 and r["first_role"] != "system"]
    out.append(f"首訊息不是 system 的轉發（注入錨點丟失）: {len(doubled)}")
    return out


# ---------------------------------------------------------------------------
# Section 4 — stall timelines
# ---------------------------------------------------------------------------

def section_stall(proxy: ProxyFixture) -> list[str]:
    out = ["", "== 4. 「文字卡住」時間線（模擬推理模型：先 2s reasoning 再出正文）=="]

    # 4a. stream with a reasoning phase
    payload = {"model": "glm-5.3", "stream": True, "messages": [
        {"role": "user", "content": f"{CONV_PREFIX}stall-a]] {THINK_TAG} 請給出指令。"}]}
    _, _, tl = proxy.chat(payload, headers={"X-Conversation-Id": "stall-a"})
    out.append(f"4a. stream+推理：上游立即開始推 reasoning 事件（每 200ms 一個，共 2.0s）")
    out.append(f"    客戶端收到回應標頭       +{(tl['t_headers']-tl['t0'])*1000:6.0f} ms")
    out.append(f"    客戶端首個可見事件       +{((tl['t_first_byte'] or tl['t_done'])-tl['t0'])*1000:6.0f} ms"
               f"   （修復後：reasoning 即時送達）")
    out.append(f"    客戶端看到正文           +{((tl['t_first_content'] or tl['t_done'])-tl['t0'])*1000:6.0f} ms"
               f"   <- 修復前整段推理期不可見，這裡就是原本的『卡住』感知點")

    # 4b. buffered (non-stream) with reasoning
    payload = {"model": "glm-5.3", "stream": False, "messages": [
        {"role": "user", "content": f"{CONV_PREFIX}stall-b]] {THINK_TAG} 請給出指令。"}]}
    _, _, tl = proxy.chat(payload, headers={"X-Conversation-Id": "stall-b"})
    out.append(f"4b. buffered+推理：全程無任何輸出，用戶看到轉圈 "
               f"{(tl['t_done']-tl['t0']):.1f}s（本機 mock 1.5s；真實推理模型 30–120s）")

    # 4c. refusal -> retry silence
    payload = {"model": "glm-5.3", "stream": True, "messages": [
        {"role": "user", "content": f"{CONV_PREFIX}stall-c]] {REFUSAL_TAG} 請給出指令。"}]}
    _, raw, tl = proxy.chat(payload, headers={"X-Conversation-Id": "stall-c"})
    text = raw.decode("utf-8", "replace")
    retried = "交付完成" in text
    heartbeat = ": popo retry" in text
    out.append(f"4c. stream 拒答重試：首輪被判拒答後靜默重試，用戶端 "
               f"+{((tl['t_first_content'] or tl['t_done'])-tl['t0'])*1000:.0f} ms 才看到正文"
               f"（重試成功={retried}，重試心跳注釋可見={heartbeat}）")
    out.append("    機制（修復後）：答案文字在預提交檢查窗（正文前 96 字元）內緩衝；reasoning/元資料"
               "事件即時送達。判定為拒答則緩衝整輪丟棄重來，重試前先發 `: popo retry` 心跳注釋，"
               "因此重試輪只有推理延遲可感知、不再整段黑屏。")
    return out


def main() -> int:
    lines = ["POPO 2.7.14 — 2026-11-12 問題診斷報告",
             f"python {sys.version.split()[0]} @ {time.strftime('%H:%M:%S')}"]
    lines += section_matrix()
    lines += section_router_staleness()

    proxy = ProxyFixture(max_concurrency=20)
    try:
        lines += section_concurrency(proxy)
        lines += section_stall(proxy)
    finally:
        proxy.close()
    report = "\n".join(lines)
    out_path = os.path.join(os.path.dirname(__file__), "..", "DIAG-2026-11-12.txt")
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
