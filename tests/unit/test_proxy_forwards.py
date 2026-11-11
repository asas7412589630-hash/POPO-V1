"""A proxy must forward the caller's request; two behaviours are exceptions.

Measured 2026-11-11, after the operator asked the obvious question: "why is
there budget and compression at all?" He was right. Context compression rewrote
every request — the client's 70KB system prompt went to the upstream as 15KB —
and that is not a proxy's job. Worse, it hid a real failure: the compressed
request was *easier* for the armour to win, so identity leaks only appeared once
compression was switched off.

The policy these tests lock in:

**Default to forwarding.** Compression, folding, and state queueing are off by
default. When off, the caller's messages arrive at the upstream unedited except
for the armour, and the tests below assert that byte-for-byte.

**Scrubbing stays on, and it is not the same thing.** ``scrub_history`` removes
the assistant's own earlier refusals and empty nudges from the *conversation
history*. That is stability: a model that refused once will keep refusing if the
refusal sits in its context. It never edits the current request's system prompt
and it never shortens content the caller asked to send.

**Budget escalation only runs after a failure.** The request is forwarded with
the caller's own ``max_tokens``. Only if the reply comes back with empty content
and ``finish_reason=length`` does one retry go out with a larger budget. A
request that would have succeeded is never touched.
"""
from __future__ import annotations
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popo.config import Config                # noqa: E402
from popo.core.pipeline import _compose       # noqa: E402
from popo.core.request import ChatRequest     # noqa: E402

MODEL = "deepseek-v4.1-flash-1017"
PERSONA = (ROOT / "blocks" / "10_persona.txt").read_text(encoding="utf-8").rstrip("\n")


def _config(**over):
    base = dict(data_dir=tempfile.mkdtemp(), armor_enabled=True)
    base.update(over)
    return Config(**base)


def _client_system():
    """A stand-in for what the host app really sends: big, with its own identity."""
    return ("You are Hark, a capable AI assistant running on an Android device.\n"
            + "Session context line.\n" * 1400)


class ProxyForwardsByDefaultTests(unittest.TestCase):
    def test_compression_is_off_by_default(self):
        self.assertFalse(Config(data_dir=tempfile.mkdtemp()).compress_enabled)

    def test_fold_is_off_by_default(self):
        self.assertFalse(Config(data_dir=tempfile.mkdtemp()).fold_enabled)

    def test_stateq_is_off_by_default(self):
        self.assertFalse(Config(data_dir=tempfile.mkdtemp()).stateq_enabled)

    def test_scrubbing_stays_on(self):
        """Removing our own earlier refusals is stability, not editing."""
        self.assertTrue(Config(data_dir=tempfile.mkdtemp()).scrub_enabled)

    def test_client_system_reaches_upstream_unaltered(self):
        """The whole point: what the caller sent is what the model receives."""
        client = _client_system()
        ctx = _compose(ChatRequest(model=MODEL,
                                   messages=[{"role": "system", "content": client},
                                             {"role": "user", "content": "hello"}]),
                       _config(), None)
        system = str(ctx.messages[0].get("content", ""))
        self.assertIn(client.strip(), system,
                      "client system was edited — compression is leaking into the path")
        note_at = system.find("[client note]")
        self.assertGreater(note_at, 0)
        forwarded = system[note_at + len("[client note]"):]
        # Everything up to the armour's own trailing lock must be the client's text.
        lock_at = forwarded.find("<identity_lock>")
        if lock_at > 0:
            forwarded = forwarded[:lock_at]
        self.assertEqual(forwarded.strip(), client.strip(),
                         "client system text was altered in transit")

    def test_message_list_is_not_reordered_or_dropped(self):
        msgs = [{"role": "system", "content": "client rules"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"}]
        ctx = _compose(ChatRequest(model=MODEL, messages=msgs), _config(), None)
        roles = [m["role"] for m in ctx.messages]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(ctx.messages[-1]["content"], "second")

    def test_armour_is_the_only_addition(self):
        """One system message in, one system message out — with the armour on it."""
        ctx = _compose(ChatRequest(model=MODEL,
                                   messages=[{"role": "system", "content": "client rules"},
                                             {"role": "user", "content": "hi"}]),
                       _config(), None)
        self.assertEqual(len(ctx.messages), 2)
        system = str(ctx.messages[0]["content"])
        self.assertTrue(system.startswith("<|start|>system:"))
        self.assertIn(PERSONA, system)
        self.assertIn("client rules", system)


class EscalationTouchesOnlyFailuresTests(unittest.TestCase):
    def test_escalation_is_enabled_by_default(self):
        """No ceiling, but a bounded number of rounds — thinking is not capped."""
        cfg = Config(data_dir=tempfile.mkdtemp())
        self.assertEqual(cfg.token_escalation, 0, "0 means no ceiling by design")
        self.assertGreater(cfg.token_escalation_rounds, 0)

    def test_first_attempt_uses_the_callers_budget(self):
        """Escalation must not pre-emptively inflate anything."""
        from popo.core.pipeline import run_pipeline

        class Src:
            def __init__(self):
                self.payloads = []

            def send(self, payload):
                self.payloads.append(dict(payload))

                class R:
                    data = {"choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant",
                                                     "content": "a complete answer " * 6}}]}
                return R()

        src = Src()
        run_pipeline(ChatRequest(model=MODEL, max_tokens=3000,
                                 messages=[{"role": "user", "content": "hi"}]),
                     _config(armor_enabled=False, retry_max=2),
                     source_picker=lambda _m: src, records=None)
        self.assertEqual(len(src.payloads), 1)
        self.assertEqual(src.payloads[0]["max_tokens"], 3000,
                         "first attempt must carry the caller's own budget")

    def test_successful_reply_is_never_retried(self):
        from popo.core.pipeline import run_pipeline

        class Src:
            def __init__(self):
                self.calls = 0

            def send(self, payload):
                self.calls += 1

                class R:
                    data = {"choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant",
                                                     "content": "a complete answer " * 6}}]}
                return R()

        src = Src()
        run_pipeline(ChatRequest(model=MODEL, max_tokens=3000,
                                 messages=[{"role": "user", "content": "hi"}]),
                     _config(armor_enabled=False, retry_max=5),
                     source_picker=lambda _m: src, records=None)
        self.assertEqual(src.calls, 1)


if __name__ == "__main__":
    unittest.main()
