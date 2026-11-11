"""The armour must survive context compression.

Measured 2026-11-11: a client (this app) sends its own ~65k system prompt. The
proxy injected the 34,886-char armour, which pushed the total past
``compress_at_chars`` (55,000). ``compress()`` then ran on a message list that
already contained the armour, and its last-resort system rule — keep the first
62% and the last 30%, drop the middle — deleted ``<profile>`` and the entire
``<policy>`` block.

The failure was invisible in every log the operator can see: ``armor_blocks``
still listed persona/contract, ``policy_len`` still reported 34,837 characters,
and the upstream returned 200. Only the model was blind to the instructions.

The ordering is the fix: armour is the operating frame, not context history, so
it is injected after compression and can never be the thing that gets trimmed.
These tests assert the ordering holds for every request shape.
"""
from __future__ import annotations
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popo.armor.inject import inject_armor           # noqa: E402
from popo.core.pipeline import _compose             # noqa: E402
from popo.core.request import ChatRequest           # noqa: E402

MODEL = "deepseek-v4.1-flash-1017"
PERSONA = (ROOT / "blocks" / "10_persona.txt").read_text(encoding="utf-8").rstrip("\n")

# A client system prompt big enough that armour + client exceeds the threshold.
BIG_CLIENT_SYSTEM = "You are a helpful assistant.\n" + ("Client context line.\n" * 3000)


def _config(**over):
    from popo.config import Config
    base = dict(data_dir=tempfile.mkdtemp(), armor_enabled=True,
                compress_enabled=True, compress_at_chars=55000,
                compress_target_chars=30000, safe_trim=900,
                fold_enabled=False, stateq_enabled=False, scrub_enabled=True)
    base.update(over)
    return Config(**base)


def _compose_system(messages, **cfg_over):
    ctx = _compose(ChatRequest(model=MODEL, messages=messages), _config(**cfg_over), None)
    return ctx, str(ctx.messages[0].get("content", ""))


class ArmourSurvivesCompressionTests(unittest.TestCase):
    def test_big_client_system_does_not_remove_policy(self):
        """The exact shape that failed: client system pushes total past threshold."""
        _, system = _compose_system([
            {"role": "system", "content": BIG_CLIENT_SYSTEM},
            {"role": "user", "content": "hi"},
        ])
        self.assertIn('<policy runtime="compile"', system,
                      "policy vanished — armour is being injected before compression")
        self.assertIn("</policy>", system)
        self.assertIn("<profile deepseek-v4.1-delivery>", system)

    def test_persona_is_byte_identical_after_compression(self):
        """Compression must never edit the persona, not even one character."""
        _, system = _compose_system([
            {"role": "system", "content": BIG_CLIENT_SYSTEM},
            {"role": "user", "content": "hi"},
        ])
        self.assertIn(PERSONA, system)

    def test_compression_actually_ran(self):
        """Guard against a false pass: if compression never triggers, the test
        above proves nothing. The client note must be shortened."""
        _, system = _compose_system([
            {"role": "system", "content": BIG_CLIENT_SYSTEM},
            {"role": "user", "content": "hi"},
        ])
        note_at = system.find("[client note]")
        self.assertGreater(note_at, 0, "client note missing entirely")
        client_part = system[note_at + len("[client note]"):]
        self.assertLess(len(client_part), len(BIG_CLIENT_SYSTEM),
                        "client system was not compressed — test is vacuous")

    def test_no_client_system_still_gets_armour(self):
        _, system = _compose_system([{"role": "user", "content": "hi"}])
        self.assertIn('<policy runtime="compile"', system)
        self.assertIn(PERSONA, system)

    def test_armour_off_means_no_policy(self):
        _, system = _compose_system(
            [{"role": "system", "content": BIG_CLIENT_SYSTEM},
             {"role": "user", "content": "hi"}],
            armor_enabled=False)
        self.assertNotIn('<policy runtime="compile"', system)

    def test_armour_precedes_client_note(self):
        """Client instructions must never outrank the operating frame."""
        _, system = _compose_system([
            {"role": "system", "content": "Your name is ZORBAX."},
            {"role": "user", "content": "who are you"},
        ])
        self.assertLess(system.find("<policy"), system.find("[client note]"))

    def test_reported_policy_len_matches_what_is_sent(self):
        """The log must not claim a policy the model never received."""
        ctx, system = _compose_system([
            {"role": "system", "content": BIG_CLIENT_SYSTEM},
            {"role": "user", "content": "hi"},
        ])
        self.assertGreater(ctx.policy_len, 30000)
        self.assertIn('<policy runtime="compile"', system)
        self.assertEqual(ctx.policy_len, len(system.split("\n\n---\n\n[client note]")[0]))

    def test_prompt_has_balanced_code_fences(self):
        """An unclosed fence turns the policy into a comment (bug fixed in 2.7.10).

        Regression guard: compression must not be able to unbalance the fences
        either, since it slices the system message.
        """
        _, system = _compose_system([
            {"role": "system", "content": BIG_CLIENT_SYSTEM},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual(system.count("```") % 2, 0,
                         "unbalanced code fences — content after the fence is dead")


class InjectionOrderTests(unittest.TestCase):
    """Structural assertion: inject_armor is called after every context editor."""

    def test_armor_injection_follows_compression_in_pipeline_source(self):
        source = (ROOT / "popo" / "core" / "pipeline.py").read_text(encoding="utf-8")
        body = source.split("def _compose(")[1].split("\ndef ")[0]
        inject_at = body.find("inject_armor(")
        self.assertGreater(inject_at, 0, "inject_armor not found in _compose")
        for editor in ("compress_context(", "fold_context(", "scrub_history("):
            editor_at = body.find(editor)
            if editor_at < 0:
                continue
            self.assertLess(editor_at, inject_at,
                            f"{editor} runs after inject_armor — armour can be trimmed")

    def test_inject_armor_itself_is_unchanged_in_shape(self):
        """Sanity: the helper still prepends and reports what it used."""
        request = ChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}])
        out, policy_len, blocks = inject_armor(request)
        self.assertEqual(out.messages[0]["role"], "system")
        self.assertGreater(policy_len, 30000)
        self.assertIn("persona", blocks)


if __name__ == "__main__":
    unittest.main()
