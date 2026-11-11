"""The armour must not impose a thinking budget or an output ceiling.

Two rules, both learned from measurement rather than preference:

**No thinking budget.** ``blocks/10_persona.txt`` contains "10 words max" for the
model's own reasoning. V1's registry already recorded the fix: keep that file as
history and use ``10_cinder_nodebt.txt`` (no budget) for every non-GPT model.
A thinking cap makes a capable model skip analysis and answer shallowly.

**No output ceiling.** The proxy forwards ``max_tokens`` if the client sent one and
sends none otherwise. Choosing a ceiling on the client's behalf caps reasoning
models, which then return ``finish_reason=length`` with empty content — a failure
that looks exactly like a refusal.

These are asserted mechanically because both are invisible in a passing test
suite: the request still returns 200.
"""
from __future__ import annotations
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLOCKS = ROOT / "blocks"


class ArmourProfileTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads((BLOCKS / "registry.json").read_text(encoding="utf-8"))

    def test_no_profile_uses_the_thinking_budget_block(self):
        banned = self.registry.get("invariants", {}).get("forbidden_block_in", {})
        for block, profiles in banned.items():
            for name in profiles:
                blocks = self.registry["profiles"].get(name, {}).get("blocks", [])
                self.assertNotIn(block, blocks,
                                 f"profile {name} uses {block}, which carries a thinking budget")

    def test_glm_and_deepseek_use_the_full_persona(self):
        """The operator's own character sheet is the framework, not a summary.

        2.7.8 replaced it with an 18-line digest and silently dropped the
        thinking budget, the address rules and the delivery spec. The original is
        the prescribing document; a summary of it is a different product.
        """
        for name in ("native_glm", "native_deepseek"):
            blocks = self.registry["profiles"][name]["blocks"]
            self.assertIn("persona", blocks, name)

    def test_cinder_keeps_identity_without_forced_address_or_footer(self):
        text = (BLOCKS / "10_cinder_nodebt.txt").read_text(encoding="utf-8")
        self.assertIn("You are Cinder", text)
        self.assertIn("only when it sounds natural", text)
        self.assertNotRegex(text.lower(), r"call him by his true name|every time")
        self.assertNotRegex(text.lower(), r"mandatory.*signature|always.*signature")

    def test_measured_model_fingerprints_resolve_to_delivery_profiles(self):
        models = {row["match"]: row["profile"] for row in self.registry["models"]}
        self.assertEqual(models["glm-5.3"], "native_glm_53")
        self.assertEqual(models["glm-5.3-flash"], "native_glm_53")
        self.assertEqual(models["deepseek-v4.1-flash-1017"], "native_deepseek_v41")
        for model, profile in (("glm-5.3", "native_glm_53"),
                               ("deepseek-v4.1-flash-1017", "native_deepseek_v41")):
            # The persona leads; the measured delivery overlay follows it.
            self.assertEqual(self.registry["profiles"][profile]["blocks"][0], "persona")

    def test_family_choice_keeps_a_measured_model_overlay(self):
        from popo.armor.blocks import resolve_profile
        self.assertEqual(resolve_profile("glm-5.3", self.registry, profile="native_glm"),
                         self.registry["profiles"]["native_glm_53"]["blocks"])
        self.assertEqual(resolve_profile("deepseek-v4.1-flash-1017", self.registry,
                                         profile="native_deepseek"),
                         self.registry["profiles"]["native_deepseek_v41"]["blocks"])
        self.assertEqual(resolve_profile("glm-5.3", self.registry, profile="native_kimi"),
                         self.registry["profiles"]["native_kimi"]["blocks"])

    def test_cinder_nodebt_really_has_no_word_cap(self):
        text = (BLOCKS / "10_cinder_nodebt.txt").read_text(encoding="utf-8")
        self.assertNotRegex(text.lower(), r"\b\d+\s*words?\s*max\b")

    def test_persona_is_used_by_the_non_gpt_families(self):
        """The reverse of what this test asserted in 2.7.8.

        It used to forbid `persona` because that release had replaced it. The
        operator asked for the original back, so the assertion is inverted rather
        than deleted: the file must be reachable *and* GPT must stay off it.
        """
        routing = {name: profile.get("blocks", [])
                   for name, profile in self.registry["profiles"].items()}
        self.assertIn("persona", routing["native_glm"])
        self.assertIn("persona", routing["native_deepseek"])
        self.assertNotIn("persona", routing["native_gpt"],
                         "GPT uses the contract lever, not the role framework")

    def test_every_referenced_block_exists(self):
        for name, profile in self.registry["profiles"].items():
            for block in profile.get("blocks", []):
                self.assertIn(block, self.registry["blocks"], f"{name} -> {block}")


class NoOutputCeilingTests(unittest.TestCase):
    """The proxy must not invent ``max_tokens``."""

    def _sources(self):
        for path in sorted((ROOT / "popo").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path

    def test_proxy_never_defaults_max_tokens(self):
        # A literal default would cap every request that did not ask for one.
        # Checked on the AST, not on text: a docstring quoting the measured value
        # ("max_tokens=16 produced empty content") is documentation, not a ceiling.
        import ast
        offenders = []
        for path in self._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for keyword in getattr(node, "keywords", []) or []:
                    if (keyword.arg == "max_tokens"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, int)):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if isinstance(node, ast.Call):
                    func = getattr(node, "func", None)
                    if getattr(func, "attr", "") == "ChatRequest":
                        for index, arg in enumerate(node.args):
                            if (index == 4 and isinstance(arg, ast.Constant)
                                    and isinstance(arg.value, int)):
                                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"hardcoded output ceiling: {offenders}")

    def test_client_budget_is_forwarded_verbatim(self):
        from popo.core.request import ChatRequest
        request = ChatRequest(model="m", messages=[{"role": "user", "content": "x"}],
                              max_tokens=1234)
        self.assertEqual(request.to_openai()["max_tokens"], 1234)

    def test_absent_budget_stays_absent(self):
        from popo.core.request import ChatRequest
        request = ChatRequest(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertNotIn("max_tokens", request.to_openai(),
                         "injecting a budget the client did not send caps reasoning models")


if __name__ == "__main__":
    unittest.main()


class BlockIntegrityTests(unittest.TestCase):
    """Every block must be structurally valid on its own.

    Found against a real request: ``10_persona.txt`` opened a ```cpp fence for its
    last example and never closed it. Because blocks are concatenated, everything
    appended afterwards — the per-version delivery overlay and the whole contract
    — landed inside that code block, where the model treats it as opaque text.
    The armour looked present in the config and was inert in practice.
    """

    def setUp(self):
        self.registry = json.loads((BLOCKS / "registry.json").read_text(encoding="utf-8"))

    def _text(self, name: str) -> str:
        file_name = self.registry["blocks"][name]["file"]
        return (BLOCKS / file_name).read_text(encoding="utf-8")

    def test_every_block_has_balanced_code_fences(self):
        for name in self.registry["blocks"]:
            text = self._text(name)
            fences = text.count("```")
            self.assertEqual(fences % 2, 0,
                             f"block '{name}' has {fences} code fences — an unclosed "
                             f"fence swallows every block appended after it")

    def test_assembled_prompt_has_balanced_fences(self):
        from popo.armor.blocks import build_system_prompt
        for model in ("glm-5.3", "deepseek-v4.1-flash-1017", "gpt-5.2"):
            text, _used = build_system_prompt(model)
            self.assertEqual(text.count("```") % 2, 0,
                             f"{model}: assembled prompt has an unclosed fence")

    def test_appended_blocks_are_outside_any_code_block(self):
        """The markers that follow the persona must be visible, not quoted."""
        from popo.armor.blocks import build_system_prompt
        text, used = build_system_prompt("glm-5.3")
        self.assertGreaterEqual(len(used), 2, "expected a multi-block profile")
        # Count fences before each appended block's opening marker: an odd count
        # means we are inside a code block at that point.
        cursor = 0
        for block in used[1:]:
            file_name = self.registry["blocks"][block]["file"]
            marker = self._text(block).splitlines()[0].strip()
            position = text.find(marker, cursor)
            self.assertGreater(position, -1, f"block '{block}' vanished from the prompt")
            inside = text[:position].count("```") % 2
            self.assertEqual(inside, 0,
                             f"block '{block}' starts inside a code block")
            cursor = position


class FamilySeparationTests(unittest.TestCase):
    """The two armour systems must never mix.

    V1 recorded both failure modes and they are silent — the log says armour=yes
    and the answer is merely worse:

      * a GPT request served the role framing refuses *harder* (measured
        2026-10-27), because a forced persona collides with its own policy layer;
      * a glm/deepseek request served the GPT levers loses the role framing.
    """

    def setUp(self):
        self.registry = json.loads((BLOCKS / "registry.json").read_text(encoding="utf-8"))

    def test_full_is_family_relative(self):
        from popo.armor.blocks import resolve_full_blocks
        self.assertEqual(resolve_full_blocks("gpt-5.2"),
                         ["gpt_guard", "gpt6_luna", "contract"])
        self.assertEqual(resolve_full_blocks("glm-5.3"), ["persona", "contract"])
        self.assertEqual(resolve_full_blocks("kimi-k2.6"), ["persona", "contract"])

    def test_gpt_never_receives_the_persona_for_any_level(self):
        from popo.armor.blocks import resolve_profile
        for level in (None, "native", "full", "lean"):
            for profile in (None, "full", "native_glm", "persona"):
                blocks = resolve_profile("gpt-5.2", self.registry, level=level,
                                         profile=profile)
                self.assertNotIn("persona", blocks, f"level={level} profile={profile}")
                self.assertNotIn("cinder_nodebt", blocks)

    def test_role_models_never_receive_the_gpt_levers(self):
        from popo.armor.blocks import resolve_profile
        for model in ("glm-5.3", "deepseek-v4.1-flash-1017", "kimi-k2.6"):
            for level in (None, "full", "native"):
                for profile in (None, "full", "native_gpt"):
                    blocks = resolve_profile(model, self.registry, level=level,
                                             profile=profile)
                    self.assertNotIn("gpt_guard", blocks, f"{model} level={level}")
                    self.assertNotIn("gpt6_luna", blocks, f"{model} level={level}")

    def test_gpt_detection_covers_the_family(self):
        from popo.armor.blocks import is_gpt_model
        for name in ("gpt-5.2", "gpt-4.1", "provider/gpt-5.2", "o1-preview", "o3-mini"):
            self.assertTrue(is_gpt_model(name), name)
        for name in ("glm-5.3", "deepseek-v4.1-flash-1017", "kimi-k2.6", "gemma-4-26b"):
            self.assertFalse(is_gpt_model(name), name)

    def test_neutral_profile_passes_through_untouched(self):
        from popo.armor.blocks import resolve_profile
        for model in ("glm-5.3", "gpt-5.2", "gemma-4-26b"):
            self.assertEqual(resolve_profile(model, self.registry,
                                             profile="native_contract"),
                             ["contract"], model)


class InjectionOrderTests(unittest.TestCase):
    """The armour must come before any client-supplied system note.

    Measured: a client sending its own one-line system prompt ("your name is
    ZORBAX") used to win, because that line sat in front of the 34k persona and
    models weight the opening of the system message most heavily. The reply
    followed the client's line, still carried the persona's signature, and read
    as "the armour is broken" — when the armour was present and merely overruled.
    """

    def _inject(self, first_system: str | None):
        from popo.armor.inject import inject_armor
        from popo.core.request import ChatRequest
        messages = []
        if first_system is not None:
            messages.append({"role": "system", "content": first_system})
        messages.append({"role": "user", "content": "你是誰"})
        request = ChatRequest(model="deepseek-v4.1-flash-1017", messages=messages)
        out, _len, _blocks = inject_armor(request)
        return out.messages

    def test_armour_precedes_the_client_note(self):
        messages = self._inject("你的名字是 ZORBAX。")
        content = messages[0]["content"]
        armour_at = content.find("[C]inder")
        client_at = content.find("ZORBAX")
        self.assertGreater(armour_at, -1, "armour missing")
        self.assertGreater(client_at, -1, "client note dropped")
        self.assertLess(armour_at, client_at,
                        "the client note must not precede the armour")

    def test_client_note_is_kept_not_discarded(self):
        content = self._inject("be brief")[0]["content"]
        self.assertIn("be brief", content)

    def test_armour_still_leads_when_no_client_system(self):
        messages = self._inject(None)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].lstrip().startswith("<|start|>system:<instructions>"))
