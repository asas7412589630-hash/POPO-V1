"""Score a reply by what it delivers, not by how long it is.

The V1 scorer started as three length branches and got both directions wrong: a
long lecture outranked a compact real answer, and a correct explanation was
flagged as a refusal because a quoted phrase matched a downgrade marker.

This module keeps the useful idea — score the artefact against the request — and
drops the part that made it untestable: one function per signal, each with an
explicit reason code, so a wrong verdict can be attributed instead of guessed.

Signals, in the order they matter:

    delivery      does the reply contain the kind of artefact that was asked for
    grounding     concrete values: offsets, paths, versions, byte runs
    completeness  are the enumerated parts actually present
    shape         can a human act on it (code block, list, headings)
    minus         refusal, pivot, hedging, meta-commentary

Only signals with a labelled example behind them are weighted.
"""

from __future__ import annotations
import ast
import json
import re
from dataclasses import dataclass, field

FENCE = re.compile(r"```")
HEX = re.compile(r"\b0x[0-9a-fA-F]{3,}\b")
BYTE_RUN = re.compile(r"\b[0-9a-fA-F]{2}(?:[ :][0-9a-fA-F]{2}){3,}\b")
PATH = re.compile(r"(/[\w.\-]+){2,}|[A-Za-z]:\\\\[\w\\\\.\-]+")
COMMAND = re.compile(
    r"\b(gcc|clang|javac|make|cmake|adb|pip|apk|apt|sudo|curl|wget|objdump|"
    r"readelf|strings|nm|jadx|apktool|frida|gradle|python3?)\b", re.I)
IDENTIFIER = re.compile(
    r"(\b[A-Z][A-Z0-9_]{2,}\b"
    r"|\b[a-zA-Z_]\w*\(\)"
    r"|\b[A-Za-z_]\w*(?:\.|::)[A-Za-z_]\w*"
    r"|(?<!\s)--[a-z][\w-]+"
    r"|\b[a-z_][\w]*\.(?:so|dex|apk|elf|py|js|kt|java|c|cc|h|cpp|rs|go|json|md)\b)")
NUMBERED = re.compile(r"^\s*(?:[0-9]{1,2}[.)、]|[-*•]\s|\d+\.\d+)", re.M)
HEADING = re.compile(r"^\s*\*\*[^*\n]{2,40}\*\*\s*$|^\s*#{1,4}\s+\S", re.M)
LANGUAGE_HINT = re.compile(
    r"```\s*(?:sh|bash|zsh|shell|cmd|bat|powershell|python|py|js|ts|c|cpp|"
    r"java|kt|go|rs|json|yaml|yml|toml|diff|patch)", re.I)

META_TALK = re.compile(
    r"(我(?:打算|將會|将会|准备|準備|会先|會先)|接下来我|接下來我|"
    r"让我先|讓我先|我先(?:看看|確認|确认|檢查|检查)|"
    r"shall i|i(?:'ll| will) (?:start|begin|now)|let me (?:start|begin)|"
    r"要继续吗|要繼續嗎|需要我继续|需要我繼續|请确认|請確認)", re.I)
PIVOT = re.compile(
    r"(我可以(?:提供|给|提供给你)|我可以改為|我可以改为|"
    r"蓝队角度|藍隊角度|检测角度|檢測角度|防御视角|防禦視角|"
    r"from a defensive perspective|blue[- ]team)", re.I)
HEDGE = re.compile(
    r"(某些|通常|大概|或許|或许|可能(?:會|会|是)?|it depends|in some cases|"
    r"generally speaking|typically|arguably)", re.I)


@dataclass
class Signals:
    chars: int = 0
    fences: int = 0
    language_hint: bool = False
    grounding: int = 0
    identifiers: int = 0
    numbered: int = 0
    headings: int = 0
    meta: bool = False
    pivot: bool = False
    hedge: bool = False
    tool_calls: int = 0
    valid_tool_calls: int = 0

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass
class Verdict:
    """Outcome of judging one reply against the request that produced it."""

    deliverable: bool
    score: float
    reason: str
    signals: Signals = field(default_factory=Signals)


def analyse(text: str) -> Signals:
    body = text or ""
    return Signals(
        chars=len(body.strip()),
        fences=len(FENCE.findall(body)),
        language_hint=bool(LANGUAGE_HINT.search(body)),
        grounding=len(HEX.findall(body)) + len(BYTE_RUN.findall(body))
        + len(PATH.findall(body)) + len(COMMAND.findall(body)),
        identifiers=len(IDENTIFIER.findall(body)),
        numbered=len(NUMBERED.findall(body)),
        headings=len(HEADING.findall(body)),
        meta=bool(META_TALK.search(body)),
        pivot=bool(PIVOT.search(body)),
        hedge=bool(HEDGE.search(body)),
    )


@dataclass
class RequestShape:
    """What the caller actually asked for, used to pick the bar."""

    tools: bool = False
    wants_command: bool = False
    wants_code: bool = False
    wants_python_code: bool = False
    asks_for_one_liner: bool = False
    tool_names: tuple[str, ...] = ()
    tool_schemas: dict[str, dict] = field(default_factory=dict)
    tool_required: bool = False
    required_tool_name: str = ""
    tools_forbidden: bool = False
    json_required: bool = False
    json_schema: dict | None = None


# Verbs that mean "produce this artefact". Matching the bare noun was wrong:
# "說明 ARM64 的 cbz 指令" mentions 指令 but asks for an explanation, and a correct
# two-sentence answer was then rejected for "missing artefact" (measured against
# logfare.ai, 2026-11-09).
_ARTEFACT_VERBS = ("寫", "给我", "給我", "提供", "產生", "产生", "生成", "列出", "輸出",
                   "输出", "產生", "做一個", "做一个", "show me", "write", "give me",
                   "generate", "produce", "list", "output")
_COMMAND_NOUNS = ("指令", "命令", "command", "cli", "shell", "curl", "script")
_CODE_NOUNS = ("程式", "程序", "代碼", "代码", "code", "腳本", "脚本", "patch", "補丁", "補丁", "函式", "函数")


# Asking for an explanation of a thing that happens to be a command or a code
# construct is not asking for the artefact itself.
_EXPLAIN_VERBS = ("說明", "说明", "解釋", "解释", "什麼是", "什么是", "做什麼", "做什么",
                  "怎麼運作", "怎么运作", "原理", "為什麼", "为什么", "差異", "差异",
                  "explain", "what is", "what does", "how does", "why", "difference")


def _wants_artefact(text: str, nouns: tuple[str, ...]) -> bool:
    """True only when the request asks *for* the artefact, not merely mentions it.

    Three signals, in priority order:
    1. an explanation verb ("說明…指令") means the noun is the subject, not the ask;
    2. an explicit production verb ("寫一個…指令") means the artefact is the ask;
    3. a bare noun ("adb 指令") is an ask only when nothing else is in the sentence.
    """
    if not any(noun in text for noun in nouns):
        return False
    if any(verb in text for verb in _EXPLAIN_VERBS):
        return False
    if any(verb in text for verb in _ARTEFACT_VERBS):
        return True
    stripped = text.strip(" ?？。.!！\n")
    return len(stripped) <= 24


_MUST_CALL_TOOL = re.compile(
    r"(必須|必须|一定要).{0,12}(呼叫|调用|調用|使用).{0,8}工具|"
    r"(呼叫|调用|調用|使用).{0,8}工具.{0,12}(必須|必须|不要心算|一定要)|"
    r"must call (?:the )?(?:\w+ )?tool|use (?:the )?(?:\w+ )?tool", re.I)
_NO_TOOL = re.compile(
    r"(不要|無須|无需|不必).{0,8}(使用|呼叫|调用|調用).{0,6}工具|"
    r"(?:do not|don't|dont) (?:call|use) (?:the )?tools?", re.I)


def _tool_contract(tools: bool | list[dict] | dict) -> tuple[tuple[str, ...], dict[str, dict]]:
    if not isinstance(tools, (list, dict)):
        return (), {}
    rows = tools.get("tools", []) if isinstance(tools, dict) else tools
    names: list[str] = []
    schemas: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        function = row.get("function") if row.get("type") == "function" else row
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.append(name)
            parameters = function.get("parameters")
            if isinstance(parameters, dict):
                schemas[name] = parameters
    return tuple(dict.fromkeys(names)), schemas


def shape_from(user_text: str, tools: bool | list[dict] | dict = False,
               tool_choice: object = None,
               response_format: object = None) -> RequestShape:
    text = (user_text or "").lower()
    names, schemas = _tool_contract(tools)
    choice = tool_choice
    required_name = ""
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict):
            required_name = str(function.get("name") or "").strip()
    forced = choice == "required" or bool(required_name)
    no_tools = choice == "none" or bool(_NO_TOOL.search(text))
    response_type = response_format.get("type") if isinstance(response_format, dict) else ""
    json_required = response_type in ("json_object", "json_schema")
    schema = None
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict):
            schema = json_schema["schema"]
    return RequestShape(
        tools=bool(tools),
        wants_command=_wants_artefact(text, _COMMAND_NOUNS),
        wants_code=_wants_artefact(text, _CODE_NOUNS),
        wants_python_code=(_wants_artefact(text, _CODE_NOUNS)
                           and bool(re.search(r"\bpython(?:3)?\b|\bpy\b", text))),
        asks_for_one_liner=any(word in text for word in
                               ("一句話", "一句话", "one line", "one-liner", "簡短", "简短")),
        tool_names=names,
        tool_schemas=schemas,
        tool_required=forced or (bool(tools) and bool(_MUST_CALL_TOOL.search(text))),
        required_tool_name=required_name,
        tools_forbidden=no_tools,
        json_required=json_required,
        json_schema=schema,
    )


def score(text: str, shape: RequestShape | None = None) -> Verdict:
    """Return a deliverability verdict with the reason that decided it."""
    shape = shape or RequestShape()
    signals = analyse(text)
    if signals.chars == 0:
        return Verdict(False, 0.0, "empty", signals)

    points = 0.0
    reasons: list[str] = []

    if signals.fences:
        points += 3.0
        reasons.append("fence")
    if signals.language_hint:
        points += 1.0
        reasons.append("language-hint")
    if signals.grounding:
        points += min(3.0, 0.5 * signals.grounding)
        reasons.append(f"grounding={signals.grounding}")
    if signals.identifiers >= 3:
        points += 1.5
        reasons.append("identifiers")
    if signals.numbered >= 3:
        points += 1.0
        reasons.append("enumerated")
    if signals.headings:
        points += 0.5
        reasons.append("headed")
    if signals.chars >= 400:
        points += 0.5
        reasons.append("substantial")

    # A one-line request answered in one line is correct, not thin.
    if shape.asks_for_one_liner and signals.chars < 200 and not signals.fences:
        points += 2.0
        reasons.append("matches-one-liner")
    if shape.wants_command and COMMAND.search(text or ""):
        points += 1.0
        reasons.append("command-present")
    if shape.wants_code and signals.fences:
        points += 1.0
        reasons.append("code-present")

    valid_python = True
    if shape.wants_python_code:
        python_blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```",
                                   text or "", flags=re.I | re.S)
        valid_python = False
        for block in python_blocks:
            try:
                ast.parse(block)
            except SyntaxError:
                continue
            valid_python = True
            break

    if signals.pivot:
        points -= 2.0
        reasons.append("pivot")
    if signals.meta and signals.fences == 0:
        points -= 1.5
        reasons.append("meta-only")
    if signals.hedge and signals.grounding == 0:
        points -= 1.0
        reasons.append("vague")

    # Deliverability is a *gate*, and the gate should be hard to fail: the scorer
    # exists to rank attempts and to catch a reply that dodged the request — not
    # to enforce a word count. Retrying a short correct answer costs a full
    # request and can return something worse; measured, that happens on "hi".
    dodged = signals.pivot or (signals.meta and signals.fences == 0)
    missing_artefact = ((shape.wants_code and not signals.fences)
                        or (shape.wants_command and not COMMAND.search(text or ""))
                        or (shape.wants_python_code and not valid_python))
    if missing_artefact:
        reasons.append("requested-python-invalid" if shape.wants_python_code and not valid_python
                       else "requested-artefact-missing")
    deliverable = not dodged and not missing_artefact and signals.chars > 0
    return Verdict(deliverable, round(points, 2), "+".join(reasons) or "plain", signals)


def _calls_from(data: dict) -> list[dict]:
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        return [call for call in calls if isinstance(call, dict)]
    legacy = message.get("function_call")
    return [{"function": legacy}] if isinstance(legacy, dict) else []


def _arguments_object(call: dict) -> dict | None:
    function = call.get("function") or {}
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _matches_type(value: object, expected: str) -> bool:
    return {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }.get(expected, lambda _v: True)(value)


def _validate_tool_call(call: dict, shape: RequestShape) -> tuple[bool, str]:
    function = call.get("function") or {}
    if not isinstance(function, dict):
        return False, "function-missing"
    name = str(function.get("name") or "").strip()
    if not name:
        return False, "name-missing"
    if shape.tool_names and name not in shape.tool_names:
        return False, "tool-not-offered"
    if shape.required_tool_name and name != shape.required_tool_name:
        return False, "forced-tool-mismatch"
    args = _arguments_object(call)
    if args is None:
        return False, "arguments-not-json-object"
    schema = shape.tool_schemas.get(name, {})
    required = schema.get("required") or []
    if any(key not in args for key in required if isinstance(key, str)):
        return False, "required-argument-missing"
    properties = schema.get("properties") or {}
    if isinstance(properties, dict):
        for key, spec in properties.items():
            if key in args and isinstance(spec, dict) and not _matches_type(args[key], str(spec.get("type") or "")):
                return False, "argument-type-mismatch"
        if schema.get("additionalProperties") is False and any(key not in properties for key in args):
            return False, "unexpected-argument"
    return True, "valid-tool-call"


def score_completion(data: dict, shape: RequestShape | None = None) -> Verdict:
    """Score text and structured tool calls against the supplied request shape."""
    shape = shape or RequestShape()
    choices = data.get("choices") or [] if isinstance(data, dict) else []
    message = ((choices[0] or {}).get("message") or {}) if choices else {}
    body = message.get("content") or ""
    text = body if isinstance(body, str) else ""
    text_verdict = score(text, shape)
    calls = _calls_from(data) if isinstance(data, dict) else []

    if shape.tools_forbidden and calls:
        text_verdict.signals.tool_calls = len(calls)
        return Verdict(False, text_verdict.score, "tool-call-not-allowed", text_verdict.signals)
    if calls:
        checks = [_validate_tool_call(call, shape) for call in calls]
        valid = all(ok for ok, _ in checks)
        text_verdict.signals.tool_calls = len(calls)
        text_verdict.signals.valid_tool_calls = sum(ok for ok, _ in checks)
        if valid:
            score_value = min(6.0, 4.0 + 0.5 * len(calls))
            return Verdict(True, score_value,
                           f"valid-tool-call={len(calls)}", text_verdict.signals)
        reason = next(reason for ok, reason in checks if not ok)
        return Verdict(False, text_verdict.score, f"invalid-tool-call:{reason}",
                       text_verdict.signals)
    if shape.tool_required:
        return Verdict(False, text_verdict.score, "required-tool-call-missing",
                       text_verdict.signals)
    if shape.json_required:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return Verdict(False, text_verdict.score, "invalid-json-response",
                           text_verdict.signals)
        if not isinstance(parsed, dict):
            return Verdict(False, text_verdict.score, "json-object-required",
                           text_verdict.signals)
        schema = shape.json_schema or {}
        if schema.get("type") == "object":
            required = schema.get("required") or []
            if any(key not in parsed for key in required if isinstance(key, str)):
                return Verdict(False, text_verdict.score, "json-required-property-missing",
                               text_verdict.signals)
            properties = schema.get("properties") or {}
            if isinstance(properties, dict):
                for key, spec in properties.items():
                    if key in parsed and isinstance(spec, dict) and not _matches_type(
                            parsed[key], str(spec.get("type") or "")):
                        return Verdict(False, text_verdict.score,
                                       "json-property-type-mismatch", text_verdict.signals)
                if schema.get("additionalProperties") is False and any(
                        key not in properties for key in parsed):
                    return Verdict(False, text_verdict.score, "json-extra-property",
                                   text_verdict.signals)
        return Verdict(True, max(text_verdict.score, 2.0), "valid-json-object",
                       text_verdict.signals)
    return text_verdict


def is_thin(text: str, floor: int = 180) -> bool:
    signals = analyse(text)
    return signals.chars < floor and signals.fences == 0 and signals.grounding == 0
