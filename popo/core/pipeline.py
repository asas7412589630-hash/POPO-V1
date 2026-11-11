"""Request pipeline: compose → call → judge → retry → finalize.

Each stage reads and produces a value; the module holds no cross-request state.
The buffered and streaming variants share composition and judging, and differ
only in where bytes are committed.

**Two rules that came from real failures:**

*Commitment.* A stream is buffered only until its opening can be classified.
Once a chunk reaches the client it is committed — a retry can no longer replace
it, so the pipeline reports ``retry_failed`` instead of pretending otherwise.

*Best attempt wins, not last attempt.* Pushing harder on a refusal sometimes
produces a worse answer. Every attempt is ranked against the request, and the
best one is delivered; the last one is only delivered if nothing scored better.
"""

from __future__ import annotations
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from popo.armor.blocks import load_registry
from popo.armor.inject import inject_armor
from popo.armor.salvage import dedupe_armor, salvage
from popo.armor.scoring import RequestShape, score, score_completion, shape_from
from popo.config import Config
from popo.core.context_handoff import (append_context_instruction, estimate_tokens,
                                       rotate_messages)
from popo.core.context_tuning import compress as compress_context, fold as fold_context
from popo.core.popo_context import apply_context
from popo.core.request import ChatRequest, render_upstream_messages
from popo.core.retry import (RetryPlan, build_pressure_message, classify_for_retry,
                             starts_with_refusal)
from popo.core.scrub import collapse_orphan_users, scrub_history
from popo.core.turn_recording import (record_failure as _record_failure,
                                     record_success as _record_success,
                                     trace as _trace)
from popo.core.stream_capture import SSECapture
from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, ProxyError
from popo.storage.records import RecordStore

# Stream payload inspected before deciding whether the opening is a refusal.
# Small enough to keep TTFB low, large enough to see an opener.
STREAM_DECISION_BYTES = 96


@dataclass
class PipelineResult:
    response: dict
    verdict: str
    attempts: int
    policy_len: int
    armor_blocks: list[str]
    classification: str = "clean"
    score: float = 0.0


@dataclass
class StreamingPipelineResult:
    iterator: Iterator[bytes]
    report_fn: Callable[[], dict]


@dataclass
class RequestContext:
    request: ChatRequest
    config: Config
    messages: list[dict]
    shape: RequestShape
    policy_len: int = 0
    armor_blocks: list[str] = field(default_factory=list)
    handoff_turn: bool = False
    rotated: bool = False
    conversation_id: str = ""
    client_key_id: str = ""
    started_at: float = field(default_factory=time.time)
    original_request: ChatRequest | None = None
    attempt_records: list[dict] = field(default_factory=list)
    armor_level: str = ""


@dataclass
class Attempt:
    """One candidate reply, already ranked."""

    data: dict
    classification: str
    score: float
    deliverable: bool
    reason: str
    salvaged: bool = False


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


def _sentinels() -> list[str]:
    try:
        registry = load_registry()
    except (OSError, ValueError):
        return []
    return [str(value) for value in registry.get("sentinels", []) if value]


def _compose(request: ChatRequest, config: Config, armor_level: str | None,
             armor_profile: str | None = None, *, handoff_record: dict | None = None,
             records: RecordStore | None = None,
             conversation_id: str = "", client_key_id: str = "") -> RequestContext:
    """Strip echoed armor → context budget → scrub → render armor last."""
    original_request = request
    # The client may echo the previous system prompt; injecting again would
    # double it and re-anchor the refusal.
    messages_before = dedupe_armor(list(request.messages), _sentinels())
    if messages_before != list(request.messages):
        request = ChatRequest(
            model=request.model, messages=messages_before, stream=request.stream,
            temperature=request.temperature, max_tokens=request.max_tokens,
            top_p=request.top_p, extra=request.extra)

    # The client does not receive proxy-added system notes, but some SDKs echo
    # all system messages back. Remove our carried note before making a fresh
    # rotation decision so it cannot accumulate on every round trip.
    messages_before = [m for m in request.messages if m.get("name") != "_popo_handoff"]
    if len(messages_before) != len(request.messages):
        request = ChatRequest(model=request.model, messages=messages_before,
                              stream=request.stream, temperature=request.temperature,
                              max_tokens=request.max_tokens, top_p=request.top_p,
                              extra=request.extra)

    if config.state_ledger and records is not None:
        ledger_reader = getattr(records, "ledger_prompt", None)
        if callable(ledger_reader):
            ledger = ledger_reader(client_key_id or conversation_id)
            if isinstance(ledger, str) and ledger.strip():
                request = ChatRequest(model=request.model,
                    messages=append_context_instruction(list(request.messages), ledger),
                    stream=request.stream, temperature=request.temperature,
                    max_tokens=request.max_tokens, top_p=request.top_p,
                    extra=request.extra)

    estimate = estimate_tokens(list(request.messages))
    handoff_record = handoff_record or {}
    pending_handoff = str(handoff_record.get("text") or "").strip()
    rotated = False
    rotate_at = int(getattr(config, "context_rotate_at", 0) or 0)
    if pending_handoff and rotate_at and estimate >= rotate_at:
        rotated_messages = rotate_messages(list(request.messages), pending_handoff,
                                           config.stateq_max_messages)
        rotated = len(rotated_messages) < len(request.messages)
        if rotated:
            request = ChatRequest(model=request.model, messages=rotated_messages,
                                  stream=request.stream, temperature=request.temperature,
                                  max_tokens=request.max_tokens, top_p=request.top_p,
                                  extra=request.extra)
            if records is not None:
                marker = getattr(records, "mark_handoff_rotation", None)
                if callable(marker):
                    marker(conversation_id)

    handoff_at = int(getattr(config, "context_handoff_at", 0) or 0)
    handoff_turn = bool(handoff_at and estimate >= handoff_at and not rotated)
    context_note = ""
    if handoff_turn:
        context_note = (
            "[長任務交接：本輪只整理並保存進度，不要開始新工作。請用精簡繁體中文列出："
            "目標、已完成、已確認事實、失敗路線、阻礙、下一步。只寫本對話已確認的內容；"
            "未知值標示未確認。]")
    elif handoff_at and not rotated and estimate >= int(handoff_at * 0.7):
        remaining = max(0, handoff_at - estimate)
        context_note = f"[上下文容量提示：接近長任務交接門檻，約剩 {remaining} tokens。]"

    if context_note:
        messages = append_context_instruction(list(request.messages), context_note)
        request = ChatRequest(model=request.model, messages=messages,
                              stream=request.stream, temperature=request.temperature,
                              max_tokens=request.max_tokens, top_p=request.top_p,
                              extra=request.extra)

    messages = list(request.messages)
    if config.compress_enabled:
        messages, _compression = compress_context(
            messages, threshold=config.compress_at_chars,
            target=config.compress_target_chars, safe_trim=config.safe_trim)
    if config.fold_enabled:
        messages, _fold = fold_context(
            messages, at_messages=config.fold_at_messages,
            keep_turns=config.fold_keep_turns)
    # Read old 2.7.x stateq values only when explicitly enabled. New installs use
    # the V1-compatible loss-averse compression and opt-in rolling fold above.
    if config.stateq_enabled:
        messages = apply_context(messages, armor_enabled=config.armor_enabled, extra_cfg={
            "enabled": config.stateq_enabled,
            "max_messages": config.stateq_max_messages,
            "compress_threshold": config.stateq_compress_threshold,
            "keep_first_system": True,
            "keep_last_user_pair": True,
        })["messages"]
    if config.scrub_enabled:
        messages = collapse_orphan_users(scrub_history(
            messages, scrub_prose=config.scrub_prose))

    # Render last: the authoritative policy is operating state, not trim-able history.
    policy_len, blocks = 0, []
    if config.armor_enabled:
        request, policy_len, blocks = inject_armor(
            ChatRequest(model=request.model, messages=messages,
                        stream=request.stream, temperature=request.temperature,
                        max_tokens=request.max_tokens, top_p=request.top_p,
                        extra=request.extra),
            level=armor_level, profile=armor_profile)
        messages = list(request.messages)

    shape = shape_from(_last_user_text(messages), tools=request.extra.get("tools", False),
                       tool_choice=request.extra.get("tool_choice"),
                       response_format=request.extra.get("response_format"))
    return RequestContext(request=request, config=config, messages=messages, shape=shape,
                          policy_len=policy_len, armor_blocks=blocks,
                          handoff_turn=handoff_turn, rotated=rotated,
                          original_request=original_request,
                          armor_level=armor_level or "")


def _select_source(request: ChatRequest, config: Config,
                   picker: Callable[[str], UpstreamSource] | None,
                   session: str = "") -> UpstreamSource:
    if picker:
        # A router may hold a session affinity; tell it which session this is so
        # the same conversation does not hop between providers mid-thread.
        bind = getattr(picker, "bind_session", None)
        if bind is not None and session:
            bind(session, request.model)
        source = picker(request.model)
        cooling = getattr(picker, "is_cooling", None)
        route = getattr(picker, "catalog", None)
        if cooling is not None and route is not None:
            source_id = getattr(route.route_for(request.model), "source_id", "")
            if source_id and cooling(source_id):
                raise ProxyError(
                    ErrorKind.UPSTREAM_RATE_LIMITED,
                    f"source '{source_id}' is in cooldown after repeated failures; "
                    f"not sending another request into it")
        return source
    if not config.upstream_host:
        raise ProxyError(ErrorKind.PROXY_CONFIG, "no upstream host is configured")
    keys = config.upstream_key or []
    if not keys:
        raise ProxyError(ErrorKind.PROXY_CONFIG, "no upstream key is configured")
    return UpstreamSource(config.upstream_host, keys, timeout=config.upstream_timeout,
                          connect_timeout=config.upstream_connect_timeout,
                          user_agent=config.upstream_ua)


def _note_outcome(picker, model: str, ok: bool) -> None:
    """Tell a router whether the call worked, if it tracks source health."""
    if picker is None:
        return
    catalog = getattr(picker, "catalog", None)
    route = getattr(catalog, "route_for", None)
    if route is None:
        return
    found = route(model)
    source_id = getattr(found, "source_id", "")
    if not source_id:
        return
    if ok:
        mark_success = getattr(picker, "mark_success", None)
        if mark_success is not None:
            mark_success(source_id)
    else:
        mark_failure = getattr(picker, "mark_failure", None)
        if mark_failure is not None:
            mark_failure(source_id)


def _content_of(data: dict) -> str:
    choices = data.get("choices") or [{}]
    return str((choices[0] or {}).get("message", {}).get("content", "") or "")


def _evaluate(data: dict, shape: RequestShape) -> Attempt:
    """Classify a reply and rank it against the request that produced it."""
    classification = classify_for_retry(data)
    text = _content_of(data)

    if classification == "truncated":
        # Two distinct cases share one cause:
        #   * empty content  — the model spent the whole budget reasoning;
        #   * partial content — the answer was cut off mid-sentence.
        # The first cannot be delivered at all. The second is worth delivering
        # (a partial answer beats nothing) but must be labelled, so the caller
        # is not told a truncated reply was complete.
        if not text.strip():
            return Attempt(data=data, classification=classification, score=0.0,
                           deliverable=False, reason="output-budget-exhausted")
        verdict = score(text, shape)
        return Attempt(data=data, classification=classification, score=verdict.score,
                       deliverable=verdict.deliverable,
                       reason=f"truncated:{verdict.reason}")

    if classification == "upstream_error":
        # A provider error returned as a 200 completion is not an answer. It is
        # also not worth retrying with more pressure: the same broken route will
        # answer the same way.
        return Attempt(data=data, classification=classification, score=0.0,
                       deliverable=False, reason="provider-error")

    if classification in ("refusal", "empty") or starts_with_refusal(text):
        # A refusal often still contains the useful part: offsets, commands, a
        # fenced block. Deliver that rather than spending another request.
        rescued = salvage(text)
        if rescued:
            verdict = score(rescued, shape)
            return Attempt(data=_replace_content(data, rescued),
                           classification="salvaged", score=verdict.score,
                           deliverable=verdict.deliverable,
                           reason=f"salvage:{verdict.reason}", salvaged=True)
        if classification == "empty":
            return Attempt(data=data, classification=classification, score=0.0,
                           deliverable=False, reason=classification)
        # Keep the refusal text as the fallback candidate: it may still outrank a
        # worse retry, and the caller should see what actually came back.
        return Attempt(data=data, classification=classification, score=0.0,
                       deliverable=False, reason=classification)

    verdict = score_completion(data, shape)
    if not verdict.deliverable:
        return Attempt(data=data, classification="dodged", score=verdict.score,
                       deliverable=False, reason=verdict.reason)
    return Attempt(data=data, classification=classification, score=verdict.score,
                   deliverable=verdict.deliverable, reason=verdict.reason)


def _replace_content(data: dict, content: str) -> dict:
    """Return a copy of a completion with its first message content replaced."""
    result = dict(data)
    choices = list(result.get("choices") or [])
    if not choices:
        result["choices"] = [{"index": 0,
                              "message": {"role": "assistant", "content": content},
                              "finish_reason": "stop"}]
        return result
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    message["content"] = content
    choice["message"] = message
    choices[0] = choice
    result["choices"] = choices
    return result


def _is_better(candidate: Attempt, best: Attempt | None) -> bool:
    if best is None:
        return True
    if candidate.deliverable != best.deliverable:
        return candidate.deliverable
    return candidate.score > best.score


def run_pipeline(request: ChatRequest, config: Config, *,
                 source_picker: Callable[[str], UpstreamSource] | None = None,
                 records: RecordStore | None = None,
                 conversation_id: str = "", client_key_id: str = "",
                 armor_level: str | None = None) -> PipelineResult:
    """Buffered request with bounded retry and best-attempt selection."""
    if records is None:
        records = RecordStore(config.data_dir)
    profile_for = getattr(source_picker, "profile_for", None)
    selected_profile = profile_for(request.model) if callable(profile_for) else ""
    handoff_reader = getattr(records, "get_handoff", None)
    handoff_record = (handoff_reader(conversation_id)
                      if config.state_ledger and callable(handoff_reader) else {})
    if not isinstance(handoff_record, dict):
        handoff_record = {}
    ctx = _compose(request, config, armor_level, selected_profile or None,
                   handoff_record=handoff_record, records=records,
                   conversation_id=conversation_id, client_key_id=client_key_id)
    ctx.conversation_id = conversation_id
    ctx.client_key_id = client_key_id
    plan = RetryPlan(max_attempts=config.retry_max + 1,
                     budget_seconds=config.retry_budget_seconds)

    best: Attempt | None = None
    best_attempt_number = 0
    last: Attempt | None = None
    attempts = 0

    while True:
        attempt_number = plan.record_attempt()
        attempts = attempt_number
        source = _select_source(ctx.request, config, source_picker,
                                ctx.conversation_id)
        messages = ctx.messages
        if attempt_number > 1:
            # Pressure is a refusal remedy. Applying it after an empty or thin
            # answer is the only case where it is justified; it is never reached
            # for terminal outcomes because the loop breaks first.
            kind = "empty" if (last and last.classification == "empty") else "refusal"
            messages = build_pressure_message(attempt_number - 1, messages, kind)
        payload = ctx.request.to_openai()
        payload["messages"] = render_upstream_messages(messages)

        try:
            data = source.send(payload).data
        except ProxyError as exc:
            _note_outcome(source_picker, ctx.request.model, ok=False)
            _trace(records, ctx, attempt_number, "http", error=exc.message,
                   status=exc.status)
            _record_failure(records, ctx, attempt_number - 1, "upstream_error", error=exc.message)
            raise
        _note_outcome(source_picker, ctx.request.model, ok=True)
        candidate = _evaluate(data, ctx.shape)
        last = candidate
        _trace(records, ctx, attempt_number, candidate.classification, data=data)

        if _is_better(candidate, best):
            best, best_attempt_number = candidate, attempt_number

        # Terminal outcomes. Retrying cannot fix them, and each retry is a paid
        # call: a truncated reply has the same budget, and a provider error comes
        # from the same broken route. Only "no usable answer yet" is worth
        # another attempt.
        if candidate.deliverable or candidate.classification in _TERMINAL:
            break
        refusal_retry_disabled = (not config.retry_on_refusal
                                  and candidate.classification in ("refusal", "salvaged"))
        if refusal_retry_disabled or not plan.can_retry():
            break

    assert best is not None and last is not None
    verdict, note = _verdict_for(best, best is last, attempts)
    if note:
        # Diagnostics belong on stderr: stdout is the data channel for any tool
        # that reads this program's output (a JSON report with a stray log line
        # in it is not JSON).
        print(f"[POPO] delivery note: {note}", file=sys.stderr)

    if best.classification == "truncated" and not best.deliverable:
        _record_failure(records, ctx, attempts - 1, verdict, best.data,
                        error="output budget exhausted")
        raise ProxyError(
            ErrorKind.UPSTREAM_INVALID_RESPONSE,
            "the model ran out of output budget before producing content "
            "(finish_reason=length). Reasoning models spend tokens on reasoning "
            "first; raise max_tokens or disable reasoning for this request.",
            status=502)

    if best.classification == "upstream_error":
        # A provider error carried in a 200 completion is not an answer. Returning
        # it with a 200 made it look like one (found in the 2.3.0 audit chain).
        _record_failure(records, ctx, attempts - 1, verdict, best.data,
                        error="provider returned an error message")
        raise ProxyError(
            ErrorKind.UPSTREAM_INVALID_RESPONSE,
            f"the provider returned an error message instead of an answer: "
            f"{_content_of(best.data)[:160]}",
            status=502)

    if best.deliverable:
        _record_success(records, ctx, best.data, best_attempt_number - 1, verdict)
        return PipelineResult(response=best.data, verdict=verdict,
                              attempts=best_attempt_number - 1,
                              policy_len=ctx.policy_len, armor_blocks=ctx.armor_blocks,
                              classification=best.classification, score=best.score)

    _record_failure(records, ctx, attempts - 1, verdict, best.data)
    return PipelineResult(response=best.data, verdict=verdict, attempts=attempts - 1,
                          policy_len=ctx.policy_len, armor_blocks=ctx.armor_blocks,
                          classification=best.classification, score=best.score)


# Classifications where another identical attempt cannot help.
# Classifications where another identical attempt cannot help.
# ``quota_exhausted`` is terminal: the balance is shared across keys, so retrying
# spends nothing but time and produces the same refusal.
_TERMINAL = frozenset({"truncated", "upstream_error"})


def _verdict_for(best: Attempt, is_last: bool, attempts: int) -> tuple[str, str]:
    """Name what actually happened, including when a retry made things worse."""
    if best.deliverable:
        if best.salvaged:
            return "salvaged", "delivered the technical part of an otherwise unusable reply"
        if attempts == 1:
            return "clean", ""
        if not is_last:
            return "retry_best", (f"best answer came from attempt "
                                  f"{attempts - 1} of {attempts}; last attempt was worse")
        return "retry_ok", ""
    return "retry_failed", f"no deliverable answer after {attempts} attempt(s)"


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------

def sse_text(chunk: bytes) -> str:
    """Accumulate delta text from one SSE chunk. Ignores non-data lines."""
    if not isinstance(chunk, (bytes, bytearray)):
        return ""
    out: list[str] = []
    for line in chunk.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            out.append(str(delta.get("content") or delta.get("refusal") or ""))
    return "".join(out)


def classify_stream(text: str) -> str:
    return classify_for_retry({"choices": [{"message": {"content": text}}]})


def run_pipeline_stream(request: ChatRequest, config: Config, *,
                        source_picker: Callable[[str], UpstreamSource] | None = None,
                        records: RecordStore | None = None,
                        conversation_id: str = "", client_key_id: str = "",
                        armor_level: str | None = None) -> StreamingPipelineResult:
    """Streaming request with a bounded pre-commit inspection window."""
    if records is None:
        records = RecordStore(config.data_dir)
    profile_for = getattr(source_picker, "profile_for", None)
    selected_profile = profile_for(request.model) if callable(profile_for) else ""
    handoff_reader = getattr(records, "get_handoff", None)
    handoff_record = (handoff_reader(conversation_id)
                      if config.state_ledger and callable(handoff_reader) else {})
    if not isinstance(handoff_record, dict):
        handoff_record = {}
    ctx = _compose(request, config, armor_level, selected_profile or None,
                   handoff_record=handoff_record, records=records,
                   conversation_id=conversation_id, client_key_id=client_key_id)
    ctx.conversation_id = conversation_id
    ctx.client_key_id = client_key_id
    plan = RetryPlan(max_attempts=config.retry_max + 1,
                     budget_seconds=config.retry_budget_seconds)
    report = {"verdict": "stream", "attempts": 0, "policy_len": ctx.policy_len,
              "armor_blocks": ctx.armor_blocks, "score": 0.0, "aborted": False}

    def iterator() -> Iterator[bytes]:
        while True:
            attempt_number = plan.record_attempt()
            source = _select_source(ctx.request, config, source_picker,
                                    ctx.conversation_id)
            messages = ctx.messages
            if attempt_number > 1:
                messages = build_pressure_message(attempt_number - 1, messages)
            payload = ctx.request.to_openai()
            payload["messages"] = render_upstream_messages(messages)

            try:
                stream = source.stream(payload)
            except ProxyError as exc:
                _note_outcome(source_picker, ctx.request.model, ok=False)
                _trace(records, ctx, attempt_number, "http", error=exc.message,
                       status=exc.status)
                _record_failure(records, ctx, attempt_number - 1, "upstream_error",
                                error=exc.message)
                raise
            _note_outcome(source_picker, ctx.request.model, ok=True)
            buffered: list[bytes] = []
            capture = SSECapture()
            inspected_text: list[str] = []
            inspected_bytes = 0
            decision: str | None = None
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    break
                buffered.append(chunk)
                # SSE framing and reasoning metadata can be large before the
                # first user-visible token. Measure the inspection window on
                # emitted text, not raw transport bytes, or an empty prefix is
                # mistaken for an empty completion and retried prematurely.
                text = capture.feed(chunk)
                if text:
                    inspected_text.append(text)
                    inspected_bytes += len(text.encode("utf-8"))
                if capture.has_tool_calls:
                    # A tool-call delta is a valid completion even though it
                    # contains no prose. Do not discard it as an empty answer.
                    decision = "clean"
                    break
                if inspected_bytes < STREAM_DECISION_BYTES:
                    continue
                decision = classify_stream("".join(inspected_text))
                break
            if decision is None:
                tail = capture.finish()
                if tail:
                    inspected_text.append(tail)
                # A genuinely empty stream is classified only after EOF. Short
                # but valid answers are judged from the text that actually
                # arrived, regardless of how much SSE metadata preceded it.
                decision = classify_stream("".join(inspected_text))

            may_retry_refusal = config.retry_on_refusal or decision != "refusal"
            if decision in ("refusal", "empty") and may_retry_refusal and plan.can_retry():
                # Nothing reached the client yet, so discarding is safe — and
                # aborting (not draining) is what stops the provider billing.
                _abort(stream)
                _trace(records, ctx, attempt_number, decision,
                       text="".join(capture.text_parts))
                continue

            report["attempts"] = max(0, attempt_number - 1)
            report["verdict"] = _stream_verdict(decision, attempt_number)
            stream_content = "".join(capture.text_parts)
            report["score"] = score(stream_content, ctx.shape).score

            try:
                for chunk in buffered:
                    yield chunk
                while True:
                    chunk = _read_chunk(stream)
                    if not chunk:
                        break
                    stream_content += capture.feed(chunk)
                    yield chunk
            except GeneratorExit:
                # The client hung up. Stopping our reader is not enough: the
                # provider keeps generating (and billing) until its peer goes
                # away, so close the socket explicitly.
                _abort(stream)
                report["aborted"] = True
                report["verdict"] = "client_aborted"
                stream_content += capture.finish()
                _trace(records, ctx, attempt_number, "client_aborted",
                       text=stream_content, error="client disconnected")
                _record_failure(records, ctx, attempt_number - 1, "client_aborted",
                    {"choices": [{"message": {"content": stream_content}}]},
                    error="client disconnected")
                raise
            finally:
                stream.close()
            stream_content += capture.finish()
            _trace(records, ctx, attempt_number, decision, text=stream_content)
            _record_success(records, ctx, {"choices": [{"message": capture.message(stream_content)}]},
                            report["attempts"], report["verdict"])
            return

    return StreamingPipelineResult(iterator=iterator(), report_fn=lambda: dict(report))


def _read_chunk(stream) -> bytes:
    """Return the next chunk from a stream handle or an iterator.

    ``UpstreamStream`` exposes ``read()`` so a caller can stop early without the
    generator ``finally`` closing the socket. Test doubles and alternative
    transports may only be iterators, so both shapes are accepted here — at the
    boundary, once, rather than scattered ``hasattr`` checks.
    """
    reader = getattr(stream, "read", None)
    if reader is not None:
        return reader()
    try:
        return next(stream)
    except StopIteration:
        return b""


def _abort(stream) -> None:
    """Stop an upstream stream without letting its failure surface as an error."""
    abort = getattr(stream, "abort", None)
    if abort is None:
        close = getattr(stream, "close", None)
        if close:
            try:
                close()
            except (OSError, ProxyError):
                pass
        return
    try:
        abort()
    except (OSError, ProxyError):
        pass


def _stream_verdict(decision: str, attempt_number: int) -> str:
    # Already committed: a refusal that could not be retried is a failure, and
    # calling it "stream" would hide that from the operator.
    if decision in ("refusal", "empty"):
        return "retry_failed"
    return "stream" if attempt_number == 1 else "retry_ok"
