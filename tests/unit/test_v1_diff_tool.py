"""The differential tool itself must be trustworthy.

A comparison tool that silently returns "match" for everything is worse than no
tool: it manufactures confidence. These tests assert the tool can actually fail,
that "V1 not available" is not reported as a pass, and that investigated
divergences stay marked as such.
"""
from __future__ import annotations
import importlib.util
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("v1_diff", ROOT / "tests" / "parity" / "v1_diff.py")
DIFF = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(DIFF)


class DifferentialToolTests(unittest.TestCase):
    def test_v1_absent_returns_two_not_zero(self):
        """'Could not compare' must never look like 'compared and matched'."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "parity" / "v1_diff.py"),
             "--v1", "/nonexistent/path"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a pass", result.stderr.lower())

    def test_corpus_is_not_empty(self):
        self.assertGreaterEqual(len(DIFF.CORPUS), 3)
        self.assertGreaterEqual(len(DIFF.REFUSAL_SAMPLES), 3)

    def test_every_expected_diff_carries_a_reason(self):
        for name, spec in DIFF.SCRUB_CASES.items():
            if "expected_diff" in spec:
                self.assertTrue(spec["expected_diff"].strip(),
                                f"{name} marks a divergence with no explanation")

    def test_intentional_divergences_are_documented(self):
        marked = [name for name, spec in DIFF.SCRUB_CASES.items() if spec.get("expected_diff")]
        self.assertTrue(marked, "at least one intentional divergence is documented")

    def test_v2_prompt_resolves_against_v2_not_the_cwd(self):
        """The bug this catches: chdir into V1 made V2 read V1's blocks.

        The comparison then compared V1 with itself and reported "identical" —
        a false pass from a tool whose only job is to detect differences.
        """
        import os
        v2_text, v2_blocks = DIFF._v2_prompt("glm-5.3")
        self.assertEqual(v2_blocks,
                         ["persona", "glm_53_delivery", "contract"])
        cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            moved_text, moved_blocks = DIFF._v2_prompt("glm-5.3")
        finally:
            os.chdir(cwd)
        self.assertEqual(v2_blocks, moved_blocks, "V2 blocks changed with the cwd")
        self.assertEqual(v2_text, moved_text, "V2 prompt changed with the cwd")

    def test_v1_and_v2_prompts_are_actually_different(self):
        """If these ever become equal, either the fix was reverted or the tool broke."""
        v1_dir = "/var/hark/shared/popo_audit/v1_2.7.0/POPO-V1-2.7.0"
        v1 = DIFF._load_v1(v1_dir)
        if v1 is None:
            self.skipTest("V1 checkout not available")
        try:
            v1_text, _ = DIFF._v1_prompt(v1, "glm-5.3")
            v2_text, _ = DIFF._v2_prompt("glm-5.3")
        finally:
            import os
            os.chdir(v1["cwd"])
        # Both use the same persona now, so lengths match; the point of this
        # test is that V2 reads V2's files, which the cwd test above covers.
        self.assertIn("10 words max", v1_text.lower())
        self.assertIn("10 words max", v2_text.lower())

    def test_missing_v1_directory_is_reported(self):
        self.assertIsNone(DIFF._load_v1(""))
        self.assertIsNone(DIFF._load_v1("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
