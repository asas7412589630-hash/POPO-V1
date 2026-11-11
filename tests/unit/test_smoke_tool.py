import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("smoke_real", ROOT / "tools" / "smoke_real.py")
SMOKE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SMOKE)


class SmokeToolTests(unittest.TestCase):
    def test_cases_are_declared_with_names(self):
        self.assertGreaterEqual(len(SMOKE.CASES), 1)
        for name, prompt in SMOKE.CASES:
            self.assertTrue(name)
            self.assertTrue(prompt)

    def test_no_credential_in_source(self):
        text = (ROOT / "tools" / "smoke_real.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"sk-[A-Za-z0-9]{16,}")

    def test_spend_cap_is_a_parameter(self):
        text = (ROOT / "tools" / "smoke_real.py").read_text(encoding="utf-8")
        self.assertIn("--max-spend", text)
        self.assertIn("spend cap reached", text)

    def test_module_imports_without_network(self):
        # Importing the tool must not require a configured upstream.
        self.assertTrue(callable(SMOKE.main))


if __name__ == "__main__":
    unittest.main()
