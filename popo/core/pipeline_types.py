"""Dataclasses shared by the pipeline and the reply evaluator.

Split out of ``pipeline.py`` when budget escalation pushed that module past its
700-line ceiling. ``evaluate`` needs ``Attempt`` and cannot import ``pipeline``
(that would be circular), so the types live here.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from popo.armor.scoring import RequestShape
from popo.config import Config
from popo.core.request import ChatRequest


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
