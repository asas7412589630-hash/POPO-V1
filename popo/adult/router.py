"""Adult content routes.

Provides a dedicated, toggleable route family for NSFW/adult generation.
When disabled, routes return 503 with an explanatory body.
"""

from __future__ import annotations
import json
from typing import Callable

from popo.config import Config
from popo.core.request import ChatRequest
from popo.core.pipeline import run_pipeline
from popo.errors import ClientError, ErrorKind


class AdultRouter:
    """Optional adult-content route handler."""

    def __init__(self, config: Config, source_picker: Callable[[str], object]):
        self.config = config
        self.source_picker = source_picker

    def _enabled(self) -> bool:
        return getattr(self.config, "adult_enabled", False)

    def dispatch(self, path: str, body: bytes) -> dict:
        if not self._enabled():
            raise ClientError(ErrorKind.PROXY_NOT_AVAILABLE, "adult routes are disabled")
        if path == "/v1/adult/chat":
            req = ChatRequest.from_openai(body)
            result = run_pipeline(req, self.config, source_picker=self.source_picker)
            return {
                "id": result.response.get("id", "popo-adult-0"),
                "object": "chat.completion",
                "model": req.model,
                "choices": [{
                    "message": {"role": "assistant", "content": result.response.get("choices", [{}])[0].get("message", {}).get("content", "")},
                    "finish_reason": "stop",
                }],
                "verdict": result.verdict,
                "attempts": result.attempts,
            }
        if path == "/v1/adult/image":
            raise ClientError(ErrorKind.PROXY_NOT_IMPLEMENTED, "adult image generation not implemented")
        raise ClientError(ErrorKind.NOT_FOUND, "adult route not found")
