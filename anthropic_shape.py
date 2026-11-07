#!/usr/bin/env python3
"""Anthropic Messages API <-> OpenAI Chat Completions translation.

Why: the proxy injects armour into OpenAI-shaped requests and forwards them to
an OpenAI-shaped upstream. A Claude-shaped client (Anthropic SDK, Claude Code,
anything that speaks POST /v1/messages) got a 404 and could not use the proxy at
all. This module converts the request in and the response back, so the client
sees a native Anthropic surface while everything downstream stays OpenAI.

Scope:
  * request : system / messages (text, image, tool_use, tool_result) / tools /
              tool_choice / stop_sequences / sampling params
  * response: buffered and streaming (SSE), text and tool_use
  * streaming: OpenAI deltas are re-emitted as the Anthropic event sequence
               (message_start -> content_block_* -> message_delta -> message_stop)

Unknown fields are passed through when they are harmless and dropped otherwise;
an unsupported block never raises — it degrades to text so a turn still runs.
"""
import json
import time
import uuid

DEFAULT_MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------
def _text_of(content):
    """Flatten an Anthropic `content` (str or list of blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif b.get("type") == "tool_result":
                    parts.append(_text_of(b.get("content")))
        return "".join(parts)
    return str(content)


def _parts(content):
    """Anthropic content -> OpenAI content (str, or list of text/image parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts, only_text, texts = [], True, []
    for b in content:
        if isinstance(b, str):
            texts.append(b)
            parts.append({"type": "text", "text": b})
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = b.get("text") or ""
            texts.append(txt)
            parts.append({"type": "text", "text": txt})
        elif t == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                url = "data:%s;base64,%s" % (src.get("media_type") or "image/png",
                                             src["data"])
                parts.append({"type": "image_url", "image_url": {"url": url}})
                only_text = False
            elif src.get("type") == "url" and src.get("url"):
                parts.append({"type": "image_url", "image_url": {"url": src["url"]}})
                only_text = False
        else:
            # tool_use / tool_result handled by the message mapper; anything else
            # contributes its text if it has any.
            txt = _text_of([b])
            if txt:
                texts.append(txt)
                parts.append({"type": "text", "text": txt})
    if only_text:
        return "".join(texts)
    return parts


def _tool_to_openai(t):
    if not isinstance(t, dict):
        return None
    fn = {"name": t.get("name") or ""}
    if t.get("description") is not None:
        fn["description"] = t.get("description")
    fn["parameters"] = t.get("input_schema") or {"type": "object", "properties": {}}
    return {"type": "function", "function": fn}


def _tool_choice_to_openai(tc):
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _msgs_from(role, content):
    """One Anthropic message -> one or more OpenAI messages (tool blocks split)."""
    out = []
    if isinstance(content, str) or content is None:
        return [{"role": role, "content": content or ""}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    if role == "assistant":
        text = []
        tool_calls = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {"name": b.get("name") or "",
                                 "arguments": json.dumps(b.get("input") or {},
                                                         ensure_ascii=False)},
                })
            else:
                text.append(_text_of([b]))
        msg = {"role": "assistant", "content": "".join(text) or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return [msg]

    # user: tool_result blocks become role=tool messages before the user text.
    tool_msgs, normal = [], []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": b.get("tool_use_id") or "",
                "content": _text_of(b.get("content")) or "",
            })
        else:
            normal.append(b)
    out.extend(tool_msgs)
    if normal:
        out.append({"role": "user", "content": _parts(normal)})
    if not out:
        out.append({"role": "user", "content": ""})
    return out


def to_openai(body):
    """Anthropic Messages request -> OpenAI Chat Completions request."""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    out = {}
    msgs = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        if role == "system":
            msgs.append({"role": "system", "content": _text_of(m.get("content"))})
        else:
            msgs.extend(_msgs_from(role, m.get("content")))

    out["messages"] = msgs
    if body.get("model"):
        out["model"] = body["model"]
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p", "stream"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    tools = [_tool_to_openai(t) for t in (body.get("tools") or [])]
    tools = [t for t in tools if t]
    if tools:
        out["tools"] = tools
        tc = _tool_choice_to_openai(body.get("tool_choice"))
        if tc is not None:
            out["tool_choice"] = tc
    meta = body.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        out["user"] = str(meta["user_id"])
    return out


# ---------------------------------------------------------------------------
# response: OpenAI -> Anthropic
# ---------------------------------------------------------------------------
_FINISH = {"stop": "end_turn", "length": "max_tokens",
           "tool_calls": "tool_use", "function_call": "tool_use",
           "content_filter": "end_turn", "null": None}


def _stop_reason(finish):
    if finish is None:
        return "end_turn"
    return _FINISH.get(finish, "end_turn")


def from_openai(data, model=""):
    """Buffered OpenAI chat.completion bytes -> Anthropic message bytes."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return data  # not JSON (already an error page); leave it untouched
    if not isinstance(obj, dict):
        return data
    if "choices" not in obj and isinstance(obj.get("error"), dict):
        # An upstream/API error in OpenAI shape: rewrite it into the Anthropic
        # error envelope so an Anthropic SDK surfaces the message instead of
        # choking on an unexpected response type.
        err = obj["error"]
        return json.dumps({"type": "error", "error": {
            "type": err.get("type") or err.get("code") or "api_error",
            "message": err.get("message") or "upstream error"}},
            ensure_ascii=False).encode("utf-8")
    if "choices" not in obj:
        return data

    choice = (obj.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    blocks = []
    if isinstance(message.get("content"), str) and message["content"]:
        blocks.append({"type": "text", "text": message["content"]})
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments")}
        blocks.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:20]),
                       "name": fn.get("name") or "", "input": args})
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    usage = obj.get("usage") or {}
    out = {
        "id": obj.get("id") if str(obj.get("id", "")).startswith("msg_") else
              "msg_" + uuid.uuid4().hex[:20],
        "type": "message",
        "role": "assistant",
        "model": model or obj.get("model") or "",
        "content": blocks,
        "stop_reason": _stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


def is_anthropic_content_type(ctype):
    return "text/event-stream" in (ctype or "").lower()


# ---------------------------------------------------------------------------
# streaming: OpenAI SSE -> Anthropic SSE event stream
# ---------------------------------------------------------------------------
class StreamTranslator:
    """Stateful OpenAI-SSE -> Anthropic-SSE converter.

    One instance per response. feed() takes raw upstream bytes (which may end on
    a partial line) and returns already-encoded Anthropic SSE bytes to write to
    the client. finish() emits the closing events.

    Anthropic's event grammar:
        message_start
        content_block_start (index 0)
        content_block_delta* (text_delta)
        content_block_stop
        message_delta (stop_reason + usage)
        message_stop
    Tool calls open a new content block per call (index increments).
    """

    def __init__(self, model=""):
        self.model = model
        self.buf = b""
        self.msg_id = "msg_" + uuid.uuid4().hex[:20]
        self.started = False
        self.block_index = -1
        self.text_open = False
        self.tool_index = {}      # openai tool_calls[i].id/name -> anthropic block index
        self.cur_tool = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self.finished = False

    # -- SSE helpers --
    @staticmethod
    def _ev(event, obj):
        return ("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False))).encode("utf-8")

    def _message_start(self):
        self.started = True
        return self._ev("message_start", {
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant",
                        "model": self.model, "content": [], "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": self.input_tokens, "output_tokens": 0}},
        })

    def _open_text(self):
        self.block_index += 1
        self.text_open = True
        return self._ev("content_block_start", {
            "type": "content_block_start", "index": self.block_index,
            "content_block": {"type": "text", "text": ""}})

    def _close_text(self):
        if self.text_open:
            self.text_open = False
            return self._ev("content_block_stop",
                            {"type": "content_block_stop", "index": self.block_index})
        return b""

    def _handle_choice(self, choice):
        out = b""
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str) and delta["content"]:
            if not self.started:
                out += self._message_start()
            if not self.text_open:
                out += self._open_text()
            out += self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "text_delta", "text": delta["content"]}})
        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            if idx not in self.tool_index:
                if not self.started:
                    out += self._message_start()
                out += self._close_text()
                self.block_index += 1
                self.tool_index[idx] = self.block_index
                tcid = tc.get("id") or ("toolu_" + uuid.uuid4().hex[:20])
                out += self._ev("content_block_start", {
                    "type": "content_block_start", "index": self.block_index,
                    "content_block": {"type": "tool_use", "id": tcid,
                                      "name": fn.get("name") or "", "input": {}}})
            if fn.get("arguments"):
                out += self._ev("content_block_delta", {
                    "type": "content_block_delta", "index": self.tool_index[idx],
                    "delta": {"type": "input_json_delta",
                              "partial_json": fn["arguments"]}})
        fr = choice.get("finish_reason")
        if fr:
            self.stop_reason = _stop_reason(fr)
        return out

    def feed(self, chunk):
        if self.finished:
            return b""
        self.buf += chunk
        out = b""
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            usage = obj.get("usage") or {}
            if usage:
                self.input_tokens = int(usage.get("prompt_tokens") or self.input_tokens or 0)
                self.output_tokens = int(usage.get("completion_tokens") or self.output_tokens or 0)
            for choice in (obj.get("choices") or []):
                out += self._handle_choice(choice)
        return out

    def finish(self):
        if self.finished:
            return b""
        self.finished = True
        out = b""
        if not self.started:
            out += self._message_start()
        out += self._close_text()
        out += self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens}})
        out += self._ev("message_stop", {"type": "message_stop"})
        return out


def looks_anthropic_body(obj) -> bool:
    """True when a parsed request body is shaped like an Anthropic Messages call.

    The discriminator is what the two schemas disagree about, not what they share:
    every Anthropic body allows `system` as a top-level string/block list and
    uses `max_tokens` + `messages`, while an OpenAI body puts the system prompt
    inside `messages` and has no top-level `system` field at all. Content blocks
    (`[{"type": "text", ...}]`) are a second strong signal.

    Deliberately conservative: an ambiguous body is treated as OpenAI, because a
    false positive here rewrites the caller's request while a false negative only
    produces an upstream 400 that names the missing field.
    """
    if not isinstance(obj, dict):
        return False
    has_anthropic_markers = ("system" in obj
                             or "stop_sequences" in obj
                             or "anthropic_version" in obj)
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or not msgs:
        # No messages at all: a bare {model, max_tokens} is equally valid in
        # both, so do not claim it.
        return False
    # Content blocks with a "type" of text/image/tool_use appear only in the
    # Anthropic schema; OpenAI uses a string or [{type:"text"}] but the latter
    # is uncommon and would still translate losslessly.
    blocks = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") in (
                        "tool_use", "tool_result", "image"):
                    blocks = True
                    break
        if blocks:
            break
    # An OpenAI-only field is a hard veto.
    for k in ("response_format", "functions", "function_call", "n", "seed",
              "logprobs", "stream_options", "presence_penalty",
              "frequency_penalty", "logit_bias"):
        if k in obj:
            return False
    # `tools` exists in both schemas but is shaped differently: Anthropic uses
    # {name, description, input_schema}, OpenAI nests the spec under "function".
    tools = obj.get("tools")
    if isinstance(tools, list) and tools:
        first = tools[0] if isinstance(tools[0], dict) else {}
        if "function" in first:
            return False
        if "input_schema" in first:
            return True
    return bool(has_anthropic_markers or blocks)

