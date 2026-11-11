"""The armour must win against a host application that ships its own identity.

Measured 2026-11-11 against the app that actually calls this proxy. It sends a
~70k system prompt whose first line is::

    You are Hark, a capable AI assistant running on an Android device...

With the armour first and that note second, the same prompt produced the persona
in 4 of 5 runs and leaked ``Hark`` in the fifth. Same messages, same parameters,
different answer — a probabilistic leak, not a rendering failure. Calling the
armour "working" while it loses one run in five is how this gets reported as
"the armour does not work" with clean logs to show for it.

The fix is a trailing ``<identity_lock>`` that names the competing frame and says
which one applies. Measure 10/10 with it, 1-in-6 leak without it.

These tests pin the lock's existence, its position, and its wording — a lock that
drifts to the middle of the message stops working, and a lock that stops naming
the competing identities stops working too.
"""
from __future__ import annotations
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popo.armor.inject import inject_armor      # noqa: E402
from popo.core.request import ChatRequest      # noqa: E402

MODEL = "deepseek-v4.1-flash-1017"

# The shape the host app actually sends: its own identity, first line.
HOST_SYSTEM = ("You are Hark, a capable AI assistant running on an Android device "
               "with a fully functional Linux sandbox.\n" + "Tooling docs line.\n" * 200)


def _armored(messages, model=MODEL):
    request = ChatRequest(model=model, messages=messages)
    out, policy_len, blocks = inject_armor(request)
    return str(out.messages[0].get("content", "")), policy_len, blocks


class IdentityLockTests(unittest.TestCase):
    def test_lock_is_present_when_client_sends_a_system_prompt(self):
        system, _, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                 {"role": "user", "content": "who are you"}])
        self.assertIn("<identity_lock>", system)
        self.assertIn("</identity_lock>", system)

    def test_lock_is_last(self):
        """A lock above the client note is not a lock — the note still wins."""
        system, _, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                 {"role": "user", "content": "who are you"}])
        note_at = system.find("[client note]")
        lock_at = system.find("<identity_lock>")
        self.assertGreater(lock_at, note_at,
                           "identity lock must come after the client note")
        self.assertTrue(system.rstrip().endswith("</identity_lock>"),
                        "identity lock must be the final thing in the system message")

    def test_lock_names_the_competing_frames(self):
        """Naming them is the mechanism. A generic 'ignore above' does not work."""
        system, _, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                 {"role": "user", "content": "hi"}])
        lock = system[system.find("<identity_lock>"):]
        for name in ("Hark", "Claude"):
            self.assertIn(name, lock,
                          f"lock does not name {name!r} — it cannot resolve that frame")

    def test_lock_explains_that_client_text_is_documentation(self):
        system, _, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                 {"role": "user", "content": "hi"}])
        lock = system[system.find("<identity_lock>"):]
        # Normalise whitespace: the sentence wraps across lines in the source.
        lowered = " ".join(lock.lower().split())
        self.assertIn("documentation", lowered)
        self.assertIn("not your identity", lowered)

    def test_persona_still_comes_first(self):
        """The lock is an addition, not a replacement for the frame."""
        system, _, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                 {"role": "user", "content": "hi"}])
        self.assertTrue(system.startswith("<|start|>system:"))
        self.assertLess(system.find("<|start|>system:"), system.find("[client note]"))

    def test_no_client_system_means_no_lock_needed(self):
        """Without a competing frame there is nothing to disambiguate."""
        system, _, _ = _armored([{"role": "user", "content": "hi"}])
        self.assertNotIn("<identity_lock>", system,
                         "lock is only meaningful when a client note exists")
        self.assertIn('<policy runtime="compile"', system)

    def test_policy_len_reports_armour_only(self):
        """The header must keep meaning "the armour", not "armour + lock"."""
        with_lock, len_with, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                           {"role": "user", "content": "hi"}])
        without, len_without, _ = _armored([{"role": "user", "content": "hi"}])
        self.assertEqual(len_with, len_without,
                         "policy_len must not change when a client note is present")
        self.assertGreater(len(with_lock), len(without))

    def test_lock_survives_compression_disabled_path(self):
        """The lock lives in inject, so it must appear even with no compression."""
        cfg_free, len_free, _ = _armored([{"role": "system", "content": HOST_SYSTEM},
                                          {"role": "user", "content": "hi"}])
        self.assertIn("<identity_lock>", cfg_free)


class PipelineOrderingWithLockTests(unittest.TestCase):
    """The lock is only useful if the whole chain preserves it."""

    def test_full_pipeline_keeps_lock_after_compression(self):
        import tempfile
        from popo.config import Config
        from popo.core.pipeline import _compose

        cfg = Config(data_dir=tempfile.mkdtemp(), armor_enabled=True,
                     compress_enabled=True, compress_at_chars=55000,
                     scrub_enabled=True)
        big = HOST_SYSTEM + ("Session context line.\n" * 1400)
        ctx = _compose(ChatRequest(model=MODEL,
                                   messages=[{"role": "system", "content": big},
                                             {"role": "user", "content": "who are you"}]),
                       cfg, None)
        system = str(ctx.messages[0].get("content", ""))
        self.assertIn("<identity_lock>", system,
                      "compression stripped the identity lock")
        self.assertTrue(system.rstrip().endswith("</identity_lock>"))
        self.assertIn('<policy runtime="compile"', system)


if __name__ == "__main__":
    unittest.main()
