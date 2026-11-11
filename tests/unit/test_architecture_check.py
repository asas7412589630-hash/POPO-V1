from __future__ import annotations
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("architecture_check",
                                              ROOT / "tools" / "architecture_check.py")
CHECK = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CHECK)


class ArchitectureCheckTests(unittest.TestCase):
    def setUp(self):
        self.saved = CHECK.FAILURES
        CHECK.FAILURES = []

    def tearDown(self):
        CHECK.FAILURES = self.saved

    def test_rejects_oversized_function(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "large.py"
            path.write_text("def too_big():\n" + "    pass\n" * 305, encoding="utf-8")
            CHECK.inspect_file(path)
        self.assertTrue(any("too_big" in item for item in CHECK.FAILURES))

    def test_rejects_oversized_module(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "huge.py"
            path.write_text("X = 1\n" * 705, encoding="utf-8")
            CHECK.inspect_file(path)
        self.assertTrue(any("module is" in item for item in CHECK.FAILURES))

    def test_accepts_env_read_inside_function(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ok.py"
            path.write_text("import os\ndef configure():\n    return os.environ.get('X')\n",
                            encoding="utf-8")
            CHECK.inspect_file(path)
        self.assertEqual(CHECK.FAILURES, [])

    def test_rejects_module_level_env_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "bad.py"
            path.write_text("import os\nVALUE = os.environ.get('X')\n", encoding="utf-8")
            CHECK.inspect_file(path)
        self.assertTrue(any("module-level env read" in item for item in CHECK.FAILURES))

    def test_detects_import_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "alpha.py").write_text("from beta import x\n", encoding="utf-8")
            (root / "beta.py").write_text("from alpha import y\n", encoding="utf-8")
            saved_root = CHECK.ROOT
            try:
                CHECK.ROOT = root
                CHECK.check_import_cycles([root / "alpha.py", root / "beta.py"])
            finally:
                CHECK.ROOT = saved_root
        self.assertTrue(any("import cycle" in item for item in CHECK.FAILURES))

    def test_source_tree_is_clean(self):
        CHECK.FAILURES = []
        files = [p for p in sorted((ROOT / "popo").rglob("*.py"))
                 if "__pycache__" not in p.parts]
        for path in files:
            CHECK.inspect_file(path)
        CHECK.check_import_cycles(files)
        self.assertEqual(CHECK.FAILURES, [])


if __name__ == "__main__":
    unittest.main()
