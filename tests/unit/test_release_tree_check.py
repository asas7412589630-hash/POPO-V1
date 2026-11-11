from __future__ import annotations
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_release_tree_tool", ROOT / "tools" / "check_release_tree.py")
CHECK = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CHECK)


class ReleaseTreeCheckTests(unittest.TestCase):
    def test_rejects_runtime_config_even_with_short_fake_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "popo.json").write_text('{"upstream":{"upstream_key":"sk-good"}}', encoding="utf-8")
            issues = CHECK.scan(root)
        self.assertTrue(any("runtime/secret config" in issue for issue in issues))

    def test_allows_example_config_with_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "popo.example.json").write_text(
                '{"upstream":{"upstream_key":"sk-replace-me"}}', encoding="utf-8")
            issues = CHECK.scan(root)
        self.assertEqual(issues, [])

    def test_rejects_non_placeholder_secret_in_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "authorized_keys.json").write_text(
                '{"client_key":"triad-abcdefghijklmnopqrstuvwxyz"}', encoding="utf-8")
            issues = CHECK.scan(root)
        self.assertTrue(any("non-placeholder secret field" in issue or "credential-shaped" in issue
                            for issue in issues))

    def test_dev_mode_ignores_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            data = root / "data"
            data.mkdir()
            (data / "records.jsonl").write_text("{}\n", encoding="utf-8")
            self.assertEqual(CHECK.scan(root, allow_runtime=True), [])
            self.assertTrue(CHECK.scan(root))

    def test_rejects_generated_binaries(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "bundle.tar.gz").write_bytes(b"\x1f\x8b")
            issues = CHECK.scan(root)
        self.assertTrue(any("binary artifact" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
